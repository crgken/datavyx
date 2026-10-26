"""Lifecycle acceptance against the disposable Neo4j, with no model calls."""
import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from demo.config import Config
from demo.contracts import ChangeProposal, ExecutionScope, ReviewRequest
from demo.model import DemoError, digest
from demo.service import Service
from demo.store import Store
from fixtures import commit, seed_truth

checks=[]
def check(name, actual, expected=True):
    ok=actual==expected
    checks.append(dict(name=name,passed=ok,actual=actual,expected=expected))
    print(("PASS " if ok else "FAIL ")+name,flush=True)
def denied(name, fn, code):
    try: fn(); check(name,"accepted",code)
    except DemoError as exc: check(name,exc.code,code)
async def rejected(name, fn, code):
    try: await fn(); check(name,"accepted",code)
    except DemoError as exc: check(name,exc.code,code)
def op(kind,id,action,**kw):return dict(target_type=kind,target_id=id,action=action,reason="用户核实测试",**kw)

async def main():
    store=Store(Config(uri="bolt://127.0.0.1:17694",password="",dataset="review_"+uuid4().hex))
    store.initialize()
    # runtime=None remains genuinely unconstructed throughout UI operations.
    service=Service(store,None)
    async def prepare(*ops):
        return await service.prepare_review(dict(operations=list(ops),revision=store.context()["revision"]))
    async def save(draft):return await service.confirm(draft["draft_id"],draft["approval_id"],draft["revision"])
    async def perform(*ops):
        d=await prepare(*ops);await save(d);return d
    text="待核实人员周工与研究站的协作信息，约八万元。"
    seed=commit(store,text,{"objects":[{"ref":"p","name":"周工","kind":"人员","certain":False,"reason":"身份待核实"},{"ref":"s","name":"北岸站","kind":"研究站"}],"facts":[
        {"subject":"p","predicate":"协作方","object":"s","certain":False,"reason":"内容待核实","evidence":text},
        {"subject":"p","predicate":"预算","value":{"type":"text","value":"约八万元"},"certain":False,"reason":"估计值","evidence":text}]})
    oid=seed["objects"][0]["id"];station=seed["objects"][1]["id"]
    facts={f["predicate"]:f for f in store.detail(oid)["rows"]};fid=facts["协作方"]["id"];approx=facts["预算"]["id"]
    before_users=len([m for m in store.messages() if m["role"]=="user"])
    d=await prepare(op("fact",fid,"confirm"))
    check("preview_does_not_write",store.detail(oid)["rows"][0]["review_status"],"pending")
    check("fact_only_preview_blocked",any(r["after"]=="blocked" for r in d["review_impact"]))
    denied("approval_cannot_be_bypassed",lambda:store.commit(d["draft_id"],d["approval_id"],service.scope),"approval_required")
    await save(d)
    fact=next(f for f in store.detail(oid)["rows"] if f["id"]==fid)
    check("content_confirmed_separately",fact["review_status"],"confirmed")
    check("content_remains_blocked",fact["effective_status"],"blocked")
    check("fact_does_not_confirm_object",store.detail(oid)["object"]["confirmed"],False)
    rev=store.context()["revision"];r=await save(d)
    check("confirm_idempotent",r["idempotent"])
    check("repeat_does_not_increment_revision",store.context()["revision"],rev)
    await perform(op("entity",oid,"confirm"))
    check("entity_confirmation_preserves_type",store.detail(oid)["object"]["kind"],"人员")
    check("confirmed_content_now_effective",next(f for f in store.detail(oid)["rows"] if f["id"]==fid)["effective_status"],"effective")
    check("object_does_not_confirm_other_facts",next(f for f in store.detail(oid)["rows"] if f["id"]==approx)["review_status"],"pending")
    await perform(op("fact",approx,"confirm"))
    check("approximate_text_stays_text",next(f for f in store.detail(oid)["rows"] if f["id"]==approx)["value"],{"type":"text","value":"约八万元","unit":""})
    check("sum_does_not_promote_approximation",store.query({"operation":"SUM","kind":"人员","predicate":"预算","unit":"CNY"})["sum"],"0")
    snapshot=store.projection_snapshot(d)
    await perform(op("fact",fid,"retract"))
    check("retraction_changes_projection_fingerprint",snapshot["hash"]!=store.projection_snapshot(d)["hash"])
    check("targeted_retraction_only",[f["id"] for f in store.detail(oid)["rows"]],[approx])
    history=store.detail(oid,history=True)
    check("withdrawn_history_preserved",next(f for f in history["rows"] if f["id"]==fid)["effective_status"],"retracted")
    check("withdrawal_preserves_source",store.sources(fid)["total"],1)
    await perform(op("entity",oid,"archive"))
    check("archived_object_default_hidden",all(o["id"]!=oid for o in store.objects()["rows"]))
    check("archived_object_explicitly_visible",any(o["id"]==oid for o in store.objects(include_archived=True)["rows"]))
    check("archived_count_excluded",store.query({"operation":"COUNT","kind":"人员"})["count"],0)
    check("archived_star_excluded",store.neighbors(station)["total"],0)
    await perform(op("entity",oid,"restore"))
    check("restore_preserves_confirmation",store.detail(oid)["object"]["confirmed"])
    check("restore_does_not_resurrect_retraction",all(f["id"]!=fid for f in store.detail(oid)["rows"]))
    d=await perform(op("fact",fid,"restate"));newid=d["reviews"][0]["after"]["id"]
    check("restate_uses_new_id",newid!=fid)
    check("restate_preserves_original_source",store.sources(newid)["rows"][0]["quote"],text)
    await rejected("cannot_restate_twice",lambda:prepare(op("fact",fid,"restate")),"already_restated")
    await rejected("same_type_is_not_correction",lambda:prepare(op("entity",oid,"reclassify",new_kind="人员",classification_error=True)),"unchanged_type")
    await perform(op("entity",oid,"reclassify",new_kind="工程师",classification_error=True))
    check("type_correction_keeps_id",store.detail(oid)["object"]["id"],oid)
    check("type_correction_applied",store.detail(oid)["object"]["kind"],"工程师")
    check("no_fabricated_user_messages",len([m for m in store.messages() if m["role"]=="user"]),before_users)
    check("button_actions_never_construct_model",service._runtime is None)
    check("review_audit_recorded",any(json.loads(m["change"]).get("action_event_id") for m in store.mutation_history(oid)))
    # Atomic combination: type correction must not itself confirm identity.
    pending=commit(store,"待核实设备",{"objects":[{"ref":"x","name":"测量台","kind":"机构","certain":False,"reason":"分类待核实"}]})["objects"][0]["id"]
    await perform(op("entity",pending,"reclassify",new_kind="设备",classification_error=True))
    check("type_correction_does_not_confirm",store.detail(pending)["object"]["confirmed"],False)
    await perform(op("entity",pending,"archive"));await perform(op("entity",pending,"restore"))
    check("pending_state_survives_archive_restore",store.detail(pending)["object"]["confirmed"],False)
    # Mixed operations roll back if a target snapshot differs.
    d=await prepare(op("entity",pending,"confirm"),op("entity",oid,"archive"))
    await rejected("new_operation_requires_explicit_replace",lambda:prepare(op("entity",oid,"archive")),"pending_preview")
    store.write(lambda tx:tx.run("MATCH (o:KDObject {id:$id}) SET o.reason='changed'",id=pending).consume())
    await rejected("snapshot_guard",lambda:save(d),"review_target_changed")
    check("snapshot_failure_rolls_back_all_targets",store.detail(oid)["object"]["archived"],False)
    await service.cancel_preview(d["draft_id"])
    # A failure after lifecycle writes must roll back the entire transaction.
    d=await prepare(op("entity",pending,"confirm"),op("entity",oid,"archive"))
    original_context=store.record_commit_context
    def fail_context(*args):raise DemoError("injected_failure","验收故障")
    store.record_commit_context=fail_context
    await rejected("lifecycle_transaction_failure",lambda:save(d),"injected_failure")
    store.record_commit_context=original_context
    check("lifecycle_rollback_object",store.detail(pending)["object"]["confirmed"],False)
    check("lifecycle_rollback_other_target",store.detail(oid)["object"]["archived"],False)
    check("lifecycle_rollback_draft",store.draft(d["draft_id"])["status"],"pending")
    action=store.read(lambda tx:dict(tx.run("MATCH (e:KDActionEvent {id:$id}) RETURN e.status AS status",id=d["action_event_id"]).single()))
    check("lifecycle_rollback_action_event",action["status"],"prepared")
    await service.cancel_preview(d["draft_id"])
    # Explicit combined decisions share one atomic revision.
    coupled=commit(store,"待核实项目",{"objects":[{"ref":"x","name":"远星","kind":"项目","certain":False,"reason":"身份待核实"}],"facts":[{"subject":"x","predicate":"阶段","value":{"type":"text","value":"筹备"},"certain":False,"reason":"内容待核实","evidence":"待核实项目"}]})["objects"][0]["id"]
    coupled_fact=store.detail(coupled)["rows"][0]["id"];rev=store.context()["revision"]
    await perform(op("entity",coupled,"confirm"),op("fact",coupled_fact,"confirm"))
    check("combined_confirmation_effective",store.detail(coupled)["rows"][0]["effective_status"],"effective")
    check("combined_confirmation_one_revision",store.context()["revision"],rev+1)
    # Confirming an uncertain numeric record never changes precision.
    money=commit(store,"预算暂估八万元",{"objects":[{"ref":"x","name":"远星","kind":"项目","existing_id":coupled}],"facts":[{"subject":"x","predicate":"预算","value":{"type":"number","value":"80000","unit":"CNY"},"certain":False,"reason":"暂估","evidence":"预算暂估八万元"}]})
    money_id=next(f["id"] for f in store.detail(coupled)["rows"] if f["predicate"]=="预算")
    await perform(op("fact",money_id,"confirm"))
    current=next(f for f in store.detail(coupled)["rows"] if f["id"]==money_id)
    check("numeric_confirmation_preserves_imprecision",current["numeric_precise"],False)
    check("imprecise_numeric_still_blocked",current["effective_status"],"blocked")
    check("imprecise_number_not_summed",store.query({"operation":"SUM","kind":"项目","predicate":"预算","unit":"CNY"})["sum"],"0")
    commit(store,"预算更正为八万元确定值",{"objects":[{"ref":"x","name":"远星","kind":"项目","existing_id":coupled}],"facts":[{"subject":"x","predicate":"预算","value":{"type":"number","value":"80000","unit":"CNY"},"mode":"replace","evidence":"预算更正为八万元确定值"}]})
    check("explicit_precise_replacement_summed",store.query({"operation":"SUM","kind":"项目","predicate":"预算","unit":"CNY"})["sum"],"80000")
    await perform(op("entity",coupled,"archive"))
    check("archived_amount_excluded",store.query({"operation":"SUM","kind":"项目","predicate":"预算","unit":"CNY"})["sum"],"0")
    check("archived_list_excluded",all(o["id"]!=coupled for o in store.query({"operation":"LIST","kind":"项目"})["rows"]))
    denied("archived_object_cannot_be_enriched",lambda:store.prepare({"objects":[{"ref":"x","name":"远星","kind":"项目","existing_id":coupled}]},store.message("补充远星")),"object_archived")
    await rejected("archived_object_cannot_be_confirmed",lambda:prepare(op("entity",coupled,"confirm")),"object_archived")
    d=await prepare(op("entity",pending,"confirm"))
    commit(store,"新增独立对象",{"objects":[{"ref":"z","name":"新项目","kind":"项目"}]})
    await rejected("stale_revision_rejected",lambda:save(d),"preview_stale")
    await service.cancel_preview(d["draft_id"])
    d=await prepare(op("entity",pending,"confirm"))
    other=replace(service.scope,actor="other-user")
    denied("approval_actor_bound",lambda:store.approve(other,d["draft_id"],d["approval_id"],d["revision"]),"invalid_approval")
    restricted=Service(store,None,scope=replace(service.scope,permissions=frozenset({"read"})))
    await rejected("proposal_permission_enforced",lambda:restricted.prepare_review(dict(operations=[op("entity",pending,"confirm")],revision=d["revision"])),"permission_denied")
    denied("cross_dataset_target_rejected",lambda:store.prepare_button_review(ReviewRequest(operations=[op("entity","foreign-id","confirm")],revision=d["revision"]),service.scope),"object_not_found")
    denied("button_cannot_forge_user_quote",lambda:store.prepare_button_review(ReviewRequest(operations=[op("entity",pending,"confirm",sources=[{"message_id":"fake","quote":"confirmed"}])],revision=d["revision"]),service.scope),"invalid_action_evidence")
    store.write(lambda tx:tx.run("MATCH (p:KDDraft {id:$id}) SET p.expires_at=0 WITH p MATCH (a:KDApproval {draft_id:p.id}) SET a.expires_at=0",id=d["draft_id"]).consume())
    await rejected("expired_preview_rejected",lambda:save(d),"preview_expired")
    await service.cancel_preview(d["draft_id"])
    # A large inbound impact must be fully enumerable, without a top-k cap.
    seed_truth(store)
    owner=next(o for o in store.objects(kind="客户",limit=100)["rows"] if store.neighbors(o["id"],limit=100)["total"]>25)
    count=store.neighbors(owner["id"],limit=100)["total"]
    d=await prepare(op("entity",owner["id"],"archive"))
    first=service.review_impact(d["draft_id"],0,25);second=service.review_impact(d["draft_id"],25,100)
    check("full_impact_count",first["total"],count)
    check("full_impact_pagination",len(first["rows"])+len(second["rows"]),count)
    await save(d)
    check("inbound_edges_blocked_after_archive",store.neighbors(owner["id"])["total"],0)
    groups=store.query({"operation":"GROUP","kind":"合同","predicate":"客户"})
    check("group_does_not_use_archived_target",all(g["value"].get("object_id")!=owner["id"] for g in groups["groups"]))
    await perform(op("entity",owner["id"],"restore"))
    check("restore_reactivates_eligible_relationships",store.neighbors(owner["id"])["total"],count)
    # Migration is conservative and repeatable.
    obsolete=await prepare(op("entity",pending,"confirm"))
    legacy_mid=store.message("旧版待确认信息",session_id="legacy-review")
    legacy=store.prepare({"objects":[{"ref":"x","existing_id":pending,"name":"测量台","kind":"设备","certain":False,"reason":"身份待核实"}]},legacy_mid)
    store.save_task({"status":"preview","draft_id":legacy["draft_id"],"source_message_ids":[legacy_mid]},session_id="legacy-review")
    store.write(lambda tx:tx.run("MATCH (d:KDData {id:$d}) REMOVE d.review_schema_version WITH d MATCH (o:KDObject {id:$id}) SET o.confirmed=false REMOVE o.review_status",d=store.dataset,id=pending).consume())
    store.initialize_review_schema();rev=store.context()["revision"]
    check("migration_keeps_pending",store.detail(pending)["object"]["confirmed"],False)
    check("migration_invalidates_old_preview",store.draft(obsolete["draft_id"])["status"],"superseded")
    check("migration_clears_obsolete_conversation_grant",store.conversation(service.scope).get("approval") is None and not (store.conversation(service.scope).get("pending") or {}).get("draft_id"))
    legacy_state=store.conversation(ExecutionScope(store.dataset,session_id="legacy-review"))
    check("legacy_migration_keeps_sources_without_obsolete_preview",not legacy_state["pending"].get("draft_id") and legacy_state["pending"]["source_message_ids"]==[legacy_mid])
    store.initialize_review_schema();check("migration_repeat_no_revision_bump",store.context()["revision"],rev)
    # Field edits and confirmation must preserve the same identity invariant.
    mid=store.message("给测量台补编号 ST-17，并确认它的身份。")
    source=[{"message_id":mid,"quote":"给测量台补编号 ST-17，并确认它的身份。"}]
    state=store.conversation(service.scope)
    deps=service.dependencies(state,{"id":mid,"text":source[0]["quote"]})
    deps.entities_seen[pending]=store.detail(pending,history=True)
    proposal=ChangeProposal(task_relation="new_change",source_message_ids=[mid],entities=[{"ref":"x","action":"update","entity_id":pending,"assignments":[{"field":"identifier","value":"ST-17","sources":source}]}],reviews=[op("entity",pending,"confirm",sources=source)])
    draft=service.changes.prepare_change(proposal,deps)
    grant=store.bind_approval(service.scope,draft["draft_id"],str(uuid4()))
    store.approve(service.scope,draft["draft_id"],grant["id"],draft["revision"])
    store.commit(draft["draft_id"],grant["id"],service.scope)
    obj=store.detail(pending)["object"]
    check("combined_field_confirmation_keeps_identifier",obj["identifier"],"ST-17")
    check("combined_field_confirmation_keeps_identity_hash",obj["identity_key"],digest(["设备","","st-17"]))
    # Repeated confirmed content must remain one fact while its subject is pending.
    repeated=commit(store,"计量台价格为3200 CNY，身份待核实",{"objects":[{"ref":"x","name":"计量台","kind":"器材","certain":False,"reason":"身份待核实"}],"facts":[{"subject":"x","predicate":"价格","value":{"type":"number","value":"3200","unit":"CNY"},"evidence":"计量台价格为3200 CNY，身份待核实"}]})
    repeated_id=repeated["objects"][0]["id"]
    commit(store,"再记一次计量台价格3200 CNY",{"objects":[{"ref":"x","name":"计量台","kind":"器材","existing_id":repeated_id,"certain":False,"reason":"身份待核实"}],"facts":[{"subject":"x","predicate":"价格","value":{"type":"number","value":"3200","unit":"CNY"},"evidence":"再记一次计量台价格3200 CNY"}]})
    rows=store.detail(repeated_id)["rows"]
    check("blocked_confirmed_content_not_duplicated",len(rows),1)
    check("blocked_repeat_keeps_both_sources",rows[0]["source_count"],2)
    await perform(op("entity",repeated_id,"confirm"))
    check("endpoint_confirmation_counts_content_once",store.query({"operation":"SUM","kind":"器材","predicate":"价格","unit":"CNY"})["sum"],"3200")
    check("no_numeric_precision_escalation",all(f["value_type"]!="number" or f["numeric_precise"] for f in store.detail(oid)["rows"]))
    store.close()

if __name__=="__main__":
    try: asyncio.run(main())
    except Exception as exc:
        checks.append(dict(name="execution_complete",passed=False,error_type=type(exc).__name__))
        import traceback;traceback.print_exc()
    report=dict(passed=sum(c["passed"] for c in checks),failed=sum(not c["passed"] for c in checks),checks=checks)
    (ROOT/"artifacts"/"review-acceptance.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"Passed {report['passed']}; failed {report['failed']}")
    raise SystemExit(bool(report["failed"]))
