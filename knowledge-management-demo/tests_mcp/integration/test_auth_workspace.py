from dataclasses import replace
import unittest
from mcp_backend.auth import Authentication,Identity
from mcp_backend.errors import KnowledgeError
from mcp_backend.neo4j_repository import Repository
from tests_mcp.support import test_settings,workspace,cleanup


class WorkspaceAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate()
        cls.auth=Authentication(cls.repo.settings,cls.repo)

    @classmethod
    def tearDownClass(cls):cls.repo.close()

    def setUp(self):
        self.scope=workspace(self.repo)
        self.other=workspace(self.repo)
        self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.actor="another-actor"',d=self.other.dataset).consume())
        self.identity=Identity(self.scope.actor,'https://issuer.example/','synthetic','test-client',
                               self.scope.permissions,self.scope.expires,'synthetic-token-hash')

    def tearDown(self):cleanup(self.repo,self.scope,self.other)

    def test_verified_identity_binds_current_workspace(self):
        actual=self.auth.scope(self.identity,self.scope.workspace_id)
        self.assertEqual(actual.dataset,self.scope.dataset)
        self.assertEqual(actual.actor,self.scope.actor)

    def test_explicit_workspace_is_a_selector_not_a_grant(self):
        with self.assertRaises(KnowledgeError):self.auth.scope(self.identity,self.other.workspace_id)

    def test_disabled_membership_and_revoked_tokens_are_rechecked(self):
        self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.enabled=false',d=self.scope.dataset).consume())
        with self.assertRaises(KnowledgeError):self.auth.scope(self.identity,self.scope.workspace_id)
        self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.enabled=true '
            'CREATE (:KDMobileRevokedToken {id:$id,dataset:$d,expires:$expires})',d=self.scope.dataset,
            id=self.identity.token_hash,expires=self.scope.expires).consume())
        with self.assertRaises(KnowledgeError):self.auth.scope(self.identity,self.scope.workspace_id)

    def test_expired_identity_cannot_provision_a_workspace(self):
        with self.assertRaises(KnowledgeError):self.auth.scope(replace(self.identity,expires=1))


if __name__=='__main__':unittest.main()
