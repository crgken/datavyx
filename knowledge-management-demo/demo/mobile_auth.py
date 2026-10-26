"""Auth0 identity verification and application-owned workspace authorization.

No password, client secret, client-supplied identity or model output grants access.
Each authenticated user receives an application-owned personal workspace.
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
    'owner': ['read', 'propose', 'record_pending', 'confirm'],
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

    def configuration(self):
        domain = os.getenv('KD_AUTH0_DOMAIN', '')
        audience = os.getenv('KD_AUTH0_AUDIENCE', '')
        clients = os.getenv('KD_AUTH0_CLIENT_IDS', '').split(',')
        parsed = urlsplit('https://' + domain)
        require(domain and parsed.hostname == domain and not parsed.port and
                not parsed.username and not parsed.path and audience and
                all(c.strip() for c in clients),
                'mobile_not_configured', '手机端身份服务尚未配置。', 503)
        return 'https://' + domain + '/', audience, [c.strip() for c in clients]

    def initialize(self):
        with self._lock:
            if self._schema_ready:
                return
            with self.store.driver.session(database=self.store.config.database) as session:
                for label in ('KDMobileMember', 'KDMobileRevokedToken', 'KDMobileAccessAudit',
                              'KDUser', 'KDWorkspace', 'KDWorkspaceMember', 'KDWorkspaceAccessAudit'):
                    session.run(f'CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS '
                                f'FOR (n:{label}) REQUIRE n.id IS UNIQUE').consume()
                session.run('CREATE CONSTRAINT kdworkspace_dataset IF NOT EXISTS '
                            'FOR (w:KDWorkspace) REQUIRE w.dataset IS UNIQUE').consume()
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

    def ensure_personal_workspace(self, identity):
        self.initialize()
        now = time.time()
        workspace_id = 'personal:' + identity.actor
        member_id = digest([workspace_id, identity.actor])

        def provision(tx):
            tx.run('MERGE (u:KDUser {id:$id}) ON CREATE SET u.issuer=$issuer,u.subject=$subject,u.created_at=$now '
                   'SET u.last_seen_at=$now', id=identity.actor, issuer=identity.issuer,
                   subject=identity.subject, now=now).consume()
            existing = tx.run('MATCH (m:KDWorkspaceMember {actor:$actor,enabled:true}) '
                              'MATCH (w:KDWorkspace {id:m.workspace_id,status:"active"}) '
                              'RETURN w.id AS id ORDER BY CASE WHEN w.kind="personal" THEN 0 ELSE 1 END,'
                              'w.created_at,w.id LIMIT 1', actor=identity.actor).single()
            if existing:
                return existing['id']
            tx.run('MERGE (w:KDWorkspace {id:$id}) ON CREATE SET w.dataset=$dataset,w.kind="personal",'
                   'w.name="个人空间",w.owner_actor=$actor,w.status="active",w.created_at=$now '
                   'RETURN w.id AS id', id=workspace_id, dataset=workspace_id,
                   actor=identity.actor, now=now).consume()
            tx.run('MERGE (m:KDWorkspaceMember {id:$id}) ON CREATE SET m.workspace_id=$workspace,'
                   'm.dataset=$dataset,m.actor=$actor,m.role="owner",m.enabled=true,m.created_at=$now '
                   'SET m.updated_at=$now', id=member_id, workspace=workspace_id,
                   dataset=workspace_id, actor=identity.actor, now=now).consume()
            tx.run('MERGE (a:KDWorkspaceAccessAudit {id:$id}) ON CREATE SET a.workspace_id=$workspace,'
                   'a.actor=$actor,a.action="personal_workspace_created",'
                   'a.reason="First authenticated login",a.created_at=$now',
                   id=digest(['workspace-created', member_id]), workspace=workspace_id,
                   actor=identity.actor, now=now).consume()
            return workspace_id

        return self.store.write(provision)

    def authorize(self, identity, workspace=''):
        self.initialize()
        expected_org = os.getenv('KD_AUTH0_ORGANIZATION_ID', '')
        require(not expected_org or identity.organization == expected_org,
                'permission_denied', '组织身份不匹配。', 403)
        if not workspace:
            workspace = self.ensure_personal_workspace(identity)
        row = self.store.read(lambda tx: tx.run(
            'MATCH (m:KDWorkspaceMember {workspace_id:$workspace,actor:$actor,enabled:true}) '
            'MATCH (w:KDWorkspace {id:m.workspace_id,status:"active"}) '
            'RETURN properties(m) AS m,properties(w) AS w',
            workspace=workspace, actor=identity.actor).single())
        require(row, 'workspace_access_required', '该账号没有此工作空间的访问权限。', 403)
        member, space = dict(row['m']), dict(row['w'])
        role = member.get('role')
        require(role in ROLES and member.get('dataset') == space.get('dataset'),
                'permission_denied', '成员权限或空间绑定配置无效。', 403)
        memberships = self.store.read(lambda tx: [dict(r) for r in tx.run(
            'MATCH (m:KDWorkspaceMember {actor:$actor,enabled:true}) '
            'MATCH (w:KDWorkspace {id:m.workspace_id,status:"active"}) '
            'RETURN w.id AS id,w.dataset AS dataset,w.name AS name,w.kind AS kind,m.role AS role '
            'ORDER BY CASE WHEN w.kind="personal" THEN 0 ELSE 1 END,w.created_at,w.id', actor=identity.actor)])
        return {'tenant': space['id'], 'dataset': space['dataset'], 'actor': identity.actor,
                'permissions': ROLES[role].copy()}, {'workspace': space, 'member': member,
                'role': role, 'workspaces': memberships}

    def session(self, identity, workspace=''):
        from .mobile_speech import speech_available
        context, authorized = self.authorize(identity, workspace)
        space = authorized['workspace']
        current = {'id': space['id'], 'name': space.get('name') or '个人空间',
                   'kind': space.get('kind', 'personal')}
        workspaces = [{'id': item['id'], 'name': item['name'], 'kind': item['kind'],
                       'role': item['role']} for item in authorized['workspaces']]
        return {'user': identity.actor, 'workspace': current, 'workspaces': workspaces,
                'role': authorized['role'], 'permissions': context['permissions'],
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
