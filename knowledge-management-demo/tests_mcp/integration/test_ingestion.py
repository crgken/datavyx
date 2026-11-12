from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch
from mcp_backend.contracts import RecordInput, ReceiptInput, TOOL_OUTPUTS
from mcp_backend.errors import KnowledgeError
from mcp_backend.ingestion import Ingestion
from mcp_backend.neo4j_repository import Repository
from tests_mcp.support import test_settings, workspace, cleanup


def sample(request_id='test-request-001', identifier='001'):
    return {'request_id': request_id, 'sources': [{'ref': 's', 'text': 'Acme 001 has 0.10 USD.'}],
        'entities': [{'ref': 'a', 'action': 'create', 'kind': 'Company', 'name': 'Acme',
            'identifier': identifier, 'namespace': 'CRM', 'quotes': [{'source_ref': 's', 'quote': 'Acme'}]}],
        'facts': [{'subject_ref': 'a', 'predicate': 'amount', 'cardinality': 'one',
            'value': {'type': 'number', 'decimal': '0.10', 'unit': 'USD',
                      'unit_quotes': [{'source_ref': 's', 'quote': 'USD'}]},
            'quotes': [{'source_ref': 's', 'quote': '0.10 USD'}]}]}


class IngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Repository(test_settings()); cls.repo.migrate(); cls.service = Ingestion(cls.repo)

    @classmethod
    def tearDownClass(cls): cls.repo.close()

    def setUp(self): self.scope = workspace(self.repo); self.other = workspace(self.repo)
    def tearDown(self): cleanup(self.repo, self.scope, self.other)

    def save(self, data, scope=None):
        return self.service.record_information(scope or self.scope, RecordInput.model_validate(data))

    def nodes(self):
        return self.repo.transaction(lambda tx: tx.run('MATCH (n {dataset:$d}) '
            'WHERE n:KDObject OR n:KDFact OR n:KDSource OR n:KDMCPRequest '
            'RETURN count(n) AS n', d=self.scope.dataset).single()['n'])

    def reference(self, original, request_id='test-request-002'):
        result = sample(request_id)
        result['entities'] = [{'ref': 'a', 'action': 'reference', 'entity_id': original['entity_ids']['a']}]
        return result

    def test_atomic_save_and_real_receipt(self):
        saved = self.save(sample())
        TOOL_OUTPUTS['record_information'].model_validate(saved)
        self.assertEqual((saved['objects_created'], saved['facts_created'], saved['sources_recorded']), (1, 1, 1))
        self.assertEqual(saved['revision'], 1)
        self.assertEqual(self.nodes(), 4)
        receipt = self.service.get_receipt(self.scope, ReceiptInput(request_id='test-request-001'))
        self.assertEqual(receipt['receipt'], saved)
        properties = self.repo.transaction(lambda tx: dict(tx.run('MATCH (f:KDFact {id:$id}) RETURN properties(f) AS f',
            id=saved['fact_ids'][0]).single()['f']))
        self.assertEqual(properties['number_scaled'], 100000)
        self.assertNotIn('reviewed_by', properties)

    def test_identical_retry_returns_committed_receipt_without_new_nodes(self):
        first = self.save(sample()); again = self.save(sample())
        self.assertTrue(again['idempotent']); self.assertEqual(first['receipt_id'], again['receipt_id'])
        self.assertEqual(self.nodes(), 4); self.assertEqual(again['revision'], 1)
        changed = sample(); changed['sources'][0]['external_ref'] = 'different'
        with self.assertRaises(KnowledgeError) as error: self.save(changed)
        self.assertEqual(error.exception.code, 'IDEMPOTENCY_CONFLICT'); self.assertEqual(self.nodes(), 4)

    def test_concurrent_same_request_commits_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: self.save(sample()), range(4)))
        self.assertEqual(len({r['receipt_id'] for r in receipts}), 1)
        self.assertEqual(sum(not r['idempotent'] for r in receipts), 1)
        self.assertEqual(self.nodes(), 4)

    def test_explicit_identity_competition_never_merges(self):
        def attempt(i):
            try: return self.save(sample(f'identity-race-{i}'))
            except KnowledgeError as error: return error.code
        with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertIn('IDENTITY_CONFLICT', results); self.assertEqual(self.nodes(), 4)

    def test_same_name_without_identifier_creates_separate_objects(self):
        first = self.save(sample(identifier=''))
        second = self.save(sample('test-request-002', identifier=''))
        self.assertNotEqual(first['entity_ids']['a'], second['entity_ids']['a'])

    def test_references_append_sources_without_recounting_identical_fact(self):
        first = self.save(sample()); second = self.save(self.reference(first))
        self.assertEqual(second['objects_created'], 0); self.assertEqual(second['facts_created'], 0)
        self.assertEqual(first['fact_ids'], second['fact_ids'])
        supports = self.repo.transaction(lambda tx: tx.run('MATCH (:KDSource)-[e:KD_SUPPORTS]->(:KDFact {id:$id}) '
            'RETURN count(e) AS n', id=first['fact_ids'][0]).single()['n'])
        self.assertEqual(supports, 2)

    def test_conflicting_and_estimated_statements_are_saved_not_overwritten(self):
        first = self.save(sample()); changed = self.reference(first)
        changed['facts'][0]['value']['decimal'] = '2'
        changed['facts'][0]['certainty'] = 'estimated'
        # Evidence is a received statement, not a server interpretation check.
        changed['sources'][0]['text'] = 'Acme has about 2 USD.'
        changed['facts'][0]['quotes'][0]['quote'] = 'about 2 USD'
        second = self.save(changed)
        self.assertEqual(second['status'], 'recorded'); self.assertEqual(second['conflict_count'], 1)
        self.assertEqual(second['uncertain_count'], 1); self.assertEqual(second['facts_created'], 1)

    def test_same_value_different_certainty_preserves_separate_statements(self):
        first = self.save(sample()); estimated = self.reference(first)
        estimated['facts'][0]['certainty'] = 'estimated'
        second = self.save(estimated)
        self.assertNotEqual(first['fact_ids'], second['fact_ids'])
        self.assertEqual(second['conflict_count'], 0)

    def test_cross_space_reference_and_cardinality_bypass_rollback(self):
        first = self.save(sample())
        with self.assertRaises(KnowledgeError): self.save(self.reference(first), self.other)
        changed = self.reference(first); changed['facts'][0]['cardinality'] = 'many'
        with self.assertRaises(KnowledgeError) as error: self.save(changed)
        self.assertEqual(error.exception.code, 'SCHEMA_CONFLICT'); self.assertEqual(self.nodes(), 4)

    def test_invalid_quote_and_mid_transaction_failure_leave_nothing(self):
        bad = sample(); bad['facts'][0]['quotes'][0]['quote'] = 'not received'
        with self.assertRaises(KnowledgeError): self.save(bad)
        self.assertEqual(self.nodes(), 0)
        with patch.object(Ingestion, '_support', side_effect=RuntimeError('injected database-stage failure')):
            with self.assertRaises(RuntimeError): self.save(sample())
        self.assertEqual(self.nodes(), 0)
        self.assertFalse(self.service.get_receipt(self.scope, ReceiptInput(request_id='test-request-001'))['found'])

    def test_source_only_and_response_loss_recovery(self):
        data = sample(); data.update(recording_mode='source_only', entities=[], facts=[])
        self.save(data)  # Discard the response as if the connection closed after commit.
        recovered = self.service.get_receipt(self.scope, ReceiptInput(request_id=data['request_id']))
        self.assertTrue(recovered['found']); self.assertEqual(recovered['receipt']['objects_created'], 0)
        self.assertEqual(self.nodes(), 2)
        self.assertFalse(self.service.get_receipt(self.other, ReceiptInput(request_id=data['request_id']))['found'])
        self.assertFalse(self.service.get_receipt(replace(self.scope, client_id='another-client'),
            ReceiptInput(request_id=data['request_id']))['found'])

    def test_viewer_cannot_record(self):
        self.repo.transaction(lambda tx: tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.role="viewer"',
            d=self.scope.dataset).consume())
        with self.assertRaises(KnowledgeError): self.save(sample())
        self.assertEqual(self.nodes(), 0)

    def test_production_maintenance_allows_receipts_but_no_new_records(self):
        restricted = replace(self.repo.settings, mode='production', password='unused-test-only',
                             resource_url='https://mcp.example/mcp')
        with patch.object(self.repo, 'settings', restricted):
            with self.assertRaises(KnowledgeError) as blocked: self.save(sample())
            self.assertEqual(blocked.exception.code, 'MAINTENANCE'); self.assertEqual(self.nodes(), 0)
        with patch.object(self.repo, 'settings', replace(restricted, recording_actors=(self.scope.actor,))):
            first = self.save(sample())
        with patch.object(self.repo, 'settings', restricted):
            replay = self.save(sample()); self.assertTrue(replay['idempotent'])
            recovered = self.service.get_receipt(self.scope, ReceiptInput(request_id='test-request-001'))
            self.assertEqual(recovered['receipt']['receipt_id'], first['receipt_id'])
            with self.assertRaises(KnowledgeError): self.save(self.reference(first))
        self.assertEqual(self.nodes(), 4)


if __name__ == '__main__': unittest.main()
