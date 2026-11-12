from dataclasses import replace
import unittest
from mcp_backend.contracts import RecordInput,FindInput,GetInput,QueryInput,ReceiptInput
from mcp_backend.mutation_contracts import SelectionMutation,RestoreInput,UpdateInput,HistoryInput,HistoryResult
from mcp_backend.mutations import Mutations
from mcp_backend.ingestion import Ingestion
from mcp_backend.records import Records
from mcp_backend.query import Queries
from mcp_backend.neo4j_repository import Repository
from mcp_backend.permissions import PERMISSIONS
from mcp_backend.errors import KnowledgeError
from tests_mcp.support import test_settings,workspace,cleanup
from tests_mcp.integration.test_ingestion import sample
from tests_mcp.integration.test_mutations import instruction
from tests_mcp.integration.test_currency_batch import batch,text_filter


class RestorationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate();cls.service=Mutations(cls.repo)
        cls.ingestion=Ingestion(cls.repo);cls.read=Records(cls.repo);cls.query=Queries(cls.repo)
    @classmethod
    def tearDownClass(cls):cls.repo.close()
    def setUp(self):self.scope=replace(workspace(self.repo),permissions=PERMISSIONS)
    def tearDown(self):cleanup(self.repo,self.scope)
    def save(self,data):return self.ingestion.record_information(self.scope,RecordInput.model_validate(data))
    def delete(self,selection,request='restore-delete-001'):
        found=self.read.find_records(self.scope,FindInput(selection=selection))
        data={**instruction(request),'selection':selection,'expected_revision':found['revision'],
            'expected_count':found['selection_count'],'selection_digest':found['selection_digest']}
        return self.service.delete_information(self.scope,SelectionMutation.model_validate(data)),data
    def restoration(self,deleted,request='restoration-test-001'):
        revision=self.read.find_records(self.scope,FindInput())['revision']
        return RestoreInput(**instruction(request,text='Restore the specified deletion.'),
            delete_receipt_id=deleted['receipt_id'],expected_revision=revision)
    def total(self):
        result=self.query.query_records(self.scope,QueryInput(operation='SUM',kind='transaction',predicate='amount',unit='CNY',
            filters=[text_filter('direction','expense')]))
        return result['sum']

    def test_twelve_transaction_edit_delete_restore_truth(self):
        saved=self.save(batch());oid=saved['entity_ids']['t004']
        record=self.read.get_record(self.scope,GetInput(entity_id=oid));fid=next(f['id'] for f in record['facts']['rows'] if f['predicate']=='amount')
        update={**instruction('finance-edit-004',text='Change lunch amount to 65.'),'changes':[{
            'action':'replace_fact','entity_id':oid,'expected_version':1,'predicate':'amount','fact_ids':[fid],
            'quotes':[{'source_ref':'s','quote':'65'}],'replacements':[{'value':{'type':'number','decimal':'65','unit':''},
                'inherit_unit':True,'quotes':[{'source_ref':'s','quote':'65'}]}]}]}
        self.service.update_information(self.scope,UpdateInput.model_validate(update));self.assertEqual(self.total(),'1620')
        deleted,_=self.delete({'resource':'entity','ids':[oid]});self.assertEqual(self.total(),'1555')
        self.assertEqual(self.query.query_records(self.scope,QueryInput(operation='COUNT',kind='transaction'))['count'],11)
        restored=self.service.restore_information(self.scope,self.restoration(deleted));self.assertEqual(restored['objects_changed'],1)
        self.assertEqual(self.total(),'1620');self.assertEqual(self.read.get_record(self.scope,GetInput(entity_id=oid))['object']['version'],4)
        self.assertEqual(self.query.query_records(self.scope,QueryInput(operation='COUNT',kind='transaction'))['count'],12)
        history=self.service.get_history(self.scope,HistoryInput(entity_id=oid))
        HistoryResult.model_validate(history);self.assertEqual(history['total'],3)
        self.assertEqual([r['operation'] for r in history['rows']],['update','delete','restore'])

    def test_restore_does_not_reactivate_separately_deleted_fact(self):
        saved=self.save(sample());oid=saved['entity_ids']['a']
        self.delete({'resource':'fact','ids':saved['fact_ids']})
        deleted,_=self.delete({'resource':'entity','ids':[oid]},'delete-object-later')
        self.service.restore_information(self.scope,self.restoration(deleted))
        self.assertEqual(self.read.get_record(self.scope,GetInput(entity_id=oid))['facts']['total'],0)

    def test_old_delete_retry_after_restore_is_not_a_new_deletion(self):
        saved=self.save(sample());oid=saved['entity_ids']['a'];deleted,body=self.delete({'resource':'entity','ids':[oid]})
        request=self.restoration(deleted);first=self.service.restore_information(self.scope,request)
        self.assertTrue(self.service.restore_information(self.scope,request)['idempotent'])
        replay=self.service.delete_information(self.scope,SelectionMutation.model_validate(body))
        self.assertEqual(replay['current_state'],'restored');self.assertTrue(replay['idempotent'])
        receipt=self.ingestion.get_receipt(self.scope,ReceiptInput(request_id=body['request_id']))
        self.assertEqual(receipt['receipt']['current_state'],'restored')
        self.read.get_record(self.scope,GetInput(entity_id=oid))

    def test_stale_restore_later_deletion_and_history_permissions(self):
        saved=self.save(sample());oid=saved['entity_ids']['a'];deleted,_=self.delete({'resource':'entity','ids':[oid]})
        stale=self.restoration(deleted);self.save(sample('other-finance-object',identifier='002'))
        with self.assertRaises(KnowledgeError) as error:self.service.restore_information(self.scope,stale)
        self.assertEqual(error.exception.code,'STALE_SELECTION')
        self.service.restore_information(self.scope,self.restoration(deleted,'restore-after-refresh'))
        self.delete({'resource':'entity','ids':[oid]},'delete-object-again')
        with self.assertRaises(KnowledgeError) as error:self.service.restore_information(self.scope,self.restoration(deleted,'wrong-deletion-owner'))
        self.assertEqual(error.exception.code,'RESTORE_CONFLICT')
        with self.assertRaises(KnowledgeError):self.service.get_history(replace(self.scope,permissions=frozenset({'read'})),HistoryInput(entity_id=oid))

    def test_history_cursor_and_original_content_survive_without_ordinary_disclosure(self):
        saved=self.save(sample());oid=saved['entity_ids']['a'];deleted,_=self.delete({'resource':'entity','ids':[oid]})
        self.service.restore_information(self.scope,self.restoration(deleted))
        first=self.service.get_history(self.scope,HistoryInput(entity_id=oid,limit=1))
        self.assertTrue(first['has_more'])
        second=self.service.get_history(self.scope,HistoryInput(entity_id=oid,limit=1,cursor=first['next_cursor']))
        self.assertFalse(second['has_more']);self.assertEqual(first['total'],2)
        self.assertEqual(first['rows'][0]['before']['name'],'Acme')
        with self.assertRaises(KnowledgeError):self.service.get_history(self.scope,HistoryInput(source_id=saved['source_ids']['s'],limit=1,cursor=first['next_cursor']))
