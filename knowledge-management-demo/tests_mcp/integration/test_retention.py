from concurrent.futures import ThreadPoolExecutor,TimeoutError
from datetime import datetime,timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from mcp_backend.account_state import AccountState
from mcp_backend.contracts import RecordInput
from mcp_backend.errors import KnowledgeError
from mcp_backend.ingestion import Ingestion
from mcp_backend.neo4j_repository import Repository
from mcp_backend.retention import Retention,support_due
from mcp_backend.retention_backup import export_graph,write_backup,read_backup,restore_isolated,graph_fingerprint
from tests_mcp.integration.test_ingestion import sample
from tests_mcp.support import test_settings,workspace,cleanup


class RetentionTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[2]/'.mcp-artifacts';parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent)
        root=Path(self.temp.name).resolve();assert root.is_relative_to(parent.resolve())
        (root/'purge').mkdir();(root/'backups').mkdir()
        self.repo=Repository(test_settings(purge_ledger_dir=str(root/'purge'),purge_ledger_secret='test-secret-'*4))
        AccountState.initialize(self.repo.settings);self.repo.migrate();self.scope=workspace(self.repo)
        Ingestion(self.repo).record_information(self.scope,RecordInput.model_validate(sample()))
        self.store=Retention(self.repo.settings,root/'backups');self.store.initialize();self.now=time.time()

    def tearDown(self):cleanup(self.repo,self.scope);self.repo.close();self.temp.cleanup()
    def deny(self,fn,code):
        with self.assertRaises(KnowledgeError) as caught:fn()
        self.assertEqual(caught.exception.code,code)

    def backup(self,name='fresh.json.gz',age=0):
        snapshot=export_graph(self.repo,[self.scope.dataset],now=self.now-age)
        path=self.store.backup_root/name;write_backup(path,snapshot)
        self.store.register_backup(name,now=self.now);return path,snapshot

    def restore_verify(self,name,snapshot):
        cleanup(self.repo,self.scope)
        self.assertEqual(restore_isolated(self.repo,snapshot)['nodes'],len(snapshot['body']['nodes']))
        return self.store.verify_restored(name,self.repo,now=self.now)

    def test_roundtrip_preserves_sources_facts_receipts_and_relationships(self):
        path,snapshot=self.backup();loaded=read_backup(path);self.assertEqual(snapshot,loaded)
        proof=self.restore_verify(path.name,loaded)
        self.assertEqual(proof['verification'],'isolated-restore-fingerprint')
        after=export_graph(self.repo,[self.scope.dataset])
        self.assertEqual(graph_fingerprint(snapshot['body']),graph_fingerprint(after['body']))
        self.deny(lambda:restore_isolated(self.repo,snapshot),'RESTORE_TARGET_NOT_EMPTY')

    def test_temporal_spatial_and_binary_legacy_properties_restore_without_string_coercion(self):
        self.repo.transaction(lambda tx:tx.run('MATCH (o:KDObject {dataset:$d}) '
            'SET o.moment=datetime("2026-11-01T01:30:00.123456789-04:00[America/New_York]"),'
            'o.day=date("2026-11-26"),o.clock=time("12:13:14.123456789Z"),o.span=duration("P1M2DT3.4S"),'
            'o.location=point({longitude:10.2,latitude:20.3}),o.binary=$b',
            d=self.scope.dataset,b=bytearray(b'legacy-bytes')).consume())
        path,snapshot=self.backup();self.restore_verify(path.name,snapshot)
        after=export_graph(self.repo,[self.scope.dataset])
        self.assertEqual(graph_fingerprint(snapshot['body']),graph_fingerprint(after['body']))

    def test_legacy_directory_is_registered_and_only_due_known_bundle_is_removed(self):
        import os
        name='workspace-migration-20261026';folder=self.store.backup_root.parent/name;folder.mkdir()
        child=folder/'old-backup';child.write_bytes(b'synthetic-expired-copy')
        old=self.now-31*86400;os.utime(child,(old,old));os.utime(folder,(old,old))
        self.store.register_legacy(name,[self.repo.settings.resource_url],self.now)
        self.deny(lambda:self.store.rotate_legacy(now=self.now,apply=True),'BACKUP_SUCCESSOR_REQUIRED')
        fresh,snapshot=self.backup();self.restore_verify(fresh.name,snapshot)
        keep=self.store.backup_root.parent/'unregistered';keep.mkdir()
        self.assertEqual(self.store.rotate_legacy(now=self.now,apply=True)['expired'],[name])
        self.assertFalse(folder.exists());self.assertTrue(keep.exists() and fresh.exists())
        self.deny(lambda:self.store.register_legacy('..',['arbitrary']),'BACKUP_PATH_INVALID')

    def test_legacy_tree_drift_blocks_all_historical_disposal(self):
        import os
        folder=self.store.backup_root.parent/'workspace-migration-20261026';folder.mkdir()
        child=folder/'old-backup';child.write_bytes(b'synthetic-copy')
        old=self.now-31*86400;os.utime(child,(old,old));os.utime(folder,(old,old))
        self.store.register_legacy(folder.name,[self.repo.settings.resource_url],self.now)
        fresh,snapshot=self.backup();self.restore_verify(fresh.name,snapshot)
        child.write_bytes(b'changed-after-inventory')
        self.deny(lambda:self.store.rotate_legacy(now=self.now,apply=True),'BACKUP_CHANGED')
        self.assertTrue(folder.exists())

    def test_expiry_boundary_and_last_good_copy_protection(self):
        old,_=self.backup('old.json.gz',30*86400)
        self.deny(lambda:self.store.rotate(now=self.now,apply=True),'BACKUP_SUCCESSOR_REQUIRED')
        self.assertTrue(old.exists())
        fresh,snapshot=self.backup();self.restore_verify(fresh.name,snapshot)
        self.assertEqual(self.store.rotate(now=self.now-1)['expired'],[])
        self.assertEqual(self.store.rotate(now=self.now,apply=True)['expired'],[old.name])
        self.assertFalse(old.exists());self.assertTrue(fresh.exists())
        self.assertEqual(self.store.rotate(now=self.now,apply=True)['expired'],[])

    def test_disposal_retries_after_unlink_before_completion_commit(self):
        old,_=self.backup('old.json.gz',31*86400);fresh,snapshot=self.backup()
        self.restore_verify(fresh.name,snapshot)
        original=self.store._save
        def fail_after_unlink(body):
            if body['backups'][old.name]['state']=='expired':raise OSError('synthetic interrupted completion')
            return original(body)
        with patch.object(self.store,'_save',side_effect=fail_after_unlink):
            with self.assertRaises(OSError):self.store.rotate(now=self.now,apply=True)
        self.assertFalse(old.exists());self.store.rotate(now=self.now,apply=True)
        self.assertEqual(self.store._read()['backups'][old.name]['state'],'expired')

    def test_backup_hash_or_inventory_tampering_blocks_rotation(self):
        old,_=self.backup('old.json.gz',31*86400);fresh,snapshot=self.backup()
        self.restore_verify(fresh.name,snapshot);old.write_bytes(b'changed')
        self.deny(lambda:self.store.rotate(now=self.now,apply=True),'BACKUP_CHANGED')
        self.assertTrue(fresh.exists())
        data=self.store.path.read_text(encoding='utf-8').replace('available','forged')
        self.store.path.write_text(data,encoding='utf-8')
        self.deny(self.store._read,'RETENTION_INVALID')

    def test_outside_paths_unknown_files_and_unverified_successor_are_not_disposed(self):
        self.deny(lambda:self.store.register_backup('../outside'),'BACKUP_PATH_INVALID')
        old,_=self.backup('old.json.gz',31*86400);fresh,_=self.backup()
        unknown=self.store.backup_root/'not-inventory.txt';unknown.write_text('keep',encoding='utf-8')
        self.deny(lambda:self.store.rotate(now=self.now,apply=True),'BACKUP_SUCCESSOR_REQUIRED')
        self.assertTrue(old.exists() and fresh.exists() and unknown.exists())

    def test_restored_data_mismatch_cannot_be_called_verified(self):
        path,snapshot=self.backup();cleanup(self.repo,self.scope);restore_isolated(self.repo,snapshot)
        self.repo.transaction(lambda tx:tx.run('MATCH (s:KDSource {dataset:$d}) SET s.synthetic_change=true',d=self.scope.dataset).consume())
        self.deny(lambda:self.store.verify_restored(path.name,self.repo),'RESTORE_MISMATCH')

    def test_support_calendar_months_explicit_closed_messages_and_actual_disposal_receipt(self):
        leap=datetime(2024,2,29,12,tzinfo=timezone.utc).timestamp()
        self.assertEqual(datetime.fromtimestamp(support_due(leap),timezone.utc),datetime(2025,2,28,12,tzinfo=timezone.utc))
        result=self.store.close_ticket('verified-closed-case',['message-1','message-2'],self.now-400*86400)
        self.assertEqual(self.store.due_tickets(self.now)[0]['message_ids'],['message-1','message-2'])
        self.deny(lambda:self.store.close_ticket('invalid-open-case',[],self.now),'INVALID_ARGUMENT')
        self.store.complete_ticket(result['case_digest'],'actual-provider-disposal-receipt',self.now)
        self.assertEqual(self.store.due_tickets(self.now),[])
        self.assertNotIn('message_ids',self.store._read()['tickets'][result['case_digest']])

    def test_backup_lock_blocks_writes_until_consistent_capture(self):
        acquired=threading.Event();release=threading.Event();original=self.repo.revision
        def held(tx,dataset,write=False):
            value=original(tx,dataset,write)
            if threading.current_thread().name.startswith('capture') and dataset==self.scope.dataset:
                acquired.set()
                if not release.wait(5):raise RuntimeError('test synchronization timed out')
            return value
        def write():
            return self.repo.scoped(self.scope,lambda tx,r:tx.run('MATCH (d:KDData {id:$d}) SET d.revision=d.revision+1 '
                'RETURN d.revision AS r',d=self.scope.dataset).single()['r'],'record')
        with patch.object(self.repo,'revision',side_effect=held),ThreadPoolExecutor(max_workers=1,thread_name_prefix='capture') as pool,ThreadPoolExecutor(max_workers=1) as writers:
            pending=pool.submit(export_graph,self.repo,[self.scope.dataset]);self.assertTrue(acquired.wait(5))
            writer=writers.submit(write)
            try:
                with self.assertRaises(TimeoutError):writer.result(timeout=0.2)
            finally:release.set()
            snapshot=pending.result(timeout=8);writer.result(timeout=8)
        data=next(n for n in snapshot['body']['nodes'] if 'KDData' in n['labels'])
        self.assertEqual(data['properties']['revision'],1)


if __name__=='__main__':unittest.main()
