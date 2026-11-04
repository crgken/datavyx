"""Failure locations and provider status, without messages, payloads or secrets."""
import hashlib
import json
import logging
import os
import re
from pathlib import Path
import traceback


def log_mobile_event(operation, payload, *, status=None):
    if os.getenv("KD_REQUEST_DIAGNOSTICS") != "1":
        return
    details = {"operation": operation, "retry_provided": "retry" in payload,
               "retry": payload.get("retry") is True}
    for source, target in (("request_id", "request_ref"), ("conversation_id", "conversation_ref")):
        value = payload.get(source)
        if isinstance(value, str) and value:
            details[target] = hashlib.sha256(value.encode()).hexdigest()[:16]
    if isinstance(status, str) and status in {"running", "succeeded", "failed", "interrupted", "not_found"}:
        details["status"] = status
    logging.getLogger("demo").warning("mobile_request_diagnostic %s", json.dumps(details))


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
        code = getattr(current, "code", None)
        if isinstance(code, str) and re.fullmatch(r"Neo\.(ClientError|TransientError|DatabaseError)\.[A-Za-z0-9_.]{1,160}", code):
            cause["database_code"] = code
        frames = traceback.extract_tb(current.__traceback__)
        if len(frames) > 12:
            frames = frames[:6] + frames[-6:]
        cause["frames"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in frames
        ]
        details["causes"].append(cause)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__)
    logging.getLogger("demo").error("operation_failed %s", json.dumps(details))
