"""Real Neo4j acceptance with a CSV oracle and optional real-model/Graphiti runs.

No in-memory database or silent mock. Writes use unique acceptance_* datasets.
The default endpoint is a disposable local test container, never port 7687.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from demo.config import Config, load_model_env
from demo.model import DemoError, Query, Value
from demo.store import Store
from fixtures import commit, contract_change, seed_truth, truth_rows


def query(store, operation="COUNT", **kwargs):
    return store.query({"operation": operation, "kind": "合同", **kwargs})


def run_structured(store, check):
    drafts = []
    for identifier, customer, amount, status in [("A", "甲公司", "200000", "审核中"), ("B", "甲公司", "300000", "已完成"), ("C", "乙公司", "100000", "审核中")]:
        drafts.append(commit(store, *contract_change(identifier, customer, amount, status)))
    check("sample_count", query(store)["count"], 3)
    check("sample_filtered_count", query(store, filters=[{"predicate": "状态", "value": {"type": "text", "value": "审核中"}}])["count"], 2)
    groups = query(store, "GROUP", predicate="客户")["groups"]
    check("sample_group", {g["value"]["value"]:g["count"] for g in groups}, {"甲公司":2,"乙公司":1})
    check("sample_sum", query(store, "SUM", predicate="金额", unit="CNY")["sum"], "600000")
    msg = "甲公司的合同 A 金额从 20 万更正为 25 万元人民币。"
    correction = {"objects": [{"ref":"a","name":"合同 A","kind":"合同","identifier":"A","namespace":"甲公司"}],
                  "facts":[{"subject":"a","predicate":"金额","value":{"type":"number","value":"250000","unit":"CNY"},"mode":"replace","evidence":msg}]}
    corrected = commit(store, msg, correction)
    check("correction_sum", query(store,"SUM",predicate="金额",unit="CNY")["sum"], "650000")
    check("correction_count", query(store)["count"], 3)
    oid = corrected["objects"][0]["id"]
    history = store.detail(oid, history=True)
    money = [f for f in history["rows"] if f["predicate"] == "金额"]
    check("history_current_and_expired", sorted(f["active"] for f in money), [False,True])
    # A repeat with the current value adds evidence, not a fourth object.
    repeat = commit(store, *contract_change("A","甲公司","250000"))
    check("repeat_count", query(store)["count"], 3)
    sid = next(f["id"] for f in store.detail(oid)["rows"] if f["predicate"] == "金额")
    check("duplicate_evidence_sources", store.sources(sid)["total"], 2)
    # A second confirmation of the frozen draft is idempotent under concurrency.
    mid = store.message("甲公司有合同 F，金额 1 元人民币。")
    message, change = contract_change("F","甲公司","1")
    mid = store.message(message)
    pending = store.prepare(change, mid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.commit(pending["draft_id"]), range(4)))
    check("concurrent_confirm_once", sum(not r["idempotent"] for r in results), 1)
    check("after_concurrency_count", query(store)["count"], 4)
    # An uncertain value cannot invalidate an existing precise current value.
    uncertain_msg, uncertain_change = contract_change("A","甲公司","大约二十多万",uncertain=True)
    commit(store, uncertain_msg, uncertain_change)
    check("uncertain_does_not_replace", query(store,"SUM",predicate="金额",unit="CNY")["sum"], "650001")
    commit(store, *contract_change("D","甲公司","大约二十多万",uncertain=True))
    sum_result = query(store,"SUM",predicate="金额",unit="CNY")
    check("uncertain_sum_excluded", (sum_result["sum"],sum_result["excluded_count"]), ("650001",1))
    commit(store, *contract_change("E","乙公司","100",unit="USD"))
    mixed = query(store,"SUM",predicate="金额",unit="CNY")
    check("currencies_not_added", (mixed["sum"],mixed["excluded_count"]), ("650001",2))
    check("usd_only",query(store,"SUM",predicate="金额",unit="USD")["sum"],"100")
    msg = "甲公司可能还有几份合同。"
    commit(store,msg,{"objects":[{"ref":"unknown","name":"甲公司可能存在的其他合同","kind":"合同","certain":False,"reason":"无明确数量或对象身份"}],"facts":[]})
    check("uncertain_identity_not_counted",query(store)["count"],6)
    msg = "仓库有设备 E1。"
    equipment = commit(store,msg,{"objects":[{"ref":"e","name":"设备 E1","kind":"设备","identifier":"E1"}],"facts":[]})
    check("new_type_discovered", "设备" in [r["kind"] for r in store.types()["rows"]],True)
    check("equipment_not_contract",query(store)["count"],6)
    # Alias must be explicitly resolved by the user; no fuzzy automatic merge.
    msg="这份合同简称甲主合同。"
    alias={"objects":[{"ref":"a","name":"甲主合同","kind":"合同","existing_id":oid}],"facts":[]}
    commit(store,msg,alias)
    check("explicit_alias_no_new_object",query(store)["count"],6)
    mid=store.message("甲主合同。")
    preview=store.prepare({"objects":[{"ref":"a","name":"甲主合同","kind":"合同"}],"facts":[]},mid)
    check("alias_is_candidate",preview["objects"][0]["resolved"],False)
    try:
        store.commit(preview["draft_id"])
        check("unresolved_commit_rejected",False,True)
    except DemoError as exc:
        check("unresolved_commit_rejected",exc.code,"identity_unresolved")
    resolved=store.resolve(preview["draft_id"],{"a":oid})
    store.commit(resolved["draft_id"])
    check("resolved_alias_reuses_identity",query(store)["count"],6)
    company_id = drafts[0]["objects"][0]["id"]
    b_id = next(o["id"] for o in drafts[1]["objects"] if o["kind"] == "合同")
    multi_message="甲公司同时拥有合同 A 和合同 B。"
    multi_change={"objects":[{"ref":"owner","name":"甲公司","kind":"客户","existing_id":company_id},
                             {"ref":"a","name":"合同 A","kind":"合同","existing_id":oid},
                             {"ref":"b","name":"合同 B","kind":"合同","existing_id":b_id}],
                  "facts":[{"subject":"owner","predicate":"拥有","object":"a","cardinality":"many","evidence":multi_message},
                           {"subject":"owner","predicate":"拥有","object":"b","cardinality":"many","evidence":multi_message}]}
    commit(store,multi_message,multi_change)
    incoming=query(store,"GROUP",predicate="拥有",direction="in")
    check("inbound_group_and_multi_relation",{g["value"]["value"]:g["count"] for g in incoming["groups"]},{"甲公司":2,"未确定":4})
    check("inbound_filter",query(store,filters=[{"predicate":"拥有","object_id":company_id,"direction":"in"}])["count"],2)
    second=next(o["id"] for o in drafts[2]["objects"] if o["kind"]=="客户")
    msg="乙公司也共同拥有合同 A。"
    commit(store,msg,{"objects":[{"ref":"owner","name":"乙公司","kind":"客户","existing_id":second},{"ref":"a","name":"合同 A","kind":"合同","existing_id":oid}],
                      "facts":[{"subject":"owner","predicate":"拥有","object":"a","cardinality":"many","evidence":msg}]})
    ambiguous=query(store,"GROUP",predicate="拥有",direction="in")
    check("multiple_owners_not_double_counted",{g["value"]["value"]:g["count"] for g in ambiguous["groups"]},{"甲公司":1,"未确定":5})
    # Historical query participant IDs and exclusions remain audit-accessible.
    audit=store.audit(mixed["query_id"],limit=100)
    check("audit_includes_all_population",audit["total"],mixed["population_count"])
    check("sources_preserved",store.sources(sid)["rows"][0]["quote"] in store.sources(sid)["rows"][0]["message"],True)
    page=store.objects("合同",limit=2)
    commit(store,"增加设备 E2。",{"objects":[{"ref":"e","name":"设备 E2","kind":"设备","identifier":"E2"}],"facts":[]})
    try:
        store.objects("合同",offset=2,limit=2,revision=page["revision"])
        check("page_revision_guard",False,True)
    except DemoError as exc:
        check("page_revision_guard",exc.code,"data_changed")
    # Frozen preview cannot silently apply after another write.
    message,change=contract_change("G","乙公司","5")
    stale=store.prepare(change,store.message(message))
    commit(store,"增加设备 E3。",{"objects":[{"ref":"e","name":"设备 E3","kind":"设备","identifier":"E3"}],"facts":[]})
    try:
        store.commit(stale["draft_id"])
        check("stale_preview_rejected",False,True)
    except DemoError as exc:
        check("stale_preview_rejected",exc.code,"preview_stale")
    try:
        store.neighbors(oid,depth=4,max_depth=4)
        check("depth_guard",False,True)
    except DemoError as exc:
        check("depth_guard",exc.code,"max_depth")
    check("decimal_fixed_precision",Value(type="number",value="0.1").fields()["number_scaled"]+Value(type="number",value="0.2").fields()["number_scaled"],300000)
    for name,value in [("tiny_decimal_rejected","1e-100000000"),("precision_tail_rejected","999999999.9999990000000000001")]:
        try:
            Value(type="number",value=value)
            check(name,False,True)
        except (ValueError,DemoError):
            check(name,True,True)
    try:
        Query.model_validate({"operation":"COUNT","cypher":"MATCH (n) DETACH DELETE n"})
        check("free_cypher_rejected",False,True)
    except ValueError:
        check("free_cypher_rejected",True,True)
    count_before=query(store)["count"]
    different_scope=commit(store,*contract_change("A","乙公司","5"))
    check("same_identifier_different_namespace",query(store)["count"],count_before+1)
    check("scoped_identifiers_have_distinct_ids",different_scope["objects"][1]["id"]!=oid,True)
    bare=store.prepare({"objects":[{"ref":"a","name":"合同 A","kind":"合同","identifier":"A"}],"facts":[]},store.message("合同 A。"))
    check("unscoped_duplicate_identifier_requires_choice",bare["objects"][0]["resolved"],False)
    return drafts[0]


def run_completeness(store,check):
    rows=truth_rows()
    seed_truth(store)
    # Independent fixed oracle guards accidental edits in fixture generation.
    check("oracle_size",len(rows),105)
    check("oracle_sum",sum(Decimal(r["amount"]) for r in rows),Decimal("556500"))
    check("all_105_count",query(store)["count"],105)
    check("all_105_sum",query(store,"SUM",predicate="金额",unit="CNY")["sum"],"556500")
    check("all_105_filtered",query(store,filters=[{"predicate":"状态","value":{"type":"text","value":"审核中"}}])["count"],53)
    groups=query(store,"GROUP",predicate="客户")["groups"]
    check("all_105_group",{g["value"]["value"]:g["count"] for g in groups},{"客户甲":35,"客户乙":35,"客户丙":35})
    found,revision,offset=[],None,0
    while True:
        page=store.objects("合同",offset=offset,limit=7,revision=revision)
        revision=page["revision"]
        found.extend(page["rows"])
        if not page["has_more"]:break
        offset+=7
    check("page_set_matches_csv",sorted(o["identifier"] for o in found),sorted(r["identifier"] for r in rows))
    check("page_unique_ids",len({o["id"] for o in found}),105)
    check("same_name_distinct_ids",len([o for o in found if o["name"]=="同名合同"]),10)
    # Repeated import and aliases preserve the exact oracle object set.
    for row in rows[:10]:commit(store,*contract_change(row["identifier"],row["customer"],row["amount"],row["status"],row["name"]))
    check("duplicate_import_count",query(store)["count"],105)
    alias_target=found[0]
    commit(store,"这一份简称复核合同。",{"objects":[{"ref":"a","name":"复核合同","kind":"合同","existing_id":alias_target["id"]}],"facts":[]})
    check("bulk_alias_keeps_oracle_count",query(store)["count"],105)
    alias_preview=store.prepare({"objects":[{"ref":"a","name":"复核合同","kind":"合同"}],"facts":[]},store.message("复核合同。"))
    check("bulk_alias_detected",alias_target["id"] in [o["id"] for o in alias_preview["objects"][0]["candidates"]],True)
    clients=store.objects("客户")["rows"]
    client=next(o for o in clients if o["name"]=="客户甲")
    rels,offset,revision=[],0,None
    while True:
        page=store.neighbors(client["id"],offset=offset,limit=4,revision=revision)
        rels+=page["rows"]
        revision=page["revision"]
        if not page["has_more"]:break
        offset+=4
    check("all_neighbors_not_top_k",len(rels),35)
    check("neighbor_unique_ids",len({r["object"]["id"] for r in rels}),35)
    expected={r["identifier"] for r in rows if r["customer"]=="客户甲"}
    check("neighbor_matches_oracle",sorted(r["object"]["identifier"] for r in rels),sorted(expected))
    # All types can be enumerated even when one page contains only one kind.
    kinds=[];offset=0;revision=None
    while True:
        page=store.types(offset=offset,limit=1,revision=revision)
        kinds.extend(r["kind"] for r in page["rows"]);revision=page["revision"]
        if not page["has_more"]:break
        offset+=1
    check("type_paging",sorted(kinds),["合同","客户"])
    # Query LIST pages, separately from object-browser pages.
    identifiers=[];offset=0;revision=None
    while True:
        page=query(store,"LIST",offset=offset,limit=9,revision=revision)
        identifiers.extend(o["identifier"] for o in page["rows"]);revision=page["revision"]
        if not page["has_more"]:break
        offset+=9
    check("list_complete",sorted(identifiers),sorted(r["identifier"] for r in rows))


async def run_natural(store,check,projection):
    from demo.graphiti_extension import Pipeline
    from demo.service import Service
    pipeline=Pipeline(store.config);service=Service(store,pipeline)
    trace=[]
    try:
        messages=["甲公司有合同 A，金额 20 万元人民币，审核中。","甲公司还有合同 B，金额 30 万元人民币，已完成。","乙公司有合同 C，金额 10 万元人民币，审核中。"]
        for message in messages:
            result=await service.chat(message)
            trace.append({"message":message,"result":result})
            require_draft=result.get("draft")
            check("natural_preview_"+str(len(trace)),bool(require_draft),True)
            if require_draft:
                check("natural_identity_resolved_"+str(len(trace)),all(o["resolved"] for o in require_draft["objects"]),True)
                await service.confirm(require_draft["draft_id"],require_draft["approval_id"],require_draft["revision"])
        check("natural_count",query(store)["count"],3)
        check("natural_sum",query(store,"SUM",predicate="金额",unit="CNY")["sum"],"600000")
        result=await service.chat("合同 A 金额更正为 25 万元人民币。")
        trace.append({"message":"合同 A 金额更正为 25 万元人民币。","result":result})
        check("natural_correction_preview",bool(result.get("draft")),True)
        if result.get("draft"):await service.confirm(result["draft"]["draft_id"],result["draft"]["approval_id"],result["draft"]["revision"])
        check("natural_corrected_sum",query(store,"SUM",predicate="金额",unit="CNY")["sum"],"650000")
        for question,operation,expected in [("目前记录了多少份合同？","COUNT",3),("审核中的合同有多少份？","COUNT",2),("合同金额总计多少人民币？","SUM","650000")]:
            result=await service.chat(question);trace.append({"message":question,"result":result})
            output=result.get("result",{})
            check("natural_query_"+operation,output.get("count" if operation=="COUNT" else "sum"),expected)
        result=await service.chat("按所属公司统计合同数量。")
        trace.append({"message":"按所属公司统计合同数量。","result":result})
        check("natural_group",{g["value"]["value"]:g["count"] for g in result.get("result",{}).get("groups",[])},{"甲公司":2,"乙公司":1})
        result=await service.chat("列出目前记录的所有合同。")
        trace.append({"message":"列出目前记录的所有合同。","result":result})
        check("natural_list_complete",(result.get("result",{}).get("operation"),len(result.get("result",{}).get("rows",[]))),("LIST",3))
        result=await service.chat("甲公司的合同 A。")
        trace.append({"message":"甲公司的合同 A。","result":result})
        if result.get("draft") and all(o["resolved"] for o in result["draft"]["objects"]):await service.confirm(result["draft"]["draft_id"],result["draft"]["approval_id"],result["draft"]["revision"])
        check("natural_repeat_no_fourth",query(store)["count"],3)
        result=await service.chat("甲公司可能还有几份合同。")
        trace.append({"message":"甲公司可能还有几份合同。","result":result})
        if result.get("draft") and all(o["resolved"] for o in result["draft"]["objects"]):await service.confirm(result["draft"]["draft_id"],result["draft"]["approval_id"],result["draft"]["revision"])
        check("natural_vague_count_unchanged",query(store)["count"],3)
        result=await service.chat("合同 D 大约二十多万元。")
        trace.append({"message":"合同 D 大约二十多万元。","result":result})
        if result.get("draft") and all(o["resolved"] for o in result["draft"]["objects"]):await service.confirm(result["draft"]["draft_id"],result["draft"]["approval_id"],result["draft"]["revision"])
        check("natural_approx_amount_excluded",query(store,"SUM",predicate="金额",unit="CNY")["sum"],"650000")
        count_before_equipment=query(store)["count"]
        result=await service.chat("仓库有设备 E1。")
        trace.append({"message":"仓库有设备 E1。","result":result})
        if result.get("draft") and all(o["resolved"] for o in result["draft"]["objects"]):await service.confirm(result["draft"]["draft_id"],result["draft"]["approval_id"],result["draft"]["revision"])
        check("natural_new_type", "设备" in [r["kind"] for r in store.types()["rows"]],True)
        check("natural_new_type_does_not_change_contracts",query(store)["count"],count_before_equipment)
        if projection:
            before=query(store,"SUM",predicate="金额",unit="CNY")["sum"]
            job=store.pending_projections()[0]
            await pipeline.project(job,store)
            first=store.draft(job["id"])
            check("real_graphiti_projection",first["projection_status"],"ready")
            await pipeline.project(job,store)
            check("projection_retry_same_episode",store.draft(job["id"])["episode_id"],first["episode_id"])
            check("projection_does_not_change_sum",query(store,"SUM",predicate="金额",unit="CNY")["sum"],before)
            ghost_id="acceptance-ghost-"+uuid4().hex
            graphiti=await pipeline.graphiti()
            await graphiti.driver.execute_query("CREATE (:Entity {uuid:$id,name:'幽灵合同',group_id:$group})",params={"id":ghost_id,"group":store.config.database})
            check("semantic_ghost_not_counted",query(store)["count"],count_before_equipment)
    finally:
        (ROOT/"artifacts").mkdir(exist_ok=True)
        (ROOT/"artifacts"/"natural-trace.json").write_text(json.dumps(trace,ensure_ascii=False,indent=2),encoding="utf-8")
        await service.runtime.close()
        await pipeline.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--uri",default="bolt://127.0.0.1:17694")
    parser.add_argument("--password",default="")
    parser.add_argument("--natural",action="store_true")
    parser.add_argument("--projection",action="store_true")
    parser.add_argument("--model-env")
    args=parser.parse_args()
    if not args.uri.startswith("bolt://127.0.0.1:17694"):
        raise SystemExit("Acceptance only accepts its dedicated local test endpoint on 17694.")
    if args.model_env:
        load_model_env(args.model_env)
    os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED","false")
    os.environ.setdefault("SEMAPHORE_LIMIT","3")
    base="acceptance_"+uuid4().hex
    records=[]
    def check(name,actual,expected):
        passed=actual==expected
        records.append({"name":name,"passed":passed,"actual":actual,"expected":expected})
        print(("PASS " if passed else "FAIL ")+name,flush=True)
    stores=[]
    started=time.time()
    try:
        for suffix in ("examples","truth","natural"):
            store=Store(Config(uri=args.uri,password=args.password,dataset=base+"_"+suffix));store.initialize();stores.append(store)
        run_structured(stores[0],check)
        run_completeness(stores[1],check)
        if args.natural:asyncio.run(run_natural(stores[2],check,args.projection))
    except Exception as exc:
        records.append({"name":"execution_error","passed":False,"actual":type(exc).__name__,"expected":"complete"})
        # Traceback is useful during development, but never print provider exception strings.
        import traceback
        for frame in traceback.extract_tb(exc.__traceback__):print(f"  {Path(frame.filename).name}:{frame.lineno} {frame.name}",flush=True)
        print("Execution error type: "+type(exc).__name__,flush=True)
        if isinstance(exc,DemoError):print(exc.code+": "+str(exc),flush=True)
    finally:
        for store in stores:store.close()
        report={"passed":sum(r["passed"] for r in records),"failed":sum(not r["passed"] for r in records),
                "natural_requested":args.natural,"projection_requested":args.projection,"seconds":round(time.time()-started,2),"results":records}
        (ROOT/"artifacts").mkdir(exist_ok=True)
        (ROOT/"artifacts"/("acceptance-natural.json" if args.natural else "acceptance.json")).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")
        print(f"Passed {report['passed']}; failed {report['failed']}",flush=True)
    raise SystemExit(1 if report["failed"] else 0)


if __name__=="__main__":main()
