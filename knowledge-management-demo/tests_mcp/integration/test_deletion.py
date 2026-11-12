from dataclasses import replace
from copy import deepcopy
import unittest
from mcp_backend.contracts import RecordInput,FindInput,GetInput,SourcesInput,QueryInput,ReceiptInput
from mcp_backend.mutation_contracts import SelectionMutation
from mcp_backend.mutations import Mutations
from mcp_backend.ingestion import Ingestion
from mcp_backend.records import Records
from mcp_backend.query import Queries
from mcp_backend.catalog import Catalog
from mcp_backend.neo4j_repository import Repository
from mcp_backend.permissions import PERMISSIONS
from mcp_backend.errors import KnowledgeError
from tests_mcp.support import test_settings,workspace,cleanup
from tests_mcp.integration.test_ingestion import sample
from tests_mcp.integration.test_mutations import instruction
from tests_mcp.integration.test_currency_batch import batch


class DeletionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate();cls.service=Mutations(cls.repo)
        cls.ingestion=Ingestion(cls.repo);cls.read=Records(cls.repo);cls.query=Queries(cls.repo)
    @classmethod
    def tearDownClass(cls):cls.repo.close()
    def setUp(self):self.scope=replace(workspace(self.repo),permissions=PERMISSIONS)
    def tearDown(self):cleanup(self.repo,self.scope)
    def save(self,data):return self.ingestion.record_information(self.scope,RecordInput.model_validate(data))
    def selected(self,selection,request='delete-test-001'):
        found=self.read.find_records(self.scope,FindInput(selection=selection,limit=1))
        return {**instruction(request,text='Delete the specified records.'),'selection':selection,
            'expected_revision':found['revision'],'expected_count':found['selection_count'],
            'selection_digest':found['selection_digest']}
    def delete(self,data,scope=None):return self.service.delete_information(scope or self.scope,SelectionMutation.model_validate(data))
    def count(self,kind=''):return self.query.query_records(self.scope,QueryInput(operation='COUNT',kind=kind))['count']

    def test_object_disappears_from_all_current_paths_and_old_replay_does_not_restore(self):
        original=sample();saved=self.save(original);oid=saved['entity_ids']['a'];source=saved['source_ids']['s']
        data=self.selected({'resource':'entity','ids':[oid]});receipt=self.delete(data)
        self.assertEqual((receipt['objects_changed'],receipt['facts_changed'],receipt['sources_changed']),(1,1,1))
        self.assertEqual(self.count(),0);self.assertEqual(self.read.find_records(self.scope,FindInput(text='Acme'))['total'],0)
        self.assertEqual(self.read.find_records(self.scope,FindInput(record_type='source',text='Acme'))['total'],0)
        with self.assertRaises(KnowledgeError):self.read.get_record(self.scope,GetInput(entity_id=oid))
        with self.assertRaises(KnowledgeError):self.read.get_sources(self.scope,SourcesInput(source_id=source))
        again=self.ingestion.record_information(self.scope,RecordInput.model_validate(original))
        self.assertTrue(again['idempotent']);self.assertEqual(again['current_state'],'deleted');self.assertEqual(self.count(),0)
        self.assertEqual(again['entity_ids'],{});self.assertEqual(again['source_ids'],{})
        historical=self.ingestion.get_receipt(self.scope,ReceiptInput(request_id=original['request_id'],include_history=True))
        self.assertEqual(historical['receipt']['entity_ids'],saved['entity_ids'])
        self.assertTrue(self.delete(data)['idempotent'])

    def test_batch_is_complete_and_wrong_count_rolls_back_all_twelve(self):
        self.save(batch());data=self.selected({'resource':'entity','kind':'transaction'})
        self.assertEqual(data['expected_count'],12)
        bad=deepcopy(data);bad['expected_count']=1
        with self.assertRaises(KnowledgeError) as error:self.delete(bad)
        self.assertEqual(error.exception.code,'INCOMPLETE_SCOPE');self.assertEqual(self.count(),12)
        receipt=self.delete(data)
        self.assertEqual((receipt['matched_count'],receipt['applied_count'],receipt['objects_changed'],receipt['facts_changed']),(12,12,12,48))
        self.assertEqual(self.count(),0)

    def test_stale_selection_and_missing_scopes_cannot_delete(self):
        saved=self.save(sample());data=self.selected({'resource':'entity','ids':list(saved['entity_ids'].values())})
        self.save(sample('another-object-001',identifier='002'))
        with self.assertRaises(KnowledgeError) as error:self.delete(data)
        self.assertEqual(error.exception.code,'STALE_SELECTION');self.assertEqual(self.count(),2)
        data=self.selected({'resource':'entity','kind':'Company'})
        with self.assertRaises(KnowledgeError):self.delete(data,replace(self.scope,permissions=frozenset({'read','delete'})))
        self.assertEqual(self.count(),2)

    def test_fact_deletion_keeps_entity_and_cannot_leak_original_source(self):
        saved=self.save(sample());data=self.selected({'resource':'fact','ids':saved['fact_ids']})
        result=self.delete(data);self.assertEqual(result['facts_changed'],1);self.assertEqual(self.count(),1)
        record=self.read.get_record(self.scope,GetInput(entity_id=saved['entity_ids']['a']))
        self.assertEqual(record['facts']['total'],0)
        with self.assertRaises(KnowledgeError):self.read.get_sources(self.scope,SourcesInput(fact_id=saved['fact_ids'][0]))

    def test_source_only_can_be_deleted_but_last_shared_evidence_is_protected(self):
        raw=sample();raw.update(recording_mode='source_only',entities=[],facts=[])
        saved=self.save(raw);self.delete(self.selected({'resource':'source','ids':list(saved['source_ids'].values())}))
        receipt=self.ingestion.get_receipt(self.scope,ReceiptInput(request_id=raw['request_id']))
        self.assertEqual(receipt['receipt']['current_state'],'deleted')
        structured=self.save(sample('structured-evidence-001'))
        with self.assertRaises(KnowledgeError) as error:self.delete(self.selected({'resource':'source','ids':list(structured['source_ids'].values())},'delete-structured-source'))
        self.assertEqual(error.exception.code,'SOURCE_DEPENDENCY');self.assertEqual(self.count(),1)

    def test_workspace_clear_includes_archive_and_raw_sources_and_empty_range_is_noop(self):
        saved=self.save(sample());oid=saved['entity_ids']['a']
        self.repo.transaction(lambda tx:tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) SET o.archived=true',d=self.scope.dataset,id=oid).consume())
        raw=sample('raw-source-001');raw.update(recording_mode='source_only',entities=[],facts=[]);self.save(raw)
        data=self.selected({'resource':'workspace'});self.assertEqual(data['expected_count'],4)
        self.delete(data)
        result=self.read.find_records(self.scope,FindInput(selection={'resource':'workspace'}))
        self.assertEqual(result['selection_count'],0)
        empty=self.delete(self.selected({'resource':'workspace'},'empty-clear-001'))
        self.assertEqual((empty['status'],empty['applied_count'],empty['revision']),('noop',0,result['revision']))
