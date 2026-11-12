import json
import unittest
from mcp_backend.domain import eligibility, value_key


def assertion(fid, value='100', **fields):
    return {'id': fid, 'subject_id': 'a', 'predicate': 'amount', 'cardinality': 'one',
        'value_json': json.dumps({'type': 'number', 'value': value, 'unit': 'USD'}), **fields}


class EligibilityTests(unittest.TestCase):
    def test_numeric_representation_and_same_value_certainty_are_not_conflicts(self):
        facts = [assertion('precise', '100.000000'), assertion('estimated', certainty='estimated')]
        result = eligibility({'a': {'id': 'a'}}, facts)
        self.assertEqual(result['precise'], [])
        self.assertEqual(result['estimated'], ['uncertain'])
        self.assertEqual(value_key(assertion('zero', '0')), value_key(assertion('zero', '-0.000')))

    def test_uncertain_contradiction_blocks_the_single_value_slot(self):
        facts = [assertion('known'), assertion('different', '200', certainty='estimated')]
        result = eligibility({'a': {'id': 'a'}}, facts)
        self.assertEqual(result['known'], ['conflict'])
        self.assertEqual(set(result['different']), {'conflict', 'uncertain'})

    def test_history_unexecuted_proposal_and_unavailable_targets_are_not_current_conflicts(self):
        facts = [assertion('known'), assertion('history', '200', active=False),
                 assertion('proposal', '300', record_state='action_draft'),
                 {'id': 'relation', 'subject_id': 'a', 'object_id': 'archived', 'predicate': 'target'}]
        result = eligibility({'a': {'id': 'a'}, 'archived': {'archived': True}}, facts)
        self.assertEqual(result['known'], [])
        self.assertIn('historical', result['history'])
        self.assertIn('unexecuted_proposal', result['proposal'])
        self.assertIn('unavailable_entity', result['relation'])

    def test_legacy_pending_and_numeric_imprecision_are_conservative(self):
        facts = [assertion('pending', confirmed=False), assertion('imprecise', numeric_precise=False),
                 assertion('policy2', recording_policy_version=2, review_status='pending')]
        result = eligibility({'a': {'id': 'a'}}, facts)
        self.assertEqual(result['pending'], ['uncertain'])
        self.assertEqual(result['imprecise'], ['uncertain'])
        self.assertEqual(result['policy2'], [])

    def test_many_targets_are_distinct_and_uncertain_identity_is_excluded(self):
        facts = [{'id': str(i), 'subject_id': 'a', 'predicate': 'knows', 'cardinality': 'many',
                  'object_id': target} for i, target in enumerate(['b', 'c', 'b'])]
        objects = {'a': {}, 'b': {}, 'c': {'confirmed': False}}
        result = eligibility(objects, facts)
        self.assertEqual(result['0'], []); self.assertEqual(result['2'], [])
        self.assertEqual(result['1'], ['uncertain_identity'])


if __name__ == '__main__': unittest.main()
