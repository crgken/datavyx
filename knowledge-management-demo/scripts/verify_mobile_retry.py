"""Failed-request retry regression on the dedicated test database; no model calls."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from demo.config import Config
from demo.contracts import ExecutionScope
from demo.mobile_gateway import MobileGateway
from demo.model import DemoError
from demo.store import Store


async def main():
    store = Store(Config(uri="bolt://127.0.0.1:17694", dataset="retry-test-" + uuid4().hex))
    gateway = MobileGateway(store, None)
    service = SimpleNamespace(scope=ExecutionScope(store.dataset),
        chat=AsyncMock(side_effect=[RuntimeError("injected model failure"), {"message": "recovered"}]))
    payload = {"request_id": uuid4().hex, "message": "isolated retry fixture"}
    try:
        store.initialize()
        gateway.submit(service, payload)
        await asyncio.gather(*tuple(gateway.tasks))
        assert gateway.request_status(service.scope, payload["request_id"])["status"] == "failed"
        print("PASS failed request is durable", flush=True)
        repeated = gateway.submit(service, payload)
        assert repeated["status"] == "failed" and service.chat.await_count == 1
        print("PASS status replay does not automatically retry", flush=True)
        retry = gateway.submit(service, {**payload, "retry": True})
        assert retry["status"] == "running"
        await asyncio.gather(*tuple(gateway.tasks))
        assert gateway.request_status(service.scope, payload["request_id"])["status"] == "succeeded"
        assert service.chat.await_count == 2
        print("PASS explicit retry transitions failed to running to succeeded", flush=True)
        repeated = gateway.submit(service, {**payload, "retry": True})
        assert repeated["status"] == "succeeded" and service.chat.await_count == 2
        print("PASS retry after success does not execute again", flush=True)
        try:
            gateway.submit(service, {**payload, "message": "different fixture", "retry": True})
        except DemoError as error:
            assert error.code == "idempotency_conflict"
        else:
            raise AssertionError("Changed input reused the same request ID")
        print("PASS changed input cannot reuse request ID", flush=True)
    finally:
        await gateway.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
