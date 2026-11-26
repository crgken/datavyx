import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
from mcp_backend.errors import KnowledgeError
from scripts.mcp_retention import guard,legacy_not_mounted
from tests_mcp.support import test_settings
from scripts.mcp_backup_verify import wait_for_database
from neo4j.exceptions import ServiceUnavailable


class RetentionAdminTests(unittest.TestCase):
    def test_bolt_connectivity_does_not_replace_target_database_readiness(self):
        repo=SimpleNamespace(driver=MagicMock(),settings=SimpleNamespace(database='neo4j'))
        session=repo.driver.session.return_value.__enter__.return_value
        session.run.side_effect=[ServiceUnavailable('synthetic database is still starting'),MagicMock()]
        with patch('scripts.mcp_backup_verify.time.sleep'):wait_for_database(repo,attempts=2)
        self.assertEqual(session.run.call_count,2)
        self.assertEqual(repo.driver.verify_connectivity.call_count,2)

    def test_database_startup_deadline_is_explicit(self):
        repo=SimpleNamespace(driver=MagicMock(),settings=SimpleNamespace(database='neo4j'))
        repo.driver.session.return_value.__enter__.return_value.run.side_effect=ServiceUnavailable('synthetic startup failure')
        with patch('scripts.mcp_backup_verify.time.sleep'),self.assertRaises(KnowledgeError) as error:wait_for_database(repo,attempts=2)
        self.assertEqual(error.exception.code,'VERIFICATION_UNAVAILABLE')

    def test_management_requires_the_exact_root_authorization(self):
        with patch.dict('os.environ',{'ATLASAI_RETENTION_AUTHORIZED_COMMIT':'b'*40}):
            with self.assertRaises(KnowledgeError) as error:guard(test_settings(),'a'*40)
            self.assertEqual(error.exception.code,'PERMISSION_DENIED')

    def test_container_mount_inside_or_above_backup_blocks_historical_disposal(self):
        root=Path(__file__).resolve().parents[2]/'.mcp-artifacts'/'synthetic-backup-root'
        store=SimpleNamespace(_legacy=lambda name:root/name)
        target=root/'workspace-migration-20261026'
        for mount in (target,target/'restore-data',root):
            with self.subTest(mount=str(mount)),patch('scripts.mcp_retention.subprocess.check_output',
                side_effect=['container-1\n',json.dumps([{'Mounts':[{'Source':str(mount)}]}]).encode()]):
                with self.assertRaises(KnowledgeError) as error:legacy_not_mounted(store)
                self.assertEqual(error.exception.code,'LEGACY_BACKUP_IN_USE')
        with patch('scripts.mcp_retention.subprocess.check_output',side_effect=['container-1\n',
            json.dumps([{'Mounts':[{'Source':str(root.parent/'active-database')}]}]).encode()]):
            legacy_not_mounted(store)
