"""Adversarial application guards, against real disposable Neo4j, without LLM calls."""
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pydantic import ValidationError
from demo.config import Config
from demo.contracts import ChangeProposal, ExecutionScope
from demo.graphiti_extension import Pipeline
from demo.model import DemoError
from demo.service import Service
from demo.service import public_text
from demo.store import Store
from fixtures import commit

records = []


def check(name, actual, expected=True):
    passed = actual == expected
    records.append(dict(name=name, passed=passed, actual=actual, expected=expected))
    print(("PASS " if passed else "FAIL ") + name, flush=True)


def denied(name, operation, code):
    try:
        operation()
        check(name, "accepted", code)
    except DemoError as exc:
        check(name, exc.code, code)
    except ValidationError:
        check(name, "invalid_structure", code)


def source(mid, text):
    return [{"message_id": mid, "quote": text}]


def proposal(mid, text, field="name", value="A002"):
    return ChangeProposal(task_relation="new_change", entities=[{"ref": "x", "action": "create", "kind": "测试对象",
        "assignments": [{"field": field, "value": value, "sources": source(mid, text)}]}], source_message_ids=[mid])


def main():
    store = Store(Config(uri="bolt://127.0.0.1:17694", password="", dataset="guards_" + uuid4().hex))
    store.initialize()
    scope = ExecutionScope(store.dataset)
    service = Service(store, Pipeline(store.config), runtime=object())
    try:
        check("private_preview_id_hidden", public_text("已生成预览（草稿 opaque-id），请确认。", ["opaque-id"]), "已生成预览，请确认。")
        check("business_identifier_unchanged", public_text("名称 A002，金额 800000 CNY。", ["opaque-id"]), "名称 A002，金额 800000 CNY。")
        text = "只记录名称 A002。"
        mid = store.message(text)
        state = store.conversation(scope)
        deps = service.dependencies(state, {"id": mid, "text": text})
        p = proposal(mid, text)
        denied("model_cannot_override_dataset", lambda: replace(scope, dataset="other").check(store, "read"), "scope_denied")
        denied("read_permission", lambda: replace(scope, permissions=frozenset()).check(store, "read"), "permission_denied")
        no_write = replace(deps, scope=replace(scope, permissions=frozenset({"read"})))
        denied("propose_permission", lambda: service.changes.prepare_change(p, no_write), "permission_denied")
        denied("closed_schema", lambda: ChangeProposal.model_validate({**p.model_dump(), "cypher": "DELETE"}), "invalid_structure")
        bad = p.model_copy(deep=True)
        bad.entities[0].assignments[0].sources[0].quote = "不存在的原文"
        denied("quote_exact_match", lambda: service.changes.prepare_change(bad, deps), "invalid_evidence")
        assistant = store.message("助手猜测的值", role="assistant")
        bad = p.model_copy(deep=True)
        bad.source_message_ids.append(assistant)
        baddeps = replace(deps, allowed_source_ids=[mid, assistant])
        denied("assistant_cannot_be_source", lambda: service.changes.prepare_change(bad, baddeps), "invalid_task_sources")
        other = store.message("其他会话原文", session_id="other")
        bad.source_message_ids = [mid, other]
        denied("source_session_scope", lambda: service.changes.prepare_change(bad, replace(deps, allowed_source_ids=[mid, other])), "invalid_task_sources")
        denied("source_allowlist", lambda: service.changes.prepare_change(bad, deps), "invalid_task_sources")
        older = store.message("旧任务已结束的原文。")
        bad.source_message_ids = [mid, older]
        denied("closed_task_not_new_source", lambda: service.changes.prepare_change(bad, replace(deps, allowed_source_ids=[mid, older])), "closed_task_sources")
        numeric = p.model_copy(deep=True)
        from demo.model import FactInput, SourceQuote
        numeric.facts = [FactInput(subject="x", predicate="数值属性", value={"type": "number", "value": "80", "unit": "ANY"}, evidence=text, sources=source(mid, text))]
        denied("numeric_unit_requires_provenance", lambda: service.changes.prepare_change(numeric, deps), "unit_evidence_required")
        numeric.facts[0].unit_source = SourceQuote(message_id=mid, quote="原文不存在的单位")
        denied("unit_quote_cannot_be_fabricated", lambda: service.changes.prepare_change(numeric, deps), "invalid_evidence")
        numeric.facts[0].unit_source = SourceQuote(message_id=mid, quote="A002")
        numeric.facts[0].value.unit = "CNY"
        denied("unit_label_must_match_canonical", lambda: service.changes.prepare_change(numeric, deps), "unit_label_mismatch")
        from demo.unit_labels import unit_label_matches
        check("cldr_currency_label", unit_label_matches("人民币", "CNY"))
        check("cldr_other_currency", unit_label_matches("美元", "USD"))
        check("generic_unit_preserved", unit_label_matches("千克", "千克"))
        check("bare_magnitude_not_currency", unit_label_matches("万", "CNY"), False)
        prepared = service.changes.prepare_change(p, deps)
        draft = store.draft(prepared["draft_id"])
        check("name_only_stays_name", [draft["objects"][0]["name"], draft["objects"][0]["identifier"]], ["A002", ""])
        check("preview_no_business_write", store.objects()["total"], 0)
        denied("no_direct_commit_without_grant", lambda: store.commit(draft["draft_id"]), "approval_required")
        approval = store.bind_approval(scope, draft["draft_id"], str(uuid4()))
        denied("pending_is_not_confirmed", lambda: store.commit(draft["draft_id"], approval["id"], scope), "approval_required")
        denied("fake_grant", lambda: store.approve(scope, draft["draft_id"], str(uuid4()), draft["revision"]), "invalid_approval")
        denied("confirm_permission", lambda: store.approve(no_write.scope, draft["draft_id"], approval["id"], draft["revision"]), "permission_denied")
        denied("confirm_actor_scope", lambda: store.approve(replace(scope, actor="other"), draft["draft_id"], approval["id"], draft["revision"]), "invalid_approval")
        denied("confirm_session_scope", lambda: store.approve(replace(scope, session_id="other"), draft["draft_id"], approval["id"], draft["revision"]), "invalid_approval")
        denied("confirm_version", lambda: store.approve(scope, draft["draft_id"], approval["id"], draft["revision"] + 1), "preview_stale")
        denied("version_not_bool", lambda: store.approve(scope, draft["draft_id"], approval["id"], True), "invalid_approval")
        store.approve(scope, draft["draft_id"], approval["id"], draft["revision"])
        original_record = store.record_commit_context
        def fail_after_writes(*args):
            raise DemoError("injected_transaction_failure", "受控事务故障")
        store.record_commit_context = fail_after_writes
        denied("transaction_failure", lambda: store.commit(draft["draft_id"], approval["id"], scope), "injected_transaction_failure")
        check("transaction_rollback_objects", store.objects()["total"], 0)
        check("transaction_rollback_draft", store.draft(draft["draft_id"])["status"], "pending")
        check("transaction_rollback_approval", store.read(lambda tx: tx.run("MATCH (a:KDApproval {id:$id}) RETURN a.status AS s", id=approval["id"]).single()["s"]), "approved")
        store.record_commit_context = original_record
        store.commit(draft["draft_id"], approval["id"], scope)
        check("duplicate_commit", store.commit(draft["draft_id"], approval["id"], scope)["idempotent"])
        check("one_object", store.objects()["total"], 1)
        # Identifier-only new object retains an empty canonical name.
        mid = store.message("仅提供编号 N-7。")
        deps = service.dependencies(store.conversation(scope), {"id": mid, "text": "仅提供编号 N-7。"})
        new = service.changes.prepare_change(proposal(mid, "仅提供编号 N-7。", "identifier", "N-7"), deps)
        obj = new["objects"][0]
        check("identifier_only_stays_identifier", [obj["name"], obj["identifier"]], ["", "N-7"])
        grant = store.bind_approval(scope, new["draft_id"], str(uuid4()))
        store.approve(scope, new["draft_id"], grant["id"], new["revision"])
        commit(store, "并发写入另一个对象。", {"objects": [{"ref": "c", "name": "并发对象", "kind": "其他"}], "facts": []})
        denied("stale_draft_rejected_at_commit", lambda: store.commit(new["draft_id"], grant["id"], scope), "preview_stale")
        denied("stale_prepare", lambda: service.changes.prepare_change(proposal(mid, "仅提供编号 N-7。", "identifier", "N-8"), deps), "data_changed")
        # Same ID, explicit type correction, unchanged historical fact/source.
        seed = commit(store, "望远镜是设备，原登记错了。", {"objects": [{"ref": "p", "name": "望远镜", "kind": "人员"}],
            "facts": [{"subject": "p", "predicate": "位置", "value": {"type": "text", "value": "二楼"}, "evidence": "望远镜是设备，原登记错了。"}]})
        oid = seed["objects"][0]["id"]
        text = "将望远镜从人员更正为设备，旧位置记录保留。"
        mid = store.message(text)
        deps = service.dependencies(store.conversation(scope), {"id": mid, "text": text})
        correction = ChangeProposal(task_relation="new_change", entities=[{"ref": "p", "action": "reclassify", "entity_id": oid, "kind": "设备", "classification_error": True,
            "reason": "原记录把设备误归为人员", "sources": source(mid, text)}], source_message_ids=[mid])
        deps.entities_seen.clear()
        denied("inspect_before_change", lambda: service.changes.prepare_change(correction, deps), "entity_not_inspected")
        deps.entities_seen[oid] = store.detail(oid, history=True)
        from demo.contracts import EntityProposal
        unrelated = store.objects(kind="其他")["rows"][0]
        correction.entities.append(EntityProposal(ref="unrelated", action="reference", entity_id=unrelated["id"]))
        deps.entities_seen[unrelated["id"]] = store.detail(unrelated["id"])
        implicit = correction.model_copy(deep=True)
        implicit.entities[0].action = "reference"
        denied("no_implicit_type_change", lambda: service.changes.prepare_change(implicit, deps), "type_change_requires_operation")
        changed = service.changes.prepare_change(correction, deps)
        grant = store.bind_approval(scope, changed["draft_id"], str(uuid4()))
        store.approve(scope, changed["draft_id"], grant["id"], changed["revision"])
        store.commit(changed["draft_id"], grant["id"], scope)
        current = store.detail(oid, history=True)
        check("type_correction_preserves_id", current["object"]["id"], oid)
        check("type_correction_updates_type", current["object"]["kind"], "设备")
        check("type_correction_preserves_history", len(current["rows"]), 1)
        check("type_audit_before_after", bool(store.mutation_history(oid)))
        check("audit_only_this_entity_updates", all(not json.loads(e["change"])["entity_updates"] for e in store.mutation_history(unrelated["id"])))
        check("type_correction_no_duplicate", len([o for o in store.objects()["rows"] if o["name"] == "望远镜"]), 1)
        # Retraction retains old fact and evidence.
        text = "撤回望远镜的位置，原位置录错了。"
        mid = store.message(text)
        deps = service.dependencies(store.conversation(scope), {"id": mid, "text": text})
        deps.entities_seen[oid] = current
        retract = ChangeProposal(task_relation="new_change", entities=[{"ref": "p", "action": "reference", "entity_id": oid}],
            retractions=[{"fact_id": current["rows"][0]["id"], "reason": "原位置录错", "sources": source(mid, text)}], source_message_ids=[mid])
        prepared = service.changes.prepare_change(retract, deps)
        grant = store.bind_approval(scope, prepared["draft_id"], str(uuid4()))
        store.approve(scope, prepared["draft_id"], grant["id"], prepared["revision"])
        store.commit(prepared["draft_id"], grant["id"], scope)
        check("retraction_not_delete", store.detail(oid, history=True)["rows"][0]["active"], False)
        check("retraction_evidence_preserved", store.sources(current["rows"][0]["id"])["total"], 1)
        # App scope is checked before memory access.
        other_service = Service(store, Pipeline(store.config), runtime=object(), scope=replace(scope, dataset="other"))
        try:
            asyncio.run(other_service.memory.retrieve_context("任何内容"))
            check("memory_scope", False)
        except DemoError as exc:
            check("memory_scope", exc.code, "scope_denied")
        empty = asyncio.run(service.memory.retrieve_context("尚未投影的对象"))
        check("no_index_explicit_status", empty["status"], "not_indexed")
        class FailedRuntime:
            async def run(self, *args):
                raise DemoError("controlled_model_failure", "受控模型故障", 502)
        failed = Service(store, Pipeline(store.config), runtime=FailedRuntime())
        before = sum(m["role"] == "assistant" for m in store.messages())
        try:
            asyncio.run(failed.chat("故障注入请求"))
            check("model_failure_propagates", False)
        except DemoError as exc:
            check("model_failure_propagates", exc.code, "controlled_model_failure")
        check("model_failure_no_fake_answer", sum(m["role"] == "assistant" for m in store.messages()), before)
        check("failed_turn_sources_remain_resumable", bool(store.conversation(scope)["pending"]["source_message_ids"]))
        # Exercise the actual Pydantic AI deferred tool path, with a controlled
        # test model that is never used by the product or as an error fallback.
        from pydantic_ai.models.test import TestModel
        from demo.agent_runtime import AgentRuntime
        runtime = AgentRuntime(store, service.pipeline, model=TestModel())
        deps.approval_id, deps.approved_draft_id = "forged-approval", prepared["draft_id"]
        approval_event = {"draft_id": prepared["draft_id"], "tool_call_id": str(uuid4())}
        try:
            asyncio.run(runtime.confirm(deps, approval_event))
            check("native_deferred_cannot_forge_grant", False)
        except DemoError as exc:
            check("native_deferred_cannot_forge_grant", exc.code, "approval_required")
        deps.approval_id = grant["id"]
        deps.scope = replace(scope, permissions=frozenset({"read"}))
        try:
            asyncio.run(runtime.confirm(deps, approval_event))
            check("native_deferred_obeys_permissions", False)
        except DemoError as exc:
            check("native_deferred_obeys_permissions", exc.code, "permission_denied")
        migrated = Store(replace(store.config, dataset="migration_" + uuid4().hex))
        try:
            migrated.initialize()
            commit(migrated, "旧记录已完成。", {"objects": [{"ref": "m", "name": "旧对象", "kind": "历史类型"}], "facts": []})
            migrated_scope = ExecutionScope(migrated.dataset)
            check("migration_does_not_reopen_committed", migrated.conversation(migrated_scope)["pending"], None)
            failed_id = migrated.message("升级前尚未成功处理的输入。")
            check("migration_recovers_unprocessed", migrated.conversation(migrated_scope)["pending"]["source_message_ids"], [failed_id])
            check("migration_no_cross_session_focus", migrated.conversation(replace(migrated_scope, session_id="other"))["focus_refs"], [])
        finally:
            migrated.close()
    finally:
        store.close()
        report = {"passed": sum(r["passed"] for r in records), "failed": sum(not r["passed"] for r in records), "results": records}
        (ROOT / "artifacts" / "guard-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Passed {report['passed']}; failed {report['failed']}", flush=True)
    raise SystemExit(bool(report["failed"]))


if __name__ == "__main__":
    main()
