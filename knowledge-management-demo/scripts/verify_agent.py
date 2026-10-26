"""Real DeepSeek + Pydantic AI + disposable Neo4j acceptance. No live writes."""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from demo.config import Config, load_model_env
from demo.graphiti_extension import Pipeline
from demo.service import Service
from demo.store import Store
from fixtures import commit

records, trace = [], []


def check(name, actual, expected=True):
    ok = actual == expected
    records.append(dict(name=name, passed=ok, actual=actual, expected=expected))
    print(("PASS " if ok else "FAIL ") + name, flush=True)


async def turn(service, text):
    print("TURN " + text, flush=True)
    response = await service.chat(text)
    trace.append({"dataset": service.store.dataset, "text": text, "response": response,
                  "tools": service.store.conversation(service.scope).get("last_tool_log")})
    print("REPLY " + response.get("message", "；".join(response.get("questions", []))), flush=True)
    return response


async def save(service, response):
    draft = response.get("draft")
    if not draft:
        return None
    result = await service.confirm(draft["draft_id"], draft["approval_id"], draft["revision"])
    trace.append({"confirmation": result, "tools": service.store.conversation(service.scope).get("last_tool_log")})
    check("native_deferred_commit_" + draft["draft_id"][:8], result.get("idempotent"), False)
    return draft


async def run(args):
    if args.conversation_only:
        for _ in range(args.repeat):
            await multi_turn(args)
        return
    for index in range(args.repeat):
        # Each name/identifier variant is a different domain and phrasing.
        cases = [
            ("只新建一份合同，名称是 A002，暂时没有编号。", "合同", "A002", ""),
            ("登记一个设备，仅给它编号 EQ-007，名称暂不提供。", "设备", "", "EQ-007"),
            ("新增实验样本，把名称记作 ZX9；这次不提供编号。", "实验样本", "ZX9", ""),
            ("创建一项维护工单：编号 WO-215，不填名称。", "维护工单", "", "WO-215"),
        ]
        if args.smoke:
            cases = cases[:1]
        for number, (text, kind, name, identifier) in enumerate(cases):
            store = Store(Config(uri="bolt://127.0.0.1:17694", password="", dataset="agent_" + uuid4().hex))
            store.initialize()
            pipeline = Pipeline(store.config)
            service = Service(store, pipeline)
            try:
                response = await turn(service, text)
                draft = await save(service, response)
                check(f"field_preview_{index}_{number}", bool(draft))
                if draft:
                    obj = next(o for o in draft["objects"] if o["new"])
                    current = store.detail(obj["id"])["object"]
                    check(f"independent_name_identifier_{index}_{number}", [current["name"], current["identifier"]], [name, identifier])
                    idem = await service.confirm(draft["draft_id"], draft["approval_id"], draft["revision"])
                    check(f"confirm_idempotent_{index}_{number}", idem["idempotent"])
                    check(f"full_history_{index}_{number}", any(p["part_kind"] == "tool-return" for m in store.conversation(service.scope)["history"] for p in m["parts"]))
            finally:
                await service.runtime.close()
                await pipeline.close()
                store.close()
        if args.smoke or args.fields_only:
            continue
        for domain, correct, person, company, relation_text, wrong_kind, correct_kind in [
            ("business", True, "王总", "广通公司", "王总是广通公司的联系人。", "客户", "人员"),
            ("business", False, "王总", "广通公司", "王总是广通公司的联系人。他是人员，之前误记成设备，请更正。", "设备", "人员"),
            ("research", True, "林博士", "极光研究所", "补一条关系：林博士担任极光研究所的项目联络人。", "仪器", "人员"),
            ("research", False, "远望镜", "观测站", "远望镜原先被误归到人员，实际上是观测设备，并安装在观测站。", "人员", "观测设备"),
        ]:
            store = Store(Config(uri="bolt://127.0.0.1:17694", password="", dataset="agent_" + uuid4().hex))
            store.initialize()
            seed = commit(store, f"已有对象：{person}。", {"objects": [{"ref": "p", "name": person, "kind": correct_kind if correct else wrong_kind}], "facts": []})
            oid = seed["objects"][0]["id"]
            pipeline = Pipeline(store.config)
            service = Service(store, pipeline)
            try:
                response = await turn(service, relation_text)
                draft = await save(service, response)
                label = f"{domain}_{correct}_{index}"
                check("relation_preview_" + label, bool(draft))
                if draft:
                    same = next((o for o in draft["objects"] if o["id"] == oid), None)
                    check("preserve_object_id_" + label, same is not None)
                    types = [u for u in draft["entity_updates"] if u["field"] == "kind"] + [
                        {"previous":u["before"]["kind"],"value":u["after"]["kind"]} for u in draft.get("reviews", []) if u["action"] == "reclassify"]
                    check("type_change_only_when_wrong_" + label, bool(types), not correct)
                    check("current_type_" + label, store.detail(oid)["object"]["kind"], correct_kind)
                    check("relation_present_" + label, any(f["object"] for f in draft["facts"]))
                    check("no_inferred_contract_customer_" + label, not any(o["kind"] == "合同" for o in draft["objects"]))
                    check("mutation_audit_" + label, bool(store.mutation_history(oid)))
                    if types:
                        check("type_history_" + label, [types[0]["previous"], types[0]["value"]], [wrong_kind, correct_kind])
            finally:
                await service.runtime.close()
                await pipeline.close()
                store.close()
    if not args.smoke:
        await multi_turn(args)


async def multi_turn(args):
    store = Store(Config(uri="bolt://127.0.0.1:17694", password="", dataset="agent_" + uuid4().hex))
    store.initialize()
    pipeline = Pipeline(store.config)
    service = Service(store, pipeline)
    try:
        response = await turn(service, "王总，准备签一个80万的合同。")
        # The model may ask identity and currency in either order.
        check("multi_initial_clarify", response.get("clarification_required", False))
        response = await turn(service, "新建一份合同。")
        check("new_choice_not_reasked", response.get("clarification_required", False) and not any("还是" in q and "已有" in q for q in response["questions"]))
        response = await turn(service, "名称叫非遗视频管理系统 A002。")
        check("no_currency_inference_before_explicit", not any(f.get("value") and f["value"].get("unit") for f in response.get("draft", {}).get("facts", [])))
        if response.get("clarification_required"):
            check("multi_no_repeat_create_choice", not any("还是" in q and "已有" in q for q in response["questions"]))
        response = await turn(service, "金额的币种是人民币，其他信息保持刚才的。")
        draft = response.get("draft")
        check("multi_final_preview", bool(draft))
        if draft:
            obj = next(o for o in draft["objects"] if o["kind"] == "合同")
            check("multi_name_only", [obj["name"], obj["identifier"]], ["非遗视频管理系统 A002", ""])
            check("multi_preserved_amount", any(f.get("value") and f["value"]["value"] == "800000" and f["value"]["unit"] == "CNY" for f in draft["facts"]))
            original_id = draft["draft_id"]
            result = await turn(service, "先查一下，当前已经确认录入了几份合同？")
            check("detour_real_query", result.get("result", {}).get("count"), 0)
            check("detour_preserves_preview", store.conversation(service.scope)["pending"]["draft_id"], original_id)
            # Reconstruct the service to prove durable native history/approval.
            await service.runtime.close()
            service = Service(store, pipeline)
            restored = service.public_draft(original_id)
            await save(service, {"draft": restored})
            result = await turn(service, "刚才那份合同目前有多少份，人民币金额合计是多少？")
            check("query_actual_result", bool(result.get("result")))
            check("natural_answer_contains_total", "800000" in result.get("message", "").replace(",", "") or "80万" in result.get("message", "").replace(" ", ""))
            response = await turn(service, "王总是广通公司的联系人。")
            relation = response.get("draft")
            check("saved_focus_new_relation", bool(relation))
            if relation:
                check("no_contract_customer_from_contact", not any(o["kind"] == "合同" for o in relation["objects"]))
                await save(service, response)
            response = await turn(service, "另开一个任务：新建设备，编号为 Q-11，不要名称。")
            topic = response.get("draft")
            check("topic_switch_preview", bool(topic))
            if topic:
                check("topic_switch_no_old_money", not any(f.get("value") and f["value"].get("unit") == "CNY" for f in topic["facts"]))
                await turn(service, "取消当前未保存的任务。")
                check("cancel_closes_preview", store.draft(topic["draft_id"])["status"], "superseded")
    finally:
        await service.runtime.close()
        await pipeline.close()
        store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-env", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--conversation-only", action="store_true")
    parser.add_argument("--fields-only", action="store_true", help="Run all name/identifier cases and conversation regression, omitting relation scenarios")
    parser.add_argument("--report-label", default="")
    args = parser.parse_args()
    if args.report_label and not all(c.isascii() and (c.isalnum() or c in "-_") for c in args.report_label):
        parser.error("report-label must contain only ASCII letters, digits, hyphens or underscores")
    load_model_env(args.model_env)
    os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
    started = time.time()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        import traceback
        traceback.print_exception(type(exc), exc, exc.__traceback__, chain=False)
        check("execution_complete", type(exc).__name__, "complete")
    finally:
        report = {"passed": sum(r["passed"] for r in records), "failed": sum(not r["passed"] for r in records),
                  "seconds": round(time.time() - started, 2), "repeat": args.repeat, "model": os.getenv("LLM_MODEL"),
                  "results": records, "trace": trace}
        (ROOT / "artifacts").mkdir(exist_ok=True)
        name = "agent-smoke.json" if args.smoke else "agent-conversation.json" if args.conversation_only else "agent-acceptance.json"
        if args.report_label:
            name = "agent-" + args.report_label + ".json"
        (ROOT / "artifacts" / name).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Passed {report['passed']}; failed {report['failed']}", flush=True)
    raise SystemExit(bool(report["failed"]))


if __name__ == "__main__":
    main()
