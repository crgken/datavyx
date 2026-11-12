from dataclasses import replace
import unittest
from mcp_backend.neo4j_repository import Repository
from mcp_backend.errors import KnowledgeError
from tests_mcp.support import test_settings,workspace,cleanup


class RepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate();cls.repo.migrate()

    @classmethod
    def tearDownClass(cls):cls.repo.close()

    def setUp(self):self.scope=workspace(self.repo)
    def tearDown(self):cleanup(self.repo,self.scope)

    def test_atomic_rollback(self):
        def fail(tx):
            tx.run('CREATE (:KDSource {dataset:$d,id:$id,message:"synthetic"})',d=self.scope.dataset,id=self.scope.dataset+'-source').consume()
            raise RuntimeError('injected test failure')
        with self.assertRaises(RuntimeError):self.repo.transaction(fail)
        n=self.repo.transaction(lambda tx:tx.run('MATCH (s:KDSource {dataset:$d}) RETURN count(s) AS n',d=self.scope.dataset).single()['n'])
        self.assertEqual(n,0)

    def test_current_role_and_workspace_are_checked(self):
        self.assertEqual(self.repo.scoped(self.scope,lambda tx,revision:revision),0)
        with self.assertRaises(KnowledgeError):
            self.repo.scoped(replace(self.scope,dataset='another-space'),lambda tx,revision:revision)
        self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.role="viewer"',d=self.scope.dataset).consume())
        with self.assertRaises(KnowledgeError):self.repo.scoped(self.scope,lambda tx,r:r,permission='record')

    def test_request_identity_cannot_be_expired(self):
        with self.assertRaises(KnowledgeError):self.repo.scoped(replace(self.scope,expires=1),lambda tx,r:r)

    def test_unique_request_constraints_are_real(self):
        def run(tx):
            tx.run('CREATE (:KDMCPRequest {id:$id,dataset:$d}) CREATE (:KDMCPRequest {id:$id,dataset:$d})',id=self.scope.dataset,d=self.scope.dataset).consume()
        with self.assertRaises(KnowledgeError):self.repo.transaction(run)


if __name__=='__main__':unittest.main()
