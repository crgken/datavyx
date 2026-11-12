from decimal import Decimal
import json
import unittest
from mcp_backend.contracts import QueryInput, TOOL_OUTPUTS
from mcp_backend.errors import KnowledgeError
from mcp_backend.neo4j_repository import Repository
from mcp_backend.query import Queries
from tests_mcp.support import test_settings, workspace, cleanup


class QueryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Repository(test_settings()); cls.repo.migrate(); cls.queries = Queries(cls.repo)

    @classmethod
    def tearDownClass(cls): cls.repo.close()

    def setUp(self):
        self.scope = workspace(self.repo); self.other = workspace(self.repo)
        self.ids = {letter: self.scope.dataset + '-' + letter for letter in 'ABCDEFGHIJ'}
        def seed(tx):
            for letter, oid in self.ids.items():
                tx.run('CREATE (o:KDObject) SET o=$o', o={'id': oid, 'dataset': self.scope.dataset,
                    'kind': 'Client', 'name': letter, 'confirmed': letter != 'H', 'archived': letter == 'G'}).consume()
            values = [('A', '0.1', 'USD', 'asserted', True), ('A', '0.100000', 'USD', 'asserted', True),
                ('B', '0.2', 'USD', 'asserted', True), ('D', '10', 'USD', 'estimated', True),
                ('E', '1', 'USD', 'asserted', True), ('E', '2', 'USD', 'asserted', True),
                ('F', '12', 'EUR', 'asserted', True), ('G', '50', 'USD', 'asserted', True),
                ('H', '1', 'USD', 'asserted', True), ('I', '3', 'USD', 'asserted', True),
                ('J', '3', 'USD', 'asserted', True), ('J', '3', 'USD', 'estimated', True),
                ('B', '500', 'USD', 'asserted', False)]
            for i, (letter, amount, unit, certain, active) in enumerate(values):
                props = {'id': self.scope.dataset + '-f' + str(i), 'dataset': self.scope.dataset,
                    'subject_id': self.ids[letter], 'predicate': 'amount', 'object_id': '',
                    'value_json': json.dumps({'type': 'number', 'value': amount, 'unit': unit}),
                    'number_scaled': int(Decimal(amount) * 1_000_000), 'certainty': certain,
                    'active': active, 'cardinality': 'one'}
                if letter == 'I': props.pop('number_scaled')
                tx.run('CREATE (f:KDFact) SET f=$f', f=props).consume()
            for i, (subject, target) in enumerate([('A', 'B'), ('A', 'C'), ('A', 'B')]):
                tx.run('CREATE (f:KDFact) SET f=$f', f={'id': self.scope.dataset + '-r' + str(i),
                    'dataset': self.scope.dataset, 'subject_id': self.ids[subject], 'object_id': self.ids[target],
                    'predicate': 'knows', 'cardinality': 'many', 'active': True}).consume()
        self.repo.transaction(seed)

    def tearDown(self): cleanup(self.repo, self.scope, self.other)

    def query(self, **kwargs):
        return self.queries.query_records(self.scope, QueryInput(**kwargs))

    def test_count_full_population_not_first_page_and_conflicts_do_not_remove_objects(self):
        result = self.query(operation='COUNT', kind='Client', limit=2)
        TOOL_OUTPUTS['query_records'].model_validate(result)
        self.assertEqual(result['population_count'], 9)
        self.assertEqual(result['count'], 8)  # Excludes archived G and uncertain identity H.
        self.assertEqual(result['returned_count'], 2); self.assertTrue(result['has_more'])
        self.assertTrue(result['aggregate_complete'])
        self.assertIn(self.ids['E'], [r['id'] for r in self.query(operation='LIST')['rows']])

    def test_sum_is_exact_complete_deduplicated_and_explains_exclusions(self):
        result = self.query(operation='SUM', predicate='amount', unit='USD', limit=1)
        self.assertEqual(result['sum'], '3.3')  # A 0.1 + B 0.2 + J 3; not duplicated A or estimated J.
        self.assertEqual(result['participating_count'], 3)
        self.assertEqual(result['excluded_count'], 6)
        reasons = {r['reason']: r['count'] for r in result['exclusions']}
        for reason in ['conflict', 'uncertain', 'unit_mismatch', 'missing_value', 'numeric_unavailable', 'uncertain_identity']:
            self.assertIn(reason, reasons)
        self.assertTrue(result['has_more']); self.assertTrue(result['aggregate_complete'])
        following = self.query(operation='SUM', predicate='amount', unit='USD', limit=1, cursor=result['next_cursor'])
        self.assertEqual(following['sum'], '3.3')

    def test_explicit_other_unit_and_empty_sum(self):
        self.assertEqual(self.query(operation='SUM', predicate='amount', unit='EUR')['sum'], '12')
        empty = self.query(operation='SUM', predicate='amount', unit='JPY')
        self.assertIsNone(empty['sum']); self.assertEqual(empty['participating_count'], 0)
        absent = self.query(operation='COUNT', kind='Does not exist')
        self.assertEqual(absent['count'], 0)

    def test_group_uses_numeric_value_unit_and_distinct_entities(self):
        groups = self.query(operation='GROUP', predicate='amount')['rows']
        counts = {(r['value']['value'], r['value'].get('unit')): r['count'] for r in groups}
        # 0.1 and 0.100000 are one canonical group, even for historical data.
        self.assertEqual(counts[('0.1', 'USD')], 1)
        self.assertNotIn(('1', 'USD'), counts)
        self.assertEqual(counts[('3', 'USD')], 2)  # I can GROUP by exact literal without scaled storage.

    def test_relation_directions_and_duplicate_targets_do_not_recount(self):
        incoming = self.query(operation='COUNT', filters=[{'predicate': 'knows', 'operator': 'eq',
            'object_id': self.ids['A'], 'direction': 'in'}])
        self.assertEqual(incoming['count'], 2)
        outgoing = self.query(operation='COUNT', filters=[{'predicate': 'knows', 'operator': 'eq',
            'object_id': self.ids['B']}])
        self.assertEqual(outgoing['count'], 1)
        groups = self.query(operation='GROUP', predicate='knows')['rows']
        self.assertEqual(sorted(g['count'] for g in groups), [1, 1])

    def test_numeric_filter_does_not_match_conflict_or_estimate(self):
        result = self.query(operation='COUNT', filters=[{'predicate': 'amount', 'operator': 'gte',
            'value': {'type': 'number', 'decimal': '0.15', 'unit': 'USD'}}])
        self.assertEqual(result['count'], 3)  # B, I and J.

    def test_query_ids_are_scoped_and_aggregation_cursor_is_bound(self):
        data = QueryInput(operation='COUNT', object_ids=[self.ids['A']])
        self.assertEqual(self.queries.query_records(self.other, data)['count'], 0)
        first = self.query(operation='COUNT', limit=1)
        with self.assertRaises(KnowledgeError):
            self.query(operation='SUM', predicate='amount', unit='USD', limit=1, cursor=first['next_cursor'])

    def test_numeric_storage_mismatch_is_excluded(self):
        self.repo.transaction(lambda tx: tx.run('MATCH (f:KDFact {dataset:$d,subject_id:$id}) SET f.number_scaled=999',
            d=self.scope.dataset, id=self.ids['B']).consume())
        result = self.query(operation='SUM', predicate='amount', unit='USD')
        self.assertEqual(result['sum'], '3.1')
        self.assertEqual(next(r['count'] for r in result['exclusions'] if r['reason'] == 'numeric_unavailable'), 2)

    def test_sum_refuses_int64_overflow_before_database_addition(self):
        oid = self.scope.dataset + '-overflow'
        values = [{'id': self.scope.dataset+'-overflow-'+str(i), 'dataset': self.scope.dataset,
                   'subject_id': oid, 'predicate': 'amount', 'cardinality': 'many', 'active': True,
                   'number_scaled': 1_000_000_000_000_000-i, 'value_json': json.dumps({'type': 'number',
                       'value': format(Decimal(1_000_000_000_000_000-i)/1_000_000, 'f'), 'unit': 'USD'})}
                  for i in range(9300)]
        self.repo.transaction(lambda tx: tx.run('CREATE (:KDObject {id:$id,dataset:$d,kind:"Overflow"}) '
            'WITH 1 AS x UNWIND $values AS props CREATE (f:KDFact) SET f=props',
            id=oid, d=self.scope.dataset, values=values).consume())
        with self.assertRaises(KnowledgeError) as error:
            self.query(operation='SUM', kind='Overflow', predicate='amount', unit='USD')
        self.assertEqual(error.exception.code, 'NUMERIC_OVERFLOW')


if __name__ == '__main__': unittest.main()
