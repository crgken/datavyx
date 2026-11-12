from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from mcp_backend.contracts import RecordInput,GetInput,QueryInput,SourcesInput,ReceiptInput,OperationReceipt
from mcp_backend.mutation_contracts import UpdateInput
from mcp_backend.mutations import Mutations
from mcp_backend.ingestion import Ingestion
from mcp_backend.records import Records
from mcp_backend.query import Queries
from mcp_backend.neo4j_repository import Repository
from mcp_backend.permissions import PERMISSIONS
from mcp_backend.errors import KnowledgeError
from tests_mcp.support import test_settings,workspace,cleanup
from tests_mcp.integration.test_ingestion import sample


def instruction(request_id='mutation-update-001',text='Change amount to 65.'):
    return {'request_id':request_id,'sources':[{'ref':'s','text':text}],
        'instruction_quotes':[{'source_ref':'s','quote':text}]}


class MutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate()
        cls.service=Mutations(cls.repo);cls.ingestion=Ingestion(cls.repo);cls.read=Records(cls.repo);cls.query=Queries(cls.repo)
    @classmethod
    def tearDownClass(cls):cls.repo.close()
    def setUp(self):
        self.scope=replace(workspace(self.repo),permissions=PERMISSIONS)
        self.original=self.ingestion.record_information(self.scope,RecordInput.model_validate(sample()))
        self.oid=self.original['entity_ids']['a'];self.fid=self.original['fact_ids'][0]
    def tearDown(self):cleanup(self.repo,self.scope)
    def update(self,data,scope=None):return self.service.update_information(scope or self.scope,UpdateInput.model_validate(data))
    def correction(self,request='mutation-update-001',value='65',version=1):
        data=instruction(request,text=f'Change amount to {value}.')
        data['changes']=[{'action':'replace_fact','entity_id':self.oid,'expected_version':version,
            'predicate':'amount','fact_ids':[self.fid],'quotes':[{'source_ref':'s','quote':value}],
            'replacements':[{'value':{'type':'number','decimal':value,'unit':''},'inherit_unit':True,
                'quotes':[{'source_ref':'s','quote':value}]}]}]
        return data
    def total(self):return self.query.query_records(self.scope,QueryInput(operation='SUM',predicate='amount',unit='USD'))['sum']
    def state(self):return self.repo.transaction(lambda tx:[dict(r['n']) for r in tx.run('MATCH (n {dataset:$d}) RETURN properties(n) AS n ORDER BY n.id',d=self.scope.dataset)])

    def test_inherited_unit_accepts_the_exact_observed_label(self):
        data=self.correction();data['changes'][0]['replacements'][0]['value']['unit']='USD'
        receipt=self.update(data)
        self.assertEqual((receipt['objects_changed'],receipt['revision']),(1,2))
        self.assertEqual(self.total(),'65')
        current=self.read.get_record(self.scope,GetInput(entity_id=self.oid))
        self.assertEqual(current['object']['version'],2)
        self.assertEqual(current['facts']['rows'][0]['value']['unit'],'USD')

    def test_inherited_unit_mismatch_rolls_back_without_new_source_or_receipt(self):
        before=self.state();data=self.correction();data['changes'][0]['replacements'][0]['value']['unit']='CNY'
        with self.assertRaises(KnowledgeError) as error:self.update(data)
        self.assertEqual(error.exception.code,'UNIT_INHERITANCE_MISMATCH')
        self.assertEqual(self.state(),before)
        self.assertEqual(self.total(),'0.1')

    def test_correction_replaces_current_value_without_new_entity_or_conflict(self):
        receipt=self.update(self.correction());OperationReceipt.model_validate(receipt)
        self.assertEqual((receipt['objects_changed'],receipt['facts_changed'],receipt['revision']),(1,2,2))
        self.assertEqual(self.total(),'65')
        record=self.read.get_record(self.scope,GetInput(entity_id=self.oid))
        self.assertEqual(record['object']['version'],2);self.assertEqual(record['facts']['total'],1)
        self.assertTrue(record['facts']['rows'][0]['eligible'])
        history=self.read.get_record(self.scope,GetInput(entity_id=self.oid,include_history=True))
        self.assertEqual(history['facts']['total'],2)
        old=self.ingestion.get_receipt(self.scope,ReceiptInput(request_id='test-request-001'))
        self.assertEqual(old['receipt']['current_state'],'changed')
        with self.assertRaises(KnowledgeError):self.read.get_sources(self.scope,SourcesInput(source_id=self.original['source_ids']['s']))
        self.assertIn('0.10 USD',self.read.get_sources(self.scope,SourcesInput(source_id=self.original['source_ids']['s'],include_history=True))['rows'][0]['text'])

    def test_idempotent_retry_uses_original_receipt_and_conflicting_body_fails(self):
        data=self.correction();first=self.update(data);before=self.state();again=self.update(data)
        self.assertTrue(again['idempotent']);self.assertEqual(first['receipt_id'],again['receipt_id']);self.assertEqual(before,self.state())
        with self.assertRaises(KnowledgeError) as error:self.update(self.correction(value='66'))
        self.assertEqual(error.exception.code,'IDEMPOTENCY_CONFLICT')

    def test_noop_does_not_create_source_history_or_revision(self):
        result=self.update(self.correction(value='0.10'))
        self.assertEqual((result['status'],result['revision'],result['sources_changed']),('noop',1,0))
        self.assertFalse(any(n.get('operation_id')==result['receipt_id'] for n in self.state()))

    def test_stale_and_cross_workspace_targets_leave_everything_unchanged(self):
        before=self.state()
        with self.assertRaises(KnowledgeError) as error:self.update(self.correction(version=2))
        self.assertEqual(error.exception.code,'VERSION_CONFLICT');self.assertEqual(before,self.state())
        other=replace(workspace(self.repo),permissions=PERMISSIONS)
        try:
            with self.assertRaises(KnowledgeError):self.update(self.correction(),other)
        finally:cleanup(self.repo,other)

    def test_last_invalid_change_rolls_back_all_history_sources_and_values(self):
        data=self.correction();bad=deepcopy(data['changes'][0]);bad['fact_ids']=['not-in-workspace'];data['changes'].append(bad)
        before=self.state()
        with self.assertRaises(KnowledgeError):self.update(data)
        self.assertEqual(before,self.state());self.assertEqual(self.total(),'0.1')

    def test_complete_slot_can_resolve_explicit_conflict(self):
        other=sample('another-assertion');other['entities']=[{'ref':'a','action':'reference','entity_id':self.oid}]
        other['sources'][0]['text']='Acme has 2 USD.';other['facts'][0]['value']['decimal']='2';other['facts'][0]['quotes'][0]['quote']='2 USD'
        added=self.ingestion.record_information(self.scope,RecordInput.model_validate(other))
        data=self.correction(version=2);data['changes'][0]['action']='replace_slot'
        with self.assertRaises(KnowledgeError) as error:self.update(data)
        self.assertEqual(error.exception.code,'INCOMPLETE_SCOPE')
        data['changes'][0]['fact_ids']+=added['fact_ids'];self.update(data);self.assertEqual(self.total(),'65')

    def test_two_concurrent_updates_cannot_overwrite_each_other(self):
        def attempt(i):
            try:return self.update(self.correction(f'concurrent-update-{i}',str(65+i)))['status']
            except KnowledgeError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,range(2)))
        self.assertEqual(sorted(results),['VERSION_CONFLICT','applied'])

    def test_old_scope_cannot_modify_and_new_identity_remains_unique(self):
        with self.assertRaises(KnowledgeError):self.update(self.correction(),replace(self.scope,permissions=frozenset({'read','record'})))
        data=instruction(text='Rename Acme to Beta.')
        data['changes']=[{'action':'set_entity_fields','entity_id':self.oid,'expected_version':1,
            'fields':{'name':'Beta','identifier':'002'},'quotes':[{'source_ref':'s','quote':'Beta'}]}]
        self.update(data);record=self.read.get_record(self.scope,GetInput(entity_id=self.oid))
        self.assertEqual((record['object']['id'],record['object']['name'],record['object']['identifier']),(self.oid,'Beta','002'))
        with self.assertRaises(KnowledgeError):self.ingestion.record_information(self.scope,RecordInput.model_validate(sample('duplicate-new-id',identifier='002')))

    def test_relation_replacement_preserves_endpoints_and_bumps_local_versions(self):
        second=self.ingestion.record_information(self.scope,RecordInput.model_validate(sample('relation-company-002',identifier='002')))
        third=self.ingestion.record_information(self.scope,RecordInput.model_validate(sample('relation-company-003',identifier='003')))
        bid=second['entity_ids']['a'];cid=third['entity_ids']['a']
        body={'request_id':'relation-record-001','sources':[{'ref':'s','text':'A knows B.'}],
            'entities':[{'ref':'a','action':'reference','entity_id':self.oid},{'ref':'b','action':'reference','entity_id':bid}],
            'facts':[{'subject_ref':'a','predicate':'knows','object_ref':'b','cardinality':'many',
                'quotes':[{'source_ref':'s','quote':'A knows B.'}]}]}
        linked=self.ingestion.record_information(self.scope,RecordInput.model_validate(body))
        data=instruction(text='Replace the relation: A knows C.')
        data['changes']=[{'action':'replace_relation','entity_id':self.oid,'expected_version':2,
            'predicate':'knows','fact_ids':linked['fact_ids'],'quotes':[{'source_ref':'s','quote':'A knows C.'}],
            'replacements':[{'object_id':cid,'quotes':[{'source_ref':'s','quote':'A knows C.'}]}]}]
        receipt=self.update(data);self.assertEqual(receipt['objects_changed'],3)
        a=self.read.get_record(self.scope,GetInput(entity_id=self.oid))
        self.assertEqual(a['relations']['rows'][0]['related_object']['id'],cid)
        b=self.read.get_record(self.scope,GetInput(entity_id=bid))
        self.assertEqual(b['relations']['total'],0);self.assertEqual(b['object']['version'],3)
        self.assertEqual(self.read.get_record(self.scope,GetInput(entity_id=cid))['object']['version'],2)
