"""OAuth token verification and current application-owned workspace binding."""
from dataclasses import dataclass
import hashlib
import time
import jwt
from .domain import digest
from .errors import KnowledgeError,require
from .neo4j_repository import Scope


@dataclass(frozen=True)
class Identity:
    actor: str
    issuer: str
    subject: str
    client_id: str
    permissions: frozenset[str]
    expires: int
    token_hash: str


class Authentication:
    def __init__(self,settings,repository,key_resolver=None):
        self.settings,self.repo=settings,repository
        self.jwks=jwt.PyJWKClient(settings.issuer+'.well-known/jwks.json',lifespan=300,timeout=5)
        self.key_resolver=key_resolver or (lambda token:self.jwks.get_signing_key_from_jwt(token).key)

    def protected_resource_metadata(self):
        return {'resource':self.settings.resource_url,'authorization_servers':[self.settings.issuer],
                'scopes_supported':['knowledge:read','knowledge:record'],'bearer_methods_supported':['header']}

    def authenticate(self,authorization):
        require(isinstance(authorization,str) and authorization.startswith('Bearer ') and len(authorization)<=16384,
                'UNAUTHENTICATED','A valid bearer token is required.',401)
        token=authorization[7:]
        try:
            claims=jwt.decode(token,self.key_resolver(token),algorithms=['RS256'],
                issuer=self.settings.issuer,audience=self.settings.audience,leeway=0,
                options={'require':['exp','iat','iss','aud','sub']})
        except jwt.PyJWKClientConnectionError:
            raise KnowledgeError('SERVICE_UNAVAILABLE','Identity verification is temporarily unavailable.',503,True) from None
        except (jwt.PyJWTError,ValueError,TypeError,KeyError):
            raise KnowledgeError('UNAUTHENTICATED','Token verification failed.',401) from None
        client=claims.get('azp',claims.get('client_id',''))
        subject=claims['sub']
        require(client in self.settings.clients and isinstance(subject,str) and 0<len(subject)<=200
                and not subject.endswith('@clients') and type(claims['exp']) is int and type(claims['iat']) is int
                and (not self.settings.organization or claims.get('org_id')==self.settings.organization),
                'UNAUTHENTICATED','Token identity or client is not permitted.',401)
        scopes=claims.get('scope','')
        require(isinstance(scopes,str),'UNAUTHENTICATED','Token scopes are invalid.',401)
        permissions=frozenset(p for p in ('read','record') if 'knowledge:'+p in scopes.split())
        require('read' in permissions,'PERMISSION_DENIED','The token requires knowledge:read.',403)
        return Identity(digest([self.settings.issuer,subject]),self.settings.issuer,subject,client,
                        permissions,claims['exp'],hashlib.sha256(token.encode()).hexdigest())

    def scope(self,identity,workspace_id=None):
        def resolve(tx):
            require(identity.expires>time.time(),'UNAUTHENTICATED','Token expired.',401)
            require(not tx.run('MATCH (r:KDMobileRevokedToken {id:$id}) WHERE r.expires>$now RETURN r.id AS id',
                id=identity.token_hash,now=time.time()).single(),'UNAUTHENTICATED','Token has been revoked.',401)
            selected=workspace_id
            if not selected:
                row=tx.run('MATCH (m:KDWorkspaceMember {actor:$a,enabled:true}) '
                    'MATCH (w:KDWorkspace {id:m.workspace_id,status:"active"}) WHERE w.dataset=m.dataset '
                    'RETURN w.id AS id ORDER BY CASE WHEN w.kind="personal" THEN 0 ELSE 1 END,w.id LIMIT 1',a=identity.actor).single()
                if row:selected=row['id']
                else:
                    # Same deterministic account binding as the existing login,
                    # without importing its model runtime or credential config.
                    selected='personal:'+identity.actor
                    tx.run('MERGE (w:KDWorkspace {id:$id}) ON CREATE SET w.dataset=$id,w.kind="personal",'
                        'w.name="Personal knowledge",w.status="active",w.owner_actor=$a,w.created_at=$now '
                        'MERGE (m:KDWorkspaceMember {id:$member}) ON CREATE SET m.workspace_id=$id,m.dataset=$id,'
                        'm.actor=$a,m.role="owner",m.enabled=true,m.created_at=$now',
                        id=selected,a=identity.actor,member=digest([selected,identity.actor]),now=time.time()).consume()
            row=tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$w,actor:$a,enabled:true}) '
                'MATCH (w:KDWorkspace {id:m.workspace_id,status:"active"}) WHERE m.dataset=w.dataset '
                'RETURN w.dataset AS dataset',w=selected,a=identity.actor).single()
            require(row,'PERMISSION_DENIED','The requested workspace is not available.',403)
            scope=Scope(identity.actor,identity.client_id,selected,row['dataset'],identity.permissions,identity.expires,identity.token_hash)
            self.repo.authorize(tx,scope)
            return scope
        return self.repo.transaction(resolve)
