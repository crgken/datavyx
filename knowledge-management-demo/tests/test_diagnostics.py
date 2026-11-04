import hashlib
import json
import unittest

from demo.diagnostics import log_failure


class DiagnosticsTests(unittest.TestCase):
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
