"""Explicit bounded Neo4j transactions, independent from the old Store."""
from dataclasses import dataclass
import time
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError,ServiceUnavailable,SessionExpired
from .errors import KnowledgeError,require


@dataclass(frozen=True)
class Scope:
    actor: str
    client_id: str
    workspace_id: str
    dataset: str
    permissions: frozenset[str]
    expires: int
    token_hash: str = ''


class Repository:
    def __init__(self,settings):
        self.settings=settings
        self.driver=GraphDatabase.driver(settings.uri,auth=(settings.user,settings.password),
            connection_timeout=5,max_connection_pool_size=8,notifications_min_severity='OFF')

    def close(self):self.driver.close()

    def transaction(self,fn):
        try:
            with self.driver.session(database=self.settings.database) as session:
                with session.begin_transaction(timeout=20) as tx:
                    result=fn(tx)
                    tx.commit()
                    return result
        except KnowledgeError:raise
        except (Neo4jError,ServiceUnavailable,SessionExpired):
            raise KnowledgeError('SERVICE_UNAVAILABLE','The database operation did not complete.',503,True) from None

    def migrate(self):
        # DDL is explicit, never run while importing a package or on tool calls.
        with self.driver.session(database=self.settings.database) as session:
            for label in ('KDObject','KDFact','KDSource','KDData','KDWorkspace','KDWorkspaceMember','KDMCPRequest','KDMCPPredicate'):
                session.run(f'CREATE CONSTRAINT mcp_{label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE').consume()
            session.run('CREATE CONSTRAINT mcp_object_identity IF NOT EXISTS FOR (o:KDObject) REQUIRE (o.dataset,o.identity_key) IS UNIQUE').consume()
            session.run('CREATE INDEX mcp_fact_subject IF NOT EXISTS FOR (f:KDFact) ON (f.dataset,f.subject_id,f.predicate)').consume()
            session.run("MERGE (s:KDMCPSchema {id:'core'}) SET s.version=1").consume()

    @staticmethod
    def authorize(tx,scope,permission='read'):
        require(scope.expires>time.time(),'UNAUTHENTICATED','Authentication expired.',401)
        require(permission in scope.permissions,'PERMISSION_DENIED','The token does not grant this operation.',403)
        row=tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$w,actor:$a,enabled:true}) '
            'MATCH (w:KDWorkspace {id:m.workspace_id,status:"active"}) '
            'RETURN m.role AS role,m.dataset AS dataset,w.dataset AS bound',w=scope.workspace_id,a=scope.actor).single()
        roles={'viewer':{'read'},'editor':{'read','record'},'reviewer':{'read','record'},'owner':{'read','record'}}
        require(row and row['dataset']==scope.dataset==row['bound'] and permission in roles.get(row['role'],set()),
                'PERMISSION_DENIED','The workspace does not permit this operation.',403)
        revoked=tx.run('MATCH (r:KDMobileRevokedToken {id:$id}) WHERE r.expires>$now RETURN r.id AS id',
                       id=scope.token_hash,now=time.time()).single()
        require(not revoked,'UNAUTHENTICATED','Authentication has been revoked.',401)

    @staticmethod
    def revision(tx,dataset,write=False):
        if write:
            return tx.run('MERGE (d:KDData {id:$d}) ON CREATE SET d.revision=0,d.mcp_catalog_revision=0 '
                'SET d.mcp_lock=true RETURN coalesce(d.revision,0) AS revision',d=dataset).single()['revision']
        row=tx.run('MATCH (d:KDData {id:$d}) RETURN coalesce(d.revision,0) AS revision',d=dataset).single()
        return row['revision'] if row else 0

    def scoped(self,scope,fn,permission='read'):
        def run(tx):
            self.authorize(tx,scope,permission)
            revision=self.revision(tx,scope.dataset,write=True)
            return fn(tx,revision)
        return self.transaction(run)

    def snapshot(self,tx,scope):
        limit=self.settings.max_records+1
        objects=[dict(r['o']) for r in tx.run("MATCH (o:KDObject {dataset:$d}) WHERE coalesce(o.record_state,'active')<>'action_draft' RETURN properties(o) AS o ORDER BY o.id LIMIT $n",d=scope.dataset,n=limit)]
        facts=[dict(r['f']) for r in tx.run("MATCH (f:KDFact {dataset:$d}) WHERE coalesce(f.record_state,'active')<>'action_draft' RETURN properties(f) AS f ORDER BY f.id LIMIT $n",d=scope.dataset,n=limit)]
        require(len(objects)<limit and len(facts)<limit,'LIMIT_EXCEEDED','The workspace exceeds this query resource limit.',413)
        return {o['id']:o for o in objects},facts
