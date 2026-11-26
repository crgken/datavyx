from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from mcp_backend.account_state import AccountState
from mcp_backend.domain import digest
from mcp_backend.errors import KnowledgeError
from tests_mcp.support import test_settings


class AccountStateTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[2]/'.mcp-artifacts';parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent)
        root=Path(self.temp.name).resolve();assert root.is_relative_to(parent.resolve())
        (root/'purge').mkdir()
        self.settings=test_settings(purge_ledger_dir=str(root/'purge'))
        AccountState.initialize(self.settings);self.state=AccountState(self.settings)
        self.actor=digest(uuid4().hex)
    def tearDown(self):self.temp.cleanup()

    def test_signature_idempotency_and_fixed_deadlines(self):
        with self.state.locked():first=self.state.start(self.actor,'verified-case-123',1000)
        again=self.state.start(self.actor,'verified-case-123',5000)
        self.assertEqual(first,again)
        self.assertEqual(first['active_data_due'],1000+30*86400)
        self.assertEqual(first['backup_data_due'],1000+60*86400)
        self.assertNotIn('verified-case-123',self.state.path(self.actor).read_text())
        with self.assertRaises(KnowledgeError):self.state.start(self.actor,'different-case')

    def test_tampering_or_wrong_identity_fails_closed(self):
        body=self.state.start(self.actor,'verified-case-123')
        path=self.state.path(self.actor);value=json.loads(path.read_text())
        value['body']['profiles']={'unauthorized':{'state':'erased'}}
        path.write_text(json.dumps(value))
        with self.assertRaises(KnowledgeError) as error:self.state.load(path)
        self.assertEqual(error.exception.code,'ACCOUNT_STATE_INVALID')
        self.state.write(body)
        wrong=self.state.path(digest('another-actor'));wrong.write_bytes(path.read_bytes())
        with self.assertRaises(KnowledgeError):self.state.load(wrong)

    def test_missing_production_directory_or_key_never_reinitializes(self):
        production=replace(self.settings,mode='production',resource_url=self.settings.audience,
            password='synthetic-test-password',purge_ledger_secret='test-ledger-secret-'*3)
        key=self.state.root()/'signing-key';key.unlink()
        with self.assertRaises(KnowledgeError):AccountState(production).root()
        self.assertFalse(key.exists())

    def test_invalid_actor_case_and_duplicate_initialization_rejected(self):
        with self.assertRaises(KnowledgeError):AccountState.initialize(self.settings)
        for actor,case in [('user@example.com','verified-case-123'),(self.actor,'short')]:
            with self.assertRaises(KnowledgeError):self.state.start(actor,case)

    def test_https_candidate_cannot_use_the_local_test_missing_state_fallback(self):
        candidate=replace(self.settings,resource_url='https://kb.datavyx.com/mcp-candidate',purge_ledger_dir='')
        with self.assertRaises(KnowledgeError):AccountState(candidate).root()


if __name__=='__main__':unittest.main()
