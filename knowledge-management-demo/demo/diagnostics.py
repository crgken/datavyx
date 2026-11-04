"""Failure locations and provider status, without messages, payloads or secrets."""
import hashlib
import json
import logging
from pathlib import Path
import traceback


def log_failure(stage, error, *, request_id=None, elapsed=None):
    details = {"stage": stage, "causes": []}
    if request_id:
        details["request_ref"] = hashlib.sha256(request_id.encode()).hexdigest()[:16]
    if elapsed is not None:
        details["elapsed_seconds"] = round(elapsed, 3)
    seen = set()
    current = error
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        cause = {"type": type(current).__name__}
        status = getattr(current, "status_code", None)
        if type(status) is int and 100 <= status <= 599:
            cause["http_status"] = status
        cause["frames"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(current.__traceback__)[-8:]
        ]
        details["causes"].append(cause)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__)
    logging.getLogger("demo").error("operation_failed %s", json.dumps(details))
