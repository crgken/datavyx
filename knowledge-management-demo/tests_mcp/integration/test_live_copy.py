from concurrent.futures import ThreadPoolExecutor, TimeoutError
import importlib.util
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
from mcp_backend.migrations.legacy import audit
from mcp_backend.neo4j_repository import Repository
from tests_mcp.support import test_settings, workspace, cleanup


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('mcp_current_export', ROOT/'deploy/mcp/export_current_copy.py')
exporter = importlib.util.module_from_spec(spec); spec.loader.exec_module(exporter)


class CurrentCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.repo = Repository(test_settings()); cls.repo.migrate()
    @classmethod
    def tearDownClass(cls): cls.repo.close()
    def setUp(self): self.scope = workspace(self.repo)
    def tearDown(self): cleanup(self.repo, self.scope)

    def test_export_rolls_back_and_preserves_every_source_property(self):
        before = audit(self.repo, self.scope.dataset)
        metadata = self.repo.transaction(lambda tx: dict(tx.run('MATCH (d:KDData {id:$d}) RETURN properties(d) AS p',
            d=self.scope.dataset).single()['p']))
        result = exporter.export_locked(self.repo, [self.scope.dataset])
        self.assertFalse(result['source_properties_committed'])
        own = next(s for s in result['snapshots'] if s['body']['dataset'] == self.scope.dataset)
        self.assertEqual(own['body']['revision'], 0)
        self.assertEqual(before['fingerprint'], audit(self.repo, self.scope.dataset)['fingerprint'])
        after = self.repo.transaction(lambda tx: dict(tx.run('MATCH (d:KDData {id:$d}) RETURN properties(d) AS p',
            d=self.scope.dataset).single()['p']))
        self.assertEqual(metadata, after)

    def test_canonical_writer_waits_for_rollback_snapshot_boundary(self):
        from mcp_backend.migrations import snapshot
        entered = threading.Event(); release = threading.Event(); writer_started = threading.Event()
        original = snapshot.export_snapshot
        def hold(repo, dataset):
            if dataset == self.scope.dataset:
                entered.set()
                if not release.wait(5): raise RuntimeError('test synchronization timed out')
            return original(repo, dataset)
        def write():
            writer_started.set()
            return self.repo.scoped(self.scope, lambda tx, revision: tx.run(
                'MATCH (d:KDData {id:$d}) SET d.revision=d.revision+1 RETURN d.revision AS r',
                d=self.scope.dataset).single()['r'], 'record')
        with patch.object(snapshot, 'export_snapshot', side_effect=hold), ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(exporter.export_locked, self.repo, [self.scope.dataset])
            self.assertTrue(entered.wait(5))
            writer = pool.submit(write); self.assertTrue(writer_started.wait(2))
            try:
                with self.assertRaises(TimeoutError): writer.result(timeout=0.2)
            finally: release.set()
            result = pending.result(timeout=8)
            self.assertEqual(writer.result(timeout=8), 1)
        own = next(s for s in result['snapshots'] if s['body']['dataset'] == self.scope.dataset)
        self.assertEqual(own['body']['revision'], 0)


if __name__ == '__main__': unittest.main()
