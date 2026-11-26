import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from mcp_backend.errors import KnowledgeError
from scripts.mcp_retention import guard,legacy_not_mounted
from tests_mcp.support import test_settings


class RetentionAdminTests(unittest.TestCase):
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
