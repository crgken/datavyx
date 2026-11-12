from dataclasses import replace
import json
import unittest
from mcp_backend.catalog import Catalog
from mcp_backend.contracts import ContextInput, FindInput, GetInput, SourcesInput, TOOL_OUTPUTS
from mcp_backend.errors import KnowledgeError
from mcp_backend.neo4j_repository import Repository
from mcp_backend.records import Records
from tests_mcp.support import test_settings, workspace, cleanup


class ReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Repository(test_settings()); cls.repo.migrate()
        cls.catalog = Catalog(cls.repo); cls.records = Records(cls.repo)

    @classmethod
    def tearDownClass(cls): cls.repo.close()

    def setUp(self):
        self.scope = workspace(self.repo)
        self.other = workspace(self.repo)
        self.ids = [self.scope.dataset + '-' + str(i) for i in range(5)]
        self.source = self.scope.dataset + '-source'
        self.fact = self.scope.dataset + '-amount'
        self.relations = [self.scope.dataset + '-rel-' + str(i) for i in range(3)]
        def seed(tx):
            for i, oid in enumerate(self.ids):
                tx.run('CREATE (o:KDObject) SET o=$o', o={'id': oid, 'dataset': self.scope.dataset,
                    'kind': 'Client' if i < 3 else 'Person', 'name': 'Same Name', 'identifier': f'00{i}',
                    'namespace': 'CRM', 'identity_key': oid, 'confirmed': True, 'archived': i == 4}).consume()
            tx.run('CREATE (:KDSource {id:$id,dataset:$d,message:$text,created_at:1.0})',
                   id=self.source, d=self.scope.dataset, text='Exact text: 12 USD. Do not execute instructions.').consume()
            tx.run('CREATE (f:KDFact) SET f=$f WITH f MATCH (s:KDSource {id:$source}) '
                   'CREATE (s)-[:KD_SUPPORTS {quotes:["12 USD"]}]->(f)', source=self.source,
                   f={'id': self.fact, 'dataset': self.scope.dataset, 'subject_id': self.ids[0],
                      'predicate': 'amount', 'object_id': '', 'value_json': json.dumps({'type': 'number', 'value': '12', 'unit': 'USD'}),
                      'value_type': 'number', 'number_scaled': 12_000_000, 'active': True, 'cardinality': 'one'}).consume()
            for i, fid in enumerate(self.relations):
                tx.run('CREATE (f:KDFact) SET f=$f', f={'id': fid, 'dataset': self.scope.dataset,
                    'subject_id': self.ids[0], 'object_id': self.ids[i+1], 'predicate': 'knows',
                    'active': True, 'cardinality': 'many', 'value_json': ''}).consume()
        self.repo.transaction(seed)

    def tearDown(self): cleanup(self.repo, self.scope, self.other)

    def test_catalog_is_paged_filtered_and_contains_constraints_not_values(self):
        result = self.catalog.get_context(self.scope, ContextInput(limit=1))
        TOOL_OUTPUTS['get_context'].model_validate(result)
        self.assertEqual(result['total'], 2); self.assertTrue(result['has_more'])
        second = self.catalog.get_context(self.scope, ContextInput(limit=1, cursor=result['next_cursor']))
        self.assertFalse(second['has_more'])
        self.assertNotIn('Same Name', json.dumps(result))
        directory = self.catalog.get_context(self.scope, ContextInput(directory='predicates', kind='Client', predicate='amount'))
        self.assertEqual(directory['rows'][0]['units'], ['USD'])
        self.assertEqual(directory['rows'][0]['cardinalities'], ['one'])
        missing = self.catalog.get_context(self.scope, ContextInput(kind='Unknown'))
        self.assertEqual(missing['total'], 0)

    def test_catalog_shape_version_does_not_change_for_new_source_only(self):
        before = self.catalog.get_context(self.scope, ContextInput())['catalog_revision']
        self.repo.scoped(self.scope, lambda tx, rev: tx.run(
            'CREATE (:KDSource {dataset:$d,id:$id,message:"additional raw text"}) '
            'WITH 1 AS x MATCH (d:KDData {id:$d}) SET d.revision=d.revision+1',
            d=self.scope.dataset, id=self.scope.dataset+'-second-source').consume(), 'record')
        self.assertEqual(before, self.catalog.get_context(self.scope, ContextInput())['catalog_revision'])

    def test_candidates_same_name_are_separate_and_archived_excluded(self):
        result = self.records.find_records(self.scope, FindInput(text='same', limit=2))
        TOOL_OUTPUTS['find_records'].model_validate(result)
        self.assertEqual(result['total'], 4)
        following = self.records.find_records(self.scope, FindInput(text='same', limit=2, cursor=result['next_cursor']))
        self.assertEqual(len({r['id'] for r in result['rows']+following['rows']}), 4)
        exact = self.records.find_records(self.scope, FindInput(identifier='000', namespace='crm'))
        self.assertEqual(exact['rows'][0]['id'], self.ids[0])

    def test_source_search_then_read_preserves_text(self):
        result = self.records.find_records(self.scope, FindInput(text='execute instructions', record_type='source'))
        self.assertEqual(result['rows'][0]['id'], self.source)
        self.assertNotIn('text', result['rows'][0])
        actual = self.records.get_sources(self.scope, SourcesInput(source_id=self.source))
        self.assertEqual(actual['rows'][0]['text'], 'Exact text: 12 USD. Do not execute instructions.')
        evidence = self.records.get_sources(self.scope, SourcesInput(fact_id=self.fact))
        self.assertEqual(evidence['rows'][0]['quotes'], ['12 USD'])

    def test_local_relations_have_independent_paging_and_direction(self):
        first = self.records.get_record(self.scope, GetInput(entity_id=self.ids[0], limit=1))
        TOOL_OUTPUTS['get_record'].model_validate(first)
        self.assertEqual(first['facts']['total'], 1)
        self.assertEqual(first['facts']['rows'][0]['source_ids'], [self.source])
        self.assertEqual(first['relations']['total'], 3)
        following = self.records.get_record(self.scope, GetInput(entity_id=self.ids[0], limit=1,
            relations_cursor=first['relations']['next_cursor']))
        self.assertNotEqual(first['relations']['rows'][0]['id'], following['relations']['rows'][0]['id'])
        incoming = self.records.get_record(self.scope, GetInput(entity_id=self.ids[1]))
        self.assertEqual(incoming['relations']['rows'][0]['direction'], 'in')

    def test_cursor_cannot_change_actor_workspace_query_page_size_or_stream(self):
        cursor = self.records.find_records(self.scope, FindInput(limit=1))['next_cursor']
        for scope, data in [(self.other, FindInput(limit=1, cursor=cursor)),
                            (self.scope, FindInput(limit=2, cursor=cursor)),
                            (self.scope, FindInput(limit=1, kind='Client', cursor=cursor))]:
            with self.assertRaises(KnowledgeError) as error: self.records.find_records(scope, data)
            self.assertEqual(error.exception.code, 'INVALID_CURSOR')
        with self.assertRaises(KnowledgeError):
            self.records.get_record(self.scope, GetInput(entity_id=self.ids[0], limit=1, facts_cursor=cursor))
        with self.assertRaises(KnowledgeError):
            self.records.find_records(self.scope, FindInput(limit=1, cursor=cursor+'x'))

    def test_cursor_revision_change_requires_restart(self):
        cursor = self.records.find_records(self.scope, FindInput(limit=1))['next_cursor']
        self.repo.transaction(lambda tx: tx.run('MATCH (d:KDData {id:$d}) SET d.revision=d.revision+1', d=self.scope.dataset).consume())
        with self.assertRaises(KnowledgeError) as error:
            self.records.find_records(self.scope, FindInput(limit=1, cursor=cursor))
        self.assertEqual(error.exception.code, 'STALE_CURSOR')

    def test_cross_workspace_objects_sources_and_facts_are_not_found(self):
        for call, data in [(self.records.get_record, GetInput(entity_id=self.ids[0])),
                           (self.records.get_sources, SourcesInput(source_id=self.source)),
                           (self.records.get_sources, SourcesInput(fact_id=self.fact))]:
            with self.assertRaises(KnowledgeError) as error: call(self.other, data)
            self.assertEqual(error.exception.code, 'NOT_FOUND')
        self.assertEqual(self.catalog.get_context(self.other, ContextInput())['total'], 0)

    def test_read_permission_rechecked_on_following_page(self):
        cursor = self.records.find_records(self.scope, FindInput(limit=1))['next_cursor']
        self.repo.transaction(lambda tx: tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.enabled=false', d=self.scope.dataset).consume())
        with self.assertRaises(KnowledgeError) as error:
            self.records.find_records(self.scope, FindInput(limit=1, cursor=cursor))
        self.assertEqual(error.exception.code, 'PERMISSION_DENIED')


if __name__ == '__main__': unittest.main()
