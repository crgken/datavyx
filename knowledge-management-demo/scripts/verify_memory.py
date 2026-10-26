"""Actual Graphiti projection/search and provenance isolation on test Neo4j."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from demo.config import Config, load_model_env
from demo.contracts import ExecutionScope
from demo.graphiti_adapter import GraphitiAdapter
from demo.graphiti_extension import Pipeline
from demo.model import DemoError
from demo.store import Store
from demo.service import Service
from fixtures import commit

records = []


def check(name, actual, expected=True):
    ok = actual == expected
    records.append(dict(name=name, passed=ok, actual=actual, expected=expected))
    print(("PASS " if ok else "FAIL ") + name, flush=True)


async def run():
    stores, pipes, drafts = [], [], []
    try:
        for index in range(2):
            token = uuid4().hex[:8]
            person, company = "联络员" + token, "星海机构" + token
            store = Store(Config(uri="bolt://127.0.0.1:17694", password="", dataset="memory_" + uuid4().hex))
            store.initialize()
            stores.append(store)
            text = person + "是" + company + "的联系人。"
            draft = commit(store, text, {"objects": [{"ref": "p", "name": person, "kind": "人员"}, {"ref": "c", "name": company, "kind": "组织"}],
                "facts": [{"subject": "p", "predicate": "联系人", "object": "c", "cardinality": "many", "evidence": text}]})
            drafts.append(draft)
            pipe = Pipeline(store.config)
            pipes.append(pipe)
            job = store.pending_projections()[0]
            before = store.query({"operation": "COUNT"})["count"]
            print("PROJECT " + str(index), flush=True)
            await pipe.project(job, store)
            check("projection_ready_" + str(index), store.draft(job["id"])["projection_status"], "ready")
            check("projection_canonical_unchanged_" + str(index), store.query({"operation": "COUNT"})["count"], before)
        adapter = GraphitiAdapter(stores[0], pipes[0], ExecutionScope(stores[0].dataset))
        print("SEARCH", flush=True)
        result = await adapter.retrieve_context(drafts[0]["source"])
        check("native_search_has_memory", bool(result["items"]))
        allowed = {o["id"] for o in drafts[0]["objects"]}
        foreign = {o["id"] for o in drafts[1]["objects"]}
        returned = {oid for item in result["items"] for oid in item["canonical_refs"]}
        check("canonical_mapping", returned <= allowed and bool(returned))
        check("memory_no_foreign_refs", not bool(returned & foreign))
        check("native_search_not_complete", result["complete"], False)
        check("current_canonical_snapshots", all(item["canonical_records"] for item in result["items"]))
        foreign_result = await adapter.retrieve_context(drafts[1]["source"])
        check("foreign_query_stays_scoped", all(set(item["canonical_refs"]) <= allowed for item in foreign_result["items"]))
        # Controlled failure injection only in acceptance, never a product fallback.
        async def failed_client():
            raise OSError("controlled unavailable")
        pipes[0].graphiti = failed_client
        try:
            await adapter.retrieve_context("测试故障")
            check("memory_failure_explicit", False)
        except DemoError as exc:
            check("memory_failure_explicit", exc.code, "memory_unavailable")
        # Canonical lifecycle gates apply before index synchronization, including
        # when the semantic client is currently unavailable.
        service = Service(stores[0], pipes[0])
        oid = drafts[0]["objects"][0]["id"]
        fid = stores[0].detail(oid)["rows"][0]["id"]
        async def apply(target_type, target_id, action):
            d = await service.prepare_review({"operations":[{"target_type":target_type,"target_id":target_id,"action":action,"reason":"记忆过滤验收"}],"revision":stores[0].context()["revision"]})
            await service.confirm(d["draft_id"],d["approval_id"],d["revision"])
        await apply("entity",oid,"archive")
        result = await adapter.retrieve_context("联系人")
        check("archived_memory_filtered_before_sync", result["items"], [])
        await apply("entity",oid,"restore")
        await apply("fact",fid,"retract")
        result = await adapter.retrieve_context("联系人")
        check("withdrawn_memory_filtered_before_sync", result["items"], [])
        check("memory_actions_do_not_require_model", service._runtime is None)
    finally:
        for pipe in pipes:
            await pipe.close()
        for store in stores:
            store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-env", required=True)
    args = parser.parse_args()
    load_model_env(args.model_env)
    os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
    os.environ.setdefault("SEMAPHORE_LIMIT", "3")
    try:
        asyncio.run(run())
    except Exception as exc:
        import traceback
        traceback.print_exception(type(exc), exc, exc.__traceback__, chain=False)
        check("execution_complete", type(exc).__name__, "complete")
    report = {"passed": sum(r["passed"] for r in records), "failed": sum(not r["passed"] for r in records), "results": records}
    (ROOT / "artifacts" / "memory-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Passed {report['passed']}; failed {report['failed']}", flush=True)
    raise SystemExit(bool(report["failed"]))


if __name__ == "__main__":
    main()
