"""Signed self-service users against the isolated Neo4j, never production."""
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from uuid import uuid4
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp_backend.auth import Authentication
from mcp_backend.contracts import FindInput, RecordInput
from mcp_backend.domain import digest
from mcp_backend.errors import KnowledgeError
from mcp_backend.ingestion import Ingestion
from mcp_backend.neo4j_repository import Repository
from mcp_backend.records import Records
from tests_mcp.integration.test_ingestion import sample
from tests_mcp.support import test_settings


class SelfServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings(client_policy='authenticated',clients=()))
        cls.repo.migrate()
        cls.private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.auth=Authentication(cls.repo.settings,cls.repo,key_resolver=lambda token:cls.private.public_key())

    @classmethod
    def tearDownClass(cls):cls.repo.close()

    def setUp(self):
        self.subjects=['auth0|mcp-test-'+uuid4().hex for _ in range(2)]
        self.actors=[digest([self.repo.settings.issuer,s]) for s in self.subjects]

    def tearDown(self):
        assert self.repo.settings.mode=='test' and self.repo.settings.uri=='bolt://127.0.0.1:17694'
        # Only the exact personal IDs derived from this test's random subjects.
        for subject,actor in zip(self.subjects,self.actors):
            assert subject.startswith('auth0|mcp-test-')
            dataset='personal:'+actor
            self.repo.transaction(lambda tx:tx.run('MATCH (n) WHERE n.dataset=$d OR (n:KDData AND n.id=$d) '
                'DETACH DELETE n',d=dataset).consume())

    def identity(self,index=0,client='fresh-dcr-client',scopes='knowledge:read knowledge:record'):
        now=int(time.time())
        token=jwt.encode({'iss':self.repo.settings.issuer,'aud':self.repo.settings.audience,
            'sub':self.subjects[index],'azp':client,'iat':now-1,'exp':now+300,'scope':scopes},
            self.private,algorithm='RS256')
        return self.auth.authenticate('Bearer '+token)

    def test_first_use_creates_distinct_spaces_and_records_are_isolated(self):
        a=self.auth.scope(self.identity())
        b=self.auth.scope(self.identity(1))
        self.assertEqual(a.dataset,'personal:'+self.actors[0])
        self.assertNotEqual(a.dataset,b.dataset)
        Ingestion(self.repo).record_information(a,RecordInput.model_validate(sample()))
        self.assertEqual(len(Records(self.repo).find_records(a,FindInput())['rows']),1)
        self.assertEqual(Records(self.repo).find_records(b,FindInput())['rows'],[])
        with self.assertRaises(KnowledgeError):self.auth.scope(self.identity(1),a.workspace_id)
        row=self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) '
            'RETURN count(m) AS n,collect(m.actor) AS actors,collect(m.role) AS roles',d=a.dataset).single())
        self.assertEqual((row['n'],row['actors'],row['roles']),(1,[a.actor],['owner']))

    def test_same_user_across_clients_reuses_space_without_duplicate_members(self):
        a=self.auth.scope(self.identity(client='client-a'))
        b=self.auth.scope(self.identity(client='client-b'))
        self.assertEqual(a.dataset,b.dataset)
        self.assertNotEqual(a.client_id,b.client_id)
        row=self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {actor:$a}) '
            'RETURN count(m) AS n',a=a.actor).single())
        self.assertEqual(row['n'],1)

    def test_concurrent_first_calls_provision_once(self):
        identity=self.identity()
        with ThreadPoolExecutor(max_workers=4) as pool:
            scopes=list(pool.map(lambda _:self.auth.scope(identity),range(4)))
        self.assertEqual({s.dataset for s in scopes},{'personal:'+identity.actor})
        row=self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {actor:$a}) '
            'RETURN count(m) AS n',a=identity.actor).single())
        self.assertEqual(row['n'],1)

    def test_read_only_user_cannot_write_or_reenable_disabled_membership(self):
        identity=self.identity(scopes='knowledge:read')
        scope=self.auth.scope(identity)
        with self.assertRaises(KnowledgeError):
            Ingestion(self.repo).record_information(scope,RecordInput.model_validate(sample()))
        self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {actor:$a}) '
            'SET m.enabled=false',a=identity.actor).consume())
        with self.assertRaises(KnowledgeError):self.auth.scope(identity)
        row=self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {actor:$a}) '
            'RETURN m.enabled AS enabled',a=identity.actor).single())
        self.assertFalse(row['enabled'])


if __name__=='__main__':unittest.main()
