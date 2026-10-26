"""Auth0 identity verification and application-owned workspace authorization.

No password, client secret, client-supplied identity or model output grants access.
This runtime serves one explicitly bound customer workspace.
"""
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from .model import DemoError, digest, require


ROLES = {
    'viewer': ['read'],
    'editor': ['read', 'propose', 'record_pending'],
    'reviewer': ['read', 'propose', 'record_pending', 'confirm'],
}


def configured_web_origins():
    """Explicit browser origins for protected mobile APIs, never legacy routes."""
    origins = set()
    for value in os.getenv('KD_WEB_ALLOWED_ORIGINS', '').split(','):
        origin = value.strip()
        if not origin:
            continue
        parsed = urlsplit(origin)
        require(parsed.scheme in {'http', 'https'} and parsed.hostname and
                not parsed.username and not parsed.password and not parsed.path and
                not parsed.query and not parsed.fragment and
                origin == f'{parsed.scheme}://{parsed.netloc}' and
                (parsed.scheme == 'https' or parsed.hostname in {'localhost', '127.0.0.1'}),
                'invalid_web_origin', '网页来源配置无效；只允许明确的 HTTPS 来源或本机 HTTP 来源。', 503)
        origins.add(origin)
    return origins


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    actor: str
    token_hash: str
    expires: int
    organization: str


class MobileAuth:
    def __init__(self, store):
        self.store = store
        self._jwks = None
        self._schema_ready = False
        self._lock = threading.Lock()
        self._rates = {}

    @property
    def workspace_id(self):
        return os.getenv('KD_MOBILE_TENANT_ID', '')

    def configuration(self):
        domain = os.getenv('KD_AUTH0_DOMAIN', '')
        audience = os.getenv('KD_AUTH0_AUDIENCE', '')
        clients = os.getenv('KD_AUTH0_CLIENT_IDS', '').split(',')
        parsed = urlsplit('https://' + domain)
        require(domain and parsed.hostname == domain and not parsed.port and
                not parsed.username and not parsed.path and audience and
                all(c.strip() for c in clients) and self.workspace_id,
                'mobile_not_configured', '手机端身份服务尚未配置。', 503)
        return 'https://' + domain + '/', audience, [c.strip() for c in clients]

    def initialize(self):
        with self._lock:
            if self._schema_ready:
                return
            with self.store.driver.session(database=self.store.config.database) as session:
                for label in ('KDMobileMember', 'KDMobileRevokedToken', 'KDMobileAccessAudit'):
                    session.run(f'CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS '
                                f'FOR (n:{label}) REQUIRE n.id IS UNIQUE').consume()
            self._schema_ready = True

    def rate_limit(self, key, limit, seconds=60):
        now = time.monotonic()
        with self._lock:
            # Bound memory even when callers send many invalid tokens.
            if len(self._rates) > 10000:
                self._rates = {k: v for k, v in self._rates.items() if v[0] > now}
            until, count = self._rates.get(key, (now + seconds, 0))
            if until <= now:
                until, count = now + seconds, 0
            require(count < limit, 'rate_limited', '请求过于频繁，请稍后重试。', 429)
            self._rates[key] = (until, count + 1)

    def authenticate(self, authorization):
        issuer, audience, clients = self.configuration()
        require(isinstance(authorization, str) and authorization.startswith('Bearer ') and
                0 < len(authorization) <= 16384,
                'session_expired', '请重新登录。', 401)
        token = authorization[7:]
        try:
            import jwt
            with self._lock:
                if self._jwks is None:
                    self._jwks = jwt.PyJWKClient(issuer + '.well-known/jwks.json',
                                                lifespan=300, timeout=10)
                jwks = self._jwks
            # Retry only public-key retrieval, never business actions or failed
            # token validation. Two bounded attempts fit the mobile HTTP timeout.
            for attempt in range(2):
                try:
                    signing_key = jwks.get_signing_key_from_jwt(token)
                    break
                except jwt.PyJWKClientConnectionError:
                    if attempt == 1:
                        raise
                    time.sleep(0.25)
            claims = jwt.decode(token, signing_key.key, algorithms=['RS256'],
                audience=audience, issuer=issuer, leeway=30,
                options={'require': ['exp', 'iat', 'iss', 'aud', 'sub']})
            require(claims.get('azp', claims.get('client_id')) in clients and isinstance(claims['sub'], str) and
                    0 < len(claims['sub']) <= 200 and not claims['sub'].endswith('@clients') and
                    type(claims['exp']) is int,
                    'session_expired', '请重新登录。', 401)
        except ImportError as exc:
            raise DemoError('mobile_not_configured', '身份验证依赖尚未安装。', 503) from exc
        except jwt.PyJWKClientConnectionError as exc:
            raise DemoError('identity_unavailable', '身份验证服务暂不可用，请重试。', 503) from exc
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            raise DemoError('session_expired', '请重新登录。', 401) from exc
        identity = Identity(issuer, claims['sub'], digest([issuer, claims['sub']]),
            hashlib.sha256(token.encode()).hexdigest(), claims['exp'], claims.get('org_id', ''))
        self.initialize()
        revoked = self.store.read(lambda tx: tx.run(
            'MATCH (r:KDMobileRevokedToken {id:$id}) WHERE r.expires>$now RETURN r.id AS id',
            id=identity.token_hash, now=time.time()).single())
        require(not revoked, 'session_expired', '请重新登录。', 401)
        return identity

    def authorize(self, identity, workspace=''):
        require(not workspace or workspace == self.workspace_id,
                'permission_denied', '无工作空间访问权限。', 403)
        mid = digest([identity.issuer, identity.subject, self.workspace_id])
        row = self.store.read(lambda tx: tx.run(
            'MATCH (m:KDMobileMember {id:$id,dataset:$d,workspace:$w,enabled:true}) '
            'RETURN properties(m) AS m', id=mid, d=self.store.dataset,
            w=self.workspace_id).single())
        require(row, 'workspace_access_required', '账号尚未获得工作空间访问权限。', 403)
        member = dict(row['m'])
        role = member.get('role')
        require(role in ROLES, 'permission_denied', '成员权限配置无效。', 403)
        expected_org = os.getenv('KD_AUTH0_ORGANIZATION_ID', '')
        require(not expected_org or identity.organization == expected_org,
                'permission_denied', '组织身份不匹配。', 403)
        return {'tenant': self.workspace_id, 'actor': identity.actor,
                'permissions': ROLES[role].copy()}, member

    def session(self, identity, workspace=''):
        from .mobile_speech import speech_available
        context, member = self.authorize(identity, workspace)
        space = {'id': self.workspace_id,
                 'name': os.getenv('KD_MOBILE_WORKSPACE_NAME', self.workspace_id)}
        return {'user': identity.actor, 'workspace': space, 'workspaces': [space],
                'role': member['role'], 'permissions': context['permissions'],
                'capabilities': {'speech': speech_available()}}

    def logout(self, identity):
        def revoke(tx):
            tx.run('MATCH (r:KDMobileRevokedToken) WHERE r.expires<=$now DELETE r',
                   now=time.time()).consume()
            tx.run('MERGE (r:KDMobileRevokedToken {id:$id}) '
                   'SET r.expires=$expires,r.actor=$actor', id=identity.token_hash,
                   expires=identity.expires, actor=identity.actor).consume()
        self.store.write(revoke)
        return {'signed_out': True}
