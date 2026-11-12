from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from uuid import uuid4
from mcp_backend.contracts import RecordInput,FindInput,GetInput,ReceiptInput
from mcp_backend.mutation_contracts import PurgeInput,UpdateInput
from mcp_backend.mutations import Mutations
from mcp_backend.ingestion import Ingestion
from mcp_backend.records import Records
from mcp_backend.neo4j_repository import Repository
from mcp_backend.permissions import PERMISSIONS
from mcp_backend.purge_ledger import PurgeLedger
from mcp_backend.erasure import reapply_committed
from mcp_backend.migrations.snapshot import export_snapshot,restore_test_snapshot
from mcp_backend.errors import KnowledgeError
from tests_mcp.support import test_settings,workspace,cleanup
from tests_mcp.integration.test_ingestion import sample
from tests_mcp.integration.test_mutations import instruction
from tests_mcp.integration.test_currency_batch import batch


class ErasureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo=Repository(test_settings());cls.repo.migrate();cls.service=Mutations(cls.repo)
        cls.ingestion=Ingestion(cls.repo);cls.read=Records(cls.repo)
    @classmethod
    def tearDownClass(cls):cls.repo.close()
    def setUp(self):self.scope=replace(workspace(self.repo),permissions=PERMISSIONS);self.ledger=PurgeLedger(self.repo.settings)
    def tearDown(self):cleanup(self.repo,self.scope)
    def save(self,data):return self.ingestion.record_information(self.scope,RecordInput.model_validate(data))
    def purge_body(self,selection):
        found=self.read.find_records(self.scope,FindInput(selection=selection))
        return PurgeInput(**instruction(str(uuid4()),text='Permanently erase the specified records.'),
            selection=selection,expected_revision=found['revision'],expected_count=found['selection_count'],selection_digest=found['selection_digest'])
    def state(self):return self.repo.transaction(lambda tx:[dict(r['n']) for r in tx.run('MATCH (n {dataset:$d}) RETURN properties(n) AS n ORDER BY n.id',d=self.scope.dataset)])

    def test_erases_content_history_and_old_receipts_and_blocks_original_replay(self):
        original=sample();saved=self.save(original);oid=saved['entity_ids']['a']
        change={**instruction('rename-private-test',text='Rename Acme to PrivateBeta.'),'changes':[{
            'action':'set_entity_fields','entity_id':oid,'expected_version':1,'fields':{'name':'PrivateBeta'},
            'quotes':[{'source_ref':'s','quote':'PrivateBeta'}]}]}
        self.service.update_information(self.scope,UpdateInput.model_validate(change))
        data=self.purge_body({'resource':'entity','ids':[oid],'state':'all'})
        result=self.service.purge_information(self.scope,data)
        self.assertEqual(result['removed_counts'],{'entity':1,'fact':1,'source':2})
        text=json.dumps(self.state());self.assertNotIn('Acme',text);self.assertNotIn('PrivateBeta',text)
        files=self.ledger.entries(self.scope.dataset);self.assertEqual(files[0]['state'],'committed')
        self.assertNotIn('PrivateBeta',json.dumps(files))
        receipt=self.ingestion.get_receipt(self.scope,ReceiptInput(request_id=original['request_id']))
        self.assertEqual(receipt['receipt']['status'],'purged')
        with self.assertRaises(KnowledgeError) as error:self.ingestion.record_information(self.scope,RecordInput.model_validate(original))
        self.assertEqual(error.exception.code,'PREVIOUSLY_PURGED')
        self.assertTrue(self.service.purge_information(self.scope,data)['idempotent'])

    def test_shared_source_refuses_without_any_manifest_or_partial_deletion(self):
        saved=self.save(batch());oid=saved['entity_ids']['t004']
        data=self.purge_body({'resource':'entity','ids':[oid],'state':'all'});before=self.state()
        with self.assertRaises(KnowledgeError) as error:self.service.purge_information(self.scope,data)
        self.assertEqual(error.exception.code,'SOURCE_DEPENDENCY');self.assertEqual(self.state(),before)
        self.assertEqual(self.ledger.entries(self.scope.dataset),[])

    def test_full_workspace_erasure_includes_history_and_original_raw_text(self):
        self.save(batch());data=self.purge_body({'resource':'workspace','state':'all'})
        result=self.service.purge_information(self.scope,data)
        self.assertEqual(result['removed_counts'],{'entity':12,'fact':48,'source':1})
        self.assertEqual(self.read.find_records(self.scope,FindInput(selection={'resource':'workspace','state':'all'}))['selection_count'],0)

    def test_restored_old_backup_is_blocked_until_erasure_manifests_are_reapplied(self):
        saved=self.save(sample());backup=export_snapshot(self.repo,self.scope.dataset)
        data=self.purge_body({'resource':'entity','ids':list(saved['entity_ids'].values()),'state':'all'})
        self.service.purge_information(self.scope,data)
        # Deliberate isolated restore simulation; preserve external manifests.
        self.repo.transaction(lambda tx:tx.run('MATCH (n) WHERE n.dataset=$d OR (n:KDData AND n.id=$d) DETACH DELETE n',d=self.scope.dataset).consume())
        restore_test_snapshot(self.repo,backup)
        with self.assertRaises(KnowledgeError) as error:self.read.find_records(self.scope,FindInput())
        self.assertEqual(error.exception.code,'RECOVERY_REQUIRED')
        reapply_committed(self.repo,self.scope.dataset)
        self.assertEqual(self.read.find_records(self.scope,FindInput())['total'],0)
        self.assertNotIn('Acme',json.dumps(self.state()))

    def test_failure_after_durable_intent_rolls_back_and_requires_proven_reconciliation(self):
        saved=self.save(sample());data=self.purge_body({'resource':'entity','ids':list(saved['entity_ids'].values()),'state':'all'})
        before=self.state()
        with patch.object(self.service,'_touch',side_effect=KnowledgeError('INJECTED','Injected test failure.')):
            with self.assertRaises(KnowledgeError):self.service.purge_information(self.scope,data)
        self.assertEqual(self.state(),before)
        with self.assertRaises(KnowledgeError):self.read.find_records(self.scope,FindInput())
        entry=self.ledger.entries(self.scope.dataset)[0]
        with self.assertRaises(KnowledgeError):self.ledger.reconcile_prepared(self.repo,self.scope.dataset)
        self.ledger._write(self.ledger.path(self.scope.dataset,entry['operation_id']),{**entry,'created_at':entry['created_at']-121})
        self.ledger.reconcile_prepared(self.repo,self.scope.dataset)
        self.assertEqual(self.ledger.entries(self.scope.dataset)[0]['state'],'aborted')
        self.assertEqual(self.read.find_records(self.scope,FindInput())['total'],1)

    def test_tampered_manifest_and_missing_purge_scope_fail_closed(self):
        saved=self.save(sample());data=self.purge_body({'resource':'entity','ids':list(saved['entity_ids'].values()),'state':'all'})
        with self.assertRaises(KnowledgeError):self.service.purge_information(replace(self.scope,permissions=frozenset({'read','history'})),data)
        self.service.purge_information(self.scope,data)
        entry=self.ledger.entries(self.scope.dataset)[0];path=self.ledger.path(self.scope.dataset,entry['operation_id'])
        contents=path.read_text(encoding='utf-8');path.write_text(contents.replace('committed','prepared'),encoding='utf-8')
        with self.assertRaises(KnowledgeError) as error:self.read.find_records(self.scope,FindInput())
        self.assertEqual(error.exception.code,'PURGE_LEDGER_INVALID')

    def test_erasing_one_corrected_fact_also_removes_its_audit_payload(self):
        saved=self.save(sample());oid=saved['entity_ids']['a'];fid=saved['fact_ids'][0]
        update={**instruction('fact-private-change',text='Change amount to 65.'),'changes':[{
            'action':'replace_fact','entity_id':oid,'expected_version':1,'predicate':'amount','fact_ids':[fid],
            'quotes':[{'source_ref':'s','quote':'65'}],'replacements':[{'value':{'type':'number','decimal':'65','unit':''},
                'inherit_unit':True,'quotes':[{'source_ref':'s','quote':'65'}]}]}]}
        self.service.update_information(self.scope,UpdateInput.model_validate(update))
        current=self.read.get_record(self.scope,GetInput(entity_id=oid))['facts']['rows'][0]['id']
        result=self.service.purge_information(self.scope,self.purge_body({'resource':'fact','ids':[current],'state':'all'}))
        self.assertEqual(result['removed_counts'],{'entity':0,'fact':1,'source':1})
        self.assertNotIn('Change amount to 65.',json.dumps(self.state()))
        self.assertEqual(self.read.get_record(self.scope,GetInput(entity_id=oid))['facts']['total'],0)

    def test_source_only_erasure_and_unexecuted_historical_canonical_body(self):
        raw=sample();raw.update(recording_mode='source_only',entities=[],facts=[]);saved=self.save(raw)
        data=self.purge_body({'resource':'source','ids':list(saved['source_ids'].values()),'state':'all'})
        self.service.purge_information(self.scope,data)
        self.assertNotIn('Acme',json.dumps(self.state()))
        self.repo.transaction(lambda tx:tx.run('CREATE (:KDObject {dataset:$d,id:$id,kind:"OldProposal",name:"PrivateProposal",record_state:"action_draft"})',
            d=self.scope.dataset,id=uuid4().hex).consume())
        result=self.service.purge_information(self.scope,self.purge_body({'resource':'workspace','state':'all'}))
        self.assertEqual(result['removed_counts']['entity'],1);self.assertNotIn('PrivateProposal',json.dumps(self.state()))
