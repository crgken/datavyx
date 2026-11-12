from dataclasses import replace
from unittest.mock import patch
import unittest
from mcp_backend.contracts import RecordInput,GetInput,FindInput,QueryInput
from mcp_backend.mutation_contracts import UpdateInput,SelectionMutation,RestoreInput
from mcp_backend.mutations import Mutations
from mcp_backend.ingestion import Ingestion
from mcp_backend.records import Records
from mcp_backend.query import Queries
from mcp_backend.permissions import PERMISSIONS
from mcp_backend.purge_ledger import PurgeLedger
from mcp_backend.neo4j_repository import Repository
from mcp_backend.errors import KnowledgeError
from tests_mcp.support import test_settings,workspace,cleanup
from tests_mcp.integration.test_ingestion import sample
from tests_mcp.integration.test_mutations import instruction


class MutationEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate();cls.service=Mutations(cls.repo)
        cls.ingestion=Ingestion(cls.repo);cls.read=Records(cls.repo);cls.query=Queries(cls.repo)
    @classmethod
    def tearDownClass(cls):cls.repo.close()
    def setUp(self):
        self.scope=replace(workspace(self.repo),permissions=PERMISSIONS)
        self.saved=self.save(sample());self.oid=self.saved['entity_ids']['a'];self.fid=self.saved['fact_ids'][0]
    def tearDown(self):cleanup(self.repo,self.scope)
    def save(self,data):return self.ingestion.record_information(self.scope,RecordInput.model_validate(data))
    def update(self,change,text='Change amount to 65.',request='edge-change-001'):
        return self.service.update_information(self.scope,UpdateInput(**instruction(request,text=text),changes=[change]))
    def correction(self,oid=None,fid=None,version=1,predicate='amount',value=None,**replacement):
        return {'action':'replace_fact','entity_id':oid or self.oid,'expected_version':version,'predicate':predicate,
            'fact_ids':[fid or self.fid],'quotes':[{'source_ref':'s','quote':'65'}],
            'replacements':[{'value':value or {'type':'number','decimal':'65','unit':''},
                'inherit_unit':value is None,'quotes':[{'source_ref':'s','quote':'65'}],**replacement}]}
    def delete(self,selection,request):
        matched=self.read.find_records(self.scope,FindInput(selection=selection))
        return self.service.delete_information(self.scope,SelectionMutation(**instruction(request),selection=selection,
            expected_count=matched['selection_count'],expected_revision=matched['revision'],selection_digest=matched['selection_digest']))
    def restore(self,receipt,request):
        revision=self.read.find_records(self.scope,FindInput())['revision']
        return self.service.restore_information(self.scope,RestoreInput(**instruction(request),
            delete_receipt_id=receipt['receipt_id'],expected_revision=revision))

    def test_estimated_correction_stays_out_of_exact_sum_until_asserted(self):
        self.update(self.correction(certainty='estimated'))
        result=self.query.query_records(self.scope,QueryInput(operation='SUM',predicate='amount',unit='USD'))
        self.assertIsNone(result['sum']);self.assertIn('uncertain',[x['reason'] for x in result['exclusions']])
        fact=self.read.get_record(self.scope,GetInput(entity_id=self.oid))['facts']['rows'][0]
        self.update(self.correction(fid=fact['id'],version=2),request='assert-same-amount')
        self.assertEqual(self.query.query_records(self.scope,QueryInput(operation='SUM',predicate='amount',unit='USD'))['sum'],'65')

    def test_equivalent_currency_label_keeps_existing_renminbi_aggregation(self):
        data=sample('renminbi-record-001',identifier='002')
        data['sources'][0]['text']='Acme 002 has 0.10 人民币。'
        data['facts'][0]['quotes']=[{'source_ref':'s','quote':'0.10 人民币'}]
        data['facts'][0]['value'].update(unit='人民币',unit_quotes=[{'source_ref':'s','quote':'人民币'}])
        saved=self.save(data);oid=saved['entity_ids']['a']
        change=self.correction(oid=oid,fid=saved['fact_ids'][0],value={'type':'number','decimal':'65','unit':'CNY',
            'unit_quotes':[{'source_ref':'s','quote':'人民币'}]})
        change['replacements'][0]['quotes']=[{'source_ref':'s','quote':'65 人民币'}]
        self.update(change,text='Change amount to 65 人民币。')
        current=self.read.get_record(self.scope,GetInput(entity_id=oid))['facts']['rows'][0]
        self.assertEqual(current['value']['unit'],'人民币')
        self.assertEqual(self.query.query_records(self.scope,QueryInput(operation='SUM',predicate='amount',unit='人民币'))['sum'],'65')

    def test_boolean_scalar_compatibility_without_coercing_strings(self):
        data={'request_id':'boolean-source-001','sources':[{'ref':'s','text':'Acme is not paid.'}],
            'entities':[{'ref':'a','action':'reference','entity_id':self.oid}],
            'facts':[{'subject_ref':'a','predicate':'paid','value':{'type':'boolean','boolean':False},
                'quotes':[{'source_ref':'s','quote':'Acme is not paid.'}]}]}
        saved=self.save(data)
        change=self.correction(fid=saved['fact_ids'][0],version=2,predicate='paid',value={'type':'boolean','boolean':True})
        change['quotes']=[{'source_ref':'s','quote':'Acme is paid.'}]
        change['replacements'][0]['quotes']=change['quotes']
        self.update(change,text='Acme is paid.')
        record=self.read.get_record(self.scope,GetInput(entity_id=self.oid))
        self.assertIs(next(f for f in record['facts']['rows'] if f['predicate']=='paid')['value']['value'],True)

    def test_deleted_relation_endpoint_requires_dependency_restoration_first(self):
        second=self.save(sample('edge-second-object',identifier='002'));bid=second['entity_ids']['a']
        linked=self.save({'request_id':'edge-link-001','sources':[{'ref':'s','text':'A knows B.'}],
            'entities':[{'ref':'a','action':'reference','entity_id':self.oid},{'ref':'b','action':'reference','entity_id':bid}],
            'facts':[{'subject_ref':'a','predicate':'knows','object_ref':'b','quotes':[{'source_ref':'s','quote':'A knows B.'}]}]})
        relation=self.delete({'resource':'fact','ids':linked['fact_ids']},'edge-delete-relation')
        target=self.delete({'resource':'entity','ids':[bid]},'edge-delete-target')
        with self.assertRaises(KnowledgeError) as error:self.restore(relation,'edge-restore-too-early')
        self.assertEqual(error.exception.code,'RESTORE_DEPENDENCY')
        self.restore(target,'edge-restore-target');self.restore(relation,'edge-restore-relation')
        self.assertEqual(self.read.get_record(self.scope,GetInput(entity_id=self.oid))['relations']['total'],1)

    def test_maintenance_stops_all_new_mutation_writes_but_allows_original_replay(self):
        change=self.correction();body=UpdateInput(**instruction(text='Change amount to 65.'),changes=[change])
        self.service.update_information(self.scope,body)
        restricted=replace(self.repo.settings,mode='production',password='test-only',resource_url='https://mcp.example/mcp',
            purge_ledger_dir=str(PurgeLedger(self.repo.settings).root(self.scope.dataset)),purge_ledger_secret=self.repo.settings.cursor_secret)
        with patch.object(self.repo,'settings',restricted):
            self.assertTrue(self.service.update_information(self.scope,body)['idempotent'])
            with self.assertRaises(KnowledgeError) as error:self.update(self.correction(version=2),request='blocked-new-write')
            self.assertEqual(error.exception.code,'MAINTENANCE')
            self.read.get_record(self.scope,GetInput(entity_id=self.oid))

    def test_editor_scope_cannot_acquire_owner_bulk_or_purge_rights(self):
        self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.role="editor"',d=self.scope.dataset).consume())
        self.update(self.correction())
        with self.assertRaises(KnowledgeError) as error:self.delete({'resource':'workspace'},'editor-cannot-clear-space')
        self.assertEqual(error.exception.code,'PERMISSION_DENIED')
        self.assertNotIn('required_scope',error.exception.details)

    def test_restoring_archived_object_does_not_unarchive_it(self):
        self.repo.transaction(lambda tx:tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) SET o.archived=true',d=self.scope.dataset,id=self.oid).consume())
        deleted=self.delete({'resource':'entity','ids':[self.oid]},'archived-delete-001');self.restore(deleted,'archived-restore-001')
        self.assertEqual(self.read.find_records(self.scope,FindInput())['total'],0)
        self.assertTrue(self.read.get_record(self.scope,GetInput(entity_id=self.oid,include_history=True))['object']['archived'])
