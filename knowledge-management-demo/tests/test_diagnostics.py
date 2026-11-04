import hashlib
import json
import os
import unittest
from unittest.mock import patch

from demo.diagnostics import log_failure, log_mobile_event


class DiagnosticsTests(unittest.TestCase):
    def test_request_diagnostics_are_opt_in(self):
        with patch.dict(os.environ, {"KD_REQUEST_DIAGNOSTICS": "0"}):
            with self.assertNoLogs("demo", level="WARNING"):
                log_mobile_event("chat_submit", {"message": "private input"})

    def test_request_diagnostics_record_flags_without_payload(self):
        secret = "private-value-that-must-not-appear"
        payload = {"message": secret, "request_id": secret, "conversation_id": secret,
                   "Authorization": secret, "retry": True}
        with patch.dict(os.environ, {"KD_REQUEST_DIAGNOSTICS": "1"}):
            with self.assertLogs("demo", level="WARNING") as logs:
                log_mobile_event("chat_submit", payload, status="failed")
        record = logs.records[0].getMessage()
        self.assertNotIn(secret, record)
        details = json.loads(record.removeprefix("mobile_request_diagnostic "))
        self.assertEqual(details["operation"], "chat_submit")
        self.assertTrue(details["retry_provided"])
        self.assertTrue(details["retry"])
        self.assertEqual(details["status"], "failed")
        self.assertEqual(set(details), {"operation", "retry_provided", "retry",
                                      "request_ref", "conversation_ref", "status"})

    def test_chain_status_and_locations_without_exception_contents(self):
        secret = "test-secret-that-must-never-be-logged"
        try:
            try:
                error = RuntimeError(secret)
                error.status_code = 429
                error.body = {"api_key": secret}
                raise error
            except RuntimeError as error:
                raise ValueError("private user input " + secret) from error
        except ValueError as error:
            with self.assertLogs("demo", level="ERROR") as logs:
                log_failure("mobile_chat", error, request_id=secret, elapsed=1.23456)
        record = logs.records[0].getMessage()
        self.assertNotIn(secret, record)
        details = json.loads(record.removeprefix("operation_failed "))
        self.assertEqual(details["request_ref"], hashlib.sha256(secret.encode()).hexdigest()[:16])
        self.assertEqual(details["elapsed_seconds"], 1.235)
        self.assertEqual([c["type"] for c in details["causes"]], ["ValueError", "RuntimeError"])
        self.assertEqual(details["causes"][1]["http_status"], 429)
        self.assertEqual(details["causes"][0]["frames"][-1]["file"], "test_diagnostics.py")

    def test_cyclic_causes_are_bounded(self):
        error = RuntimeError("private")
        error.__cause__ = error
        with self.assertLogs("demo", level="ERROR") as logs:
            log_failure("agent_turn", error)
        details = json.loads(logs.records[0].getMessage().removeprefix("operation_failed "))
        self.assertEqual(len(details["causes"]), 1)
