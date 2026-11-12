from dataclasses import replace
import unittest
from mcp_backend.contracts import FindInput,GetInput,SourcesInput,RecordInput
from mcp_backend.ingestion import Ingestion
from mcp_backend.records import Records
from mcp_backend.neo4j_repository import Repository
from mcp_backend.permissions import PERMISSIONS
from mcp_backend.errors import KnowledgeError
from tests_mcp.support import test_settings,workspace,cleanup
from tests_mcp.integration.test_ingestion import sample


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate();cls.records=Records(cls.repo);cls.ingestion=Ingestion(cls.repo)
    @classmethod
    def tearDownClass(cls):cls.repo.close()
    def setUp(self):self.scope=replace(workspace(self.repo),permissions=PERMISSIONS)
    def tearDown(self):cleanup(self.repo,self.scope)

    def test_migration_is_additive_and_idempotent(self):
        saved=self.ingestion.record_information(self.scope,RecordInput.model_validate(sample()))
        def props():return self.repo.transaction(lambda tx:[dict(r['n']) for r in tx.run('MATCH (n {dataset:$d}) RETURN properties(n) AS n ORDER BY n.id',d=self.scope.dataset)])
        before=props();self.repo.migrate();self.repo.migrate();self.assertEqual(props(),before)
        record=self.records.get_record(self.scope,GetInput(entity_id=saved['entity_ids']['a']))
        self.assertEqual(record['object']['version'],1)

    def test_selection_counts_all_resources_beyond_page(self):
        from mcp_backend.contracts import RecordInput
        self.ingestion.record_information(self.scope,RecordInput.model_validate(sample()))
        result=self.records.find_records(self.scope,FindInput(selection={'resource':'workspace'},limit=1))
        self.assertEqual(result['selection_count'],3);self.assertEqual(result['returned_count'],1)
        self.assertEqual(result['resource_counts'],{'entity':1,'fact':1,'source':1});self.assertTrue(result['has_more'])

    def test_history_needs_explicit_scope_even_for_owner(self):
        from mcp_backend.contracts import RecordInput
        saved=self.ingestion.record_information(self.scope,RecordInput.model_validate(sample()))
        limited=replace(self.scope,permissions=frozenset({'read','record'}))
        for method,data in [(self.records.get_record,GetInput(entity_id=saved['entity_ids']['a'],include_history=True)),
                            (self.records.get_sources,SourcesInput(source_id=saved['source_ids']['s'],include_history=True))]:
            with self.assertRaises(KnowledgeError):method(limited,data)
