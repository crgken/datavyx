"""Acceptance against the isolated test DB; never confirms the live preview."""
import asyncio
import json
import sys
import urllib.request
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from demo.bulk_import import TablePlan, parse_table
from demo.config import Config, load_model_env
from demo.contracts import ExecutionScope
from demo.graphiti_extension import Pipeline
from demo.model import Change, DemoError
from demo.service import Service
from demo.store import Store


async def main():
    load_model_env(sys.argv[1])
    messages = json.load(urllib.request.urlopen("http://127.0.0.1:8096/api/messages"))["data"]
    source = next(m["text"] for m in reversed(messages) if m["role"] == "user" and "HT-045" in m["text"])
    table = parse_table(source)
    store = Store(Config(uri="bolt://127.0.0.1:17694", password="", dataset="bulk_acceptance_"+uuid4().hex))
    store.initialize()
    pipeline = Pipeline(store.config)
    service = Service(store, pipeline)
    checks = {}
    def check(name, condition):
        checks[name] = bool(condition)
        assert condition, name
    def counts():
        return store.read(lambda tx: dict(tx.run("MATCH (o:KDObject {dataset:$d}) WITH count(o) AS objects MATCH (f:KDFact {dataset:$d}) RETURN objects,count(f) AS facts", d=store.dataset).single()))
    async def submit(text):
        response = await service.chat(text, str(uuid4()))
        jid = response["import_job"]["id"]
        await asyncio.wait_for(service.bulk.tasks[jid], timeout=300)
        job = service.bulk.get(jid)
        assert job["status"] == "ready", job
        print("Preview ready", job["total_rows"], "rows", len(job["draft"]["objects"]), "objects", flush=True)
        return jid, job["draft"]
    try:
        # Reproduce exactly the old partial import, in the ISOLATED dataset.
        old = Change(objects=[{"ref":str(i), "kind":"合同", "identifier":r["cells"][0], "name":r["cells"][1]} for i,r in enumerate(table["rows"][:40])])
        mid = store.message(source)
        seed = store.prepare(old.model_dump(), mid)
        store.commit(seed["draft_id"])
        before = store.graph()
        existing_ids = {o["id"] for o in before["nodes"]}
        jid, draft = await submit(source)
        check("45_rows_5_columns", draft["bulk"]["row_count"] == 45 and all(len(c["covered_columns"]) == 5 for c in draft["bulk"]["coverage"]))
        check("95_objects", len(draft["objects"]) == 95)
        check("reuse_all_40_ids", {o["id"] for o in draft["objects"] if not o["new"]} == existing_ids)
        check("55_new_objects", sum(o["new"] for o in draft["objects"]) == 55)
        check("90_relations_45_values", draft["bulk"]["relation_count"] == 90 and draft["bulk"]["value_count"] == 45)
        values = [f for f in draft["facts"] if f["value"]]
        total = sum(Decimal(f["value"]["value"]) for f in values)
        check("exact_sum_17776088", total == Decimal("17776088"))
        check("literal_unit_and_header_source", all(f["value"]["unit"] == "元" and len(f["sources"]) == 2 and f["unit_source"]["quote"] == "元" for f in values))
        check("nothing_written_before_confirmation", store.graph()["node_count"] == 40 and store.graph()["edge_count"] == 0)
        check("all_resolved", all(o["resolved"] for o in draft["objects"]))
        try:
            store.commit(draft["draft_id"])
            check("approval_required", False)
        except DemoError:
            check("approval_required", True)
        try:
            store.approve(service.scope, draft["draft_id"], draft["approval_id"], draft["revision"]-1)
            check("wrong_revision_rejected", False)
        except DemoError:
            check("wrong_revision_rejected", True)
        try:
            store.import_job(ExecutionScope(store.dataset, actor="other-actor"), jid)
            check("cross_actor_job_rejected", False)
        except DemoError:
            check("cross_actor_job_rejected", True)
        try:
            store.prepare_execution_change(draft["change"], draft["message_id"], [draft["message_id"]], service.scope,
                                           draft["revision"], bulk={**draft["bulk"], "complete":False})
            check("incomplete_aggregate_rejected", False)
        except DemoError:
            check("incomplete_aggregate_rejected", True)
        result = await service.confirm(draft["draft_id"], draft["approval_id"], draft["revision"])
        check("atomic_commit_once", result["revision"] == before["revision"]+1)
        after = counts()
        check("committed_95_objects_135_facts", after == {"objects":95, "facts":135})
        amount_predicate = values[0]["predicate"]
        contract_kind = next(o["kind"] for o in draft["objects"] if o["identifier"] == "HT-001")
        result = store.query({"operation":"SUM", "kind":contract_kind, "predicate":amount_predicate, "unit":"元"})
        check("database_sum_and_participants", result["sum"] == "17776088" and result["participating_count"] == 45)
        quote = store.sources(values[-1]["id"])["rows"][0]
        check("both_row_and_header_persisted", len(quote["quotes"]) == 2)
        _, repeat = await submit(source)
        check("repeat_no_new_entities", all(not o["new"] for o in repeat["objects"]))
        await service.confirm(repeat["draft_id"], repeat["approval_id"], repeat["revision"])
        check("repeat_no_duplicate_facts_or_entities", counts() == after)
        again = await service.confirm(repeat["draft_id"], repeat["approval_id"], repeat["revision"])
        check("confirmation_idempotent", again["idempotent"] and counts() == after)
        try:
            service.bulk.get("unknown")
            check("unknown_job_rejected", False)
        except DemoError as exc:
            check("unknown_job_rejected", exc.status == 404)
        other = "记录以下设备清单。\n| 设备名称 | 所在站点 | 维护人 | 价值（美元） |\n|---|---|---|---|\n| A002 | 北岸站 | 周工 | 1,200 |\n| B003 | 南岸站 | 周工 | 340 |"
        _, variant = await submit(other)
        check("cross_domain_all_columns", variant["bulk"]["row_count"] == 2 and all(len(r["covered_columns"]) == 4 for r in variant["bulk"]["coverage"]))
        check("name_not_identifier", any(o["name"] == "A002" and not o["identifier"] for o in variant["objects"]))
        people = [o for o in variant["objects"] if o["name"] == "周工"]
        check("same_name_different_context", len(people) == 2 and people[0]["ref"] != people[1]["ref"])
        check("other_domain_relationships", variant["bulk"]["relation_count"] >= 4)
        check("other_domain_explicit_unit", all(f["value"]["unit"] == "USD" for f in variant["facts"] if f["value"]))
        await service.cancel_preview(variant["draft_id"])
        invalid = parse_table("| 名称 | 数值 |\n|---|---|\n| A002 | 约100 |")
        plan = TablePlan(action="import", entities=[{"key":"x","kind":"设备","name_column":0,"identity_policy":"stable_name","identity_columns":[0]}], values=[{"subject":"x","predicate":"测量值","column":1,"value_type":"number"}])
        try:
            service.bulk.project(invalid, plan, 0, 1)
            check("uncertain_number_not_coerced", False)
        except DemoError:
            check("uncertain_number_not_coerced", True)
        try:
            service.bulk.validate_plan(invalid, TablePlan(action="import", entities=plan.entities))
            check("missing_column_rejected", False)
        except DemoError:
            check("missing_column_rejected", True)
        report = {"dataset":store.dataset, "checks":checks, "passed":sum(checks.values()), "total":len(checks),
                  "plan":store.import_job(service.scope,jid)["plan"], "sum":str(total), "live_records_modified":False}
        (ROOT/"artifacts/bulk-import-acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps({k:v for k,v in report.items() if k!="plan"},ensure_ascii=False))
    finally:
        await pipeline.close()
        if service._runtime:
            await service.runtime.close()
        store.close()


asyncio.run(main())
