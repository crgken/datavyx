from copy import deepcopy
import json
import unittest
from mcp_backend.contracts import QueryInput
from mcp_backend.errors import KnowledgeError
from mcp_backend.migrations.legacy import audit, migrate
from mcp_backend.neo4j_repository import Repository
from mcp_backend.query import Queries
from tests_mcp.support import test_settings, workspace, cleanup


class LegacyCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Repository(test_settings()); cls.repo.migrate()

    @classmethod
    def tearDownClass(cls): cls.repo.close()
    def setUp(self): self.scope = workspace(self.repo)
    def tearDown(self): cleanup(self.repo, self.scope)

    def seed(self):
        oid = self.scope.dataset + '-old-object'; fid = self.scope.dataset + '-old-fact'
        def add(tx):
            tx.run('CREATE (:KDObject {id:$o,dataset:$d,kind:"Client",name:"Legacy",identifier:"001",confirmed:true}) '
                'CREATE (f:KDFact {id:$f,dataset:$d,subject_id:$o,predicate:"amount",active:true,value_json:$value}) '
                'CREATE (s:KDSource {id:$s,dataset:$d,message:"Legacy has 1.20 USD.",created_at:1.0}) '
                'CREATE (s)-[:KD_SUPPORTS {quote:"1.20 USD"}]->(f)',
                o=oid, f=fid, s=self.scope.dataset+'-source', d=self.scope.dataset,
                value=json.dumps({'type':'number','value':'1.20','unit':'USD'})).consume()
        self.repo.transaction(add)
        return oid, fid

    def test_empty_space_and_repeat_migration_are_safe(self):
        before = audit(self.repo, self.scope.dataset); self.assertTrue(before['ready'])
        first = migrate(self.repo, self.scope.dataset, before['fingerprint'])
        again = migrate(self.repo, self.scope.dataset, before['fingerprint'])
        self.assertEqual(first['number_backfilled'], 0); self.assertEqual(first, again)

    def test_only_scaled_storage_is_backfilled_and_sources_ids_members_are_unchanged(self):
        oid, fid = self.seed()
        before = audit(self.repo, self.scope.dataset)
        self.assertTrue(before['ready']); self.assertEqual(before['number_backfill_count'], 1)
        applied = migrate(self.repo, self.scope.dataset, before['fingerprint'])
        after = audit(self.repo, self.scope.dataset)
        self.assertEqual(before['preserved_fingerprint'], after['preserved_fingerprint'])
        self.assertEqual(before['source_fingerprint'], after['source_fingerprint'])
        self.assertEqual(before['counts'], after['counts'])
        self.assertEqual(applied['number_backfilled'], 1); self.assertEqual(after['number_backfill_count'], 0)
        self.assertEqual(migrate(self.repo, self.scope.dataset, after['fingerprint'])['number_backfilled'], 0)
        result = Queries(self.repo).query_records(self.scope, QueryInput(operation='SUM', predicate='amount', unit='USD'))
        self.assertEqual(result['sum'], '1.2')

    def test_unexecuted_archived_and_retracted_states_are_never_activated(self):
        self.seed()
        self.repo.transaction(lambda tx: tx.run('CREATE (:KDObject {id:$o,dataset:$d,kind:"Client",confirmed:false,record_state:"action_draft"}) '
            'CREATE (:KDFact {id:$f,dataset:$d,subject_id:$o,predicate:"amount",active:false,record_state:"action_draft",value_json:$v}) '
            'CREATE (:KDObject {id:$a,dataset:$d,kind:"Client",archived:true})',
            d=self.scope.dataset, o=self.scope.dataset+'-pending', f=self.scope.dataset+'-pending-f',
            a=self.scope.dataset+'-archived', v=json.dumps({'type':'text','value':'proposal'})).consume())
        before = audit(self.repo, self.scope.dataset); self.assertTrue(before['ready'])
        migrate(self.repo, self.scope.dataset, before['fingerprint'])
        self.assertEqual(before['states'], audit(self.repo, self.scope.dataset)['states'])
        count = Queries(self.repo).query_records(self.scope, QueryInput(operation='COUNT'))
        self.assertEqual(count['count'], 1)

    def test_stale_or_blocked_migration_changes_nothing(self):
        _, fid = self.seed(); before = audit(self.repo, self.scope.dataset)
        self.repo.transaction(lambda tx: tx.run('MATCH (f:KDFact {id:$id}) SET f.number_scaled=999', id=fid).consume())
        with self.assertRaises(KnowledgeError) as stale: migrate(self.repo, self.scope.dataset, before['fingerprint'])
        self.assertEqual(stale.exception.code, 'STALE_SNAPSHOT')
        current = audit(self.repo, self.scope.dataset); self.assertFalse(current['ready'])
        with self.assertRaises(KnowledgeError) as blocked: migrate(self.repo, self.scope.dataset, current['fingerprint'])
        self.assertEqual(blocked.exception.code, 'MIGRATION_BLOCKED')
        self.assertEqual(current['fingerprint'], audit(self.repo, self.scope.dataset)['fingerprint'])

    def test_unclassified_old_pending_records_require_investigation(self):
        oid, fid = self.seed()
        self.repo.transaction(lambda tx: tx.run('MATCH (o:KDObject {id:$id}) SET o.confirmed=false', id=oid).consume())
        report = audit(self.repo, self.scope.dataset)
        self.assertFalse(report['ready']); self.assertEqual(report['blockers']['unclassified_legacy_identity'], 1)

    def test_mid_migration_failure_rolls_back_additions(self):
        from unittest.mock import patch
        from mcp_backend.migrations import legacy
        self.seed(); before = audit(self.repo, self.scope.dataset)
        original = legacy._inspect
        calls = [0]
        def injected(*args):
            calls[0] += 1
            if calls[0] == 2: raise RuntimeError('injected after backfill')
            return original(*args)
        with patch.object(legacy, '_inspect', side_effect=injected):
            with self.assertRaises(RuntimeError): migrate(self.repo, self.scope.dataset, before['fingerprint'])
        self.assertEqual(before['fingerprint'], audit(self.repo, self.scope.dataset)['fingerprint'])

    def test_logical_test_copy_restore_preserves_graph_and_refuses_overwrite_or_tampering(self):
        from mcp_backend.migrations.snapshot import export_snapshot, restore_test_snapshot
        self.seed(); before = audit(self.repo, self.scope.dataset)
        snapshot = export_snapshot(self.repo, self.scope.dataset)
        with self.assertRaises(KnowledgeError): restore_test_snapshot(self.repo, snapshot)
        damaged = deepcopy(snapshot); damaged['body']['nodes'][0]['properties']['name'] = 'tampered'
        with self.assertRaises(KnowledgeError): restore_test_snapshot(self.repo, damaged)
        cleanup(self.repo, self.scope)
        restore_test_snapshot(self.repo, snapshot)
        self.assertEqual(before['fingerprint'], audit(self.repo, self.scope.dataset)['fingerprint'])


if __name__ == '__main__': unittest.main()
