"""Real model semantic lifecycle decisions across names, wording and domains."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from demo.config import Config,load_model_env
from demo.graphiti_extension import Pipeline
from demo.service import Service
from demo.store import Store
from fixtures import commit
checks=[];trace=[]
def check(name,value):
    checks.append(dict(name=name,passed=bool(value)));print(("PASS " if value else "FAIL ")+name,flush=True)
async def main():
    cases=[
        ("周工","人员","北岸研究院","研究机构","已核实周工的身份，按现在的人员分类确认对象，不确认其他信息。","confirm"),
        ("观测仪","设备","月影站","观测站","这台观测仪身份核对无误，确认它是设备，安装关系先保持待确认。","confirm"),
        ("林老师","人员","青禾学苑","学校","林老师在青禾学苑负责联络，补充这条联系人关系。","relation"),
        ("校准台","设备","巡检站","设施","校准台的部署位置是巡检站，补上部署关系，类别设备没有问题。","relation"),
        ("梁工","企业","澄湖公司","企业","梁工之前被误分为企业，他实际是人员，请只更正类型，身份仍待核实。","reclassify"),
        ("测绘仪","人员","西桥站","观测站","测绘仪不是人员，是一件设备。当前分类录错了，只修正分类，不确认身份。","reclassify"),
        ("赵工","人员","和光所","研究机构","只核实这条协作信息为真，暂时不确认赵工身份。","fact_confirm"),
        ("导航仪","设备","远航船","船舶","撤回当前选中的这条安装信息，导航仪对象和其他内容都保留。","retract"),
        ("陆工","人员","霁云所","机构","陆工这条对象记录暂时不用了，整条归档，留着历史。","archive"),
    ]
    for name,kind,target,target_kind,text,action in cases:
        store=Store(Config(uri="bolt://127.0.0.1:17694",password="",dataset="review_agent_"+uuid4().hex));store.initialize()
        seed=commit(store,"待核验的协作信息",{"objects":[{"ref":"x","name":name,"kind":kind,"certain":action=="relation","reason":"身份待核实"},{"ref":"t","name":target,"kind":target_kind}],"facts":[{"subject":"x","predicate":"协作","object":"t","certain":False,"reason":"内容待核实","evidence":"待核验的协作信息"}]})
        oid=seed["objects"][0]["id"];fid=store.detail(oid)["rows"][0]["id"]
        pipe=Pipeline(store.config);service=Service(store,pipe)
        try:
            ref={"target_type":"fact" if action in {"fact_confirm","retract"} else "entity","target_id":fid if action in {"fact_confirm","retract"} else oid,"revision":store.context()["revision"]}
            result=await service.chat(text,context_ref=ref);trace.append(dict(name=name,text=text,result=result))
            d=result.get("draft");check(name+"_preview",bool(d))
            if not d:continue
            reviews=d["reviews"]
            if action=="relation":
                check(name+"_relation_only",bool(d["facts"]) and not d["entity_updates"] and not any(o["action"]=="reclassify" for o in reviews))
                check(name+"_preserves_id",any(o["id"]==oid for o in d["objects"]))
            else:
                expected="confirm" if action=="fact_confirm" else action
                check(name+"_generic_operation",any(o["action"]==expected and o["target_id"]==(fid if action in {"fact_confirm","retract"} else oid) for o in reviews))
                check(name+"_no_recreated_fact",not d["facts"])
                if action=="fact_confirm":check(name+"_no_implicit_endpoint_confirmation",all(o["target_type"]=="fact" for o in reviews))
                if action=="reclassify":check(name+"_no_implicit_confirmation",all(o["action"]!="confirm" for o in reviews))
            await service.confirm(d["draft_id"],d["approval_id"],d["revision"])
            obj=store.detail(oid)["object"]
            if action in {"relation","confirm"}:check(name+"_type_unchanged",obj["kind"]==kind)
            if action=="reclassify":check(name+"_corrected_with_pending_identity",obj["kind"]!=kind and not obj["confirmed"])
            if action=="fact_confirm":check(name+"_content_confirmed_blocked",not obj["confirmed"] and store.detail(oid)["rows"][0]["review_status"]=="confirmed" and store.detail(oid)["rows"][0]["effective_status"]=="blocked")
            if action=="retract":check(name+"_fact_withdrawn_only",not store.detail(oid)["rows"] and not obj["archived"])
            if action=="archive":check(name+"_object_archived",obj["archived"])
            if action=="archive":
                followup=await service.chat("恢复刚才归档的陆工，保留他原来的待确认状态。")
                trace.append(dict(name=name+"_restore",result=followup))
                restored=followup.get("draft")
                check(name+"_restore_preview",bool(restored) and any(o["action"]=="restore" and o["target_id"]==oid for o in restored["reviews"]))
                if restored:
                    await service.confirm(restored["draft_id"],restored["approval_id"],restored["revision"])
                    check(name+"_restore_preserves_pending",not store.detail(oid)["object"]["archived"] and not store.detail(oid)["object"]["confirmed"])
        finally:
            if service._runtime:await service.runtime.close()
            await pipe.close();store.close()
    store=Store(Config(uri="bolt://127.0.0.1:17694",password="",dataset="review_ambiguity_"+uuid4().hex));store.initialize()
    seed=commit(store,"两台同名设备待核实",{"objects":[{"ref":"a","name":"光谱仪","kind":"设备","certain":False,"reason":"身份待核实"},{"ref":"b","name":"光谱仪","kind":"设备","certain":False,"reason":"身份待核实"}]})
    pipe=Pipeline(store.config);service=Service(store,pipe)
    try:
        result=await service.chat("把光谱仪的对象身份确认一下。")
        trace.append(dict(name="ambiguous_target",result=result))
        check("ambiguous_target_requires_clarification",result.get("clarification_required") and not result.get("draft"))
        check("ambiguous_target_no_business_write",all(not store.detail(o["id"])["object"]["confirmed"] for o in seed["objects"]))
        oid=seed["objects"][1]["id"]
        result=await service.chat("就是当前选中的这一台，确认它的设备身份，其他记录不动。",context_ref={"target_type":"entity","target_id":oid,"revision":store.context()["revision"]})
        trace.append(dict(name="selected_target",result=result))
        d=result.get("draft")
        check("selected_reference_resolves_ambiguity",bool(d) and len(d["reviews"])==1 and d["reviews"][0]["target_id"]==oid and d["reviews"][0]["action"]=="confirm")
        if d:
            await service.confirm(d["draft_id"],d["approval_id"],d["revision"])
            check("selected_reference_only_changes_target",store.detail(oid)["object"]["confirmed"] and not store.detail(seed["objects"][0]["id"])["object"]["confirmed"])
    finally:
        if service._runtime:await service.runtime.close()
        await pipe.close();store.close()
if __name__=="__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--model-env",required=True);parser.add_argument("--repeat",type=int,default=1);parser.add_argument("--report-label",default="");args=parser.parse_args()
    assert 1<=args.repeat<=5 and all(c.isascii() and (c.isalnum() or c in "-_") for c in args.report_label)
    load_model_env(args.model_env);os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED","false")
    async def repeated():
        for _ in range(args.repeat):await main()
    try:asyncio.run(repeated())
    except Exception as exc:
        import traceback;traceback.print_exc();checks.append(dict(name="execution_complete",passed=False,error_type=type(exc).__name__))
    report=dict(passed=sum(c["passed"] for c in checks),failed=sum(not c["passed"] for c in checks),repeat=args.repeat,checks=checks,trace=trace)
    (ROOT/"artifacts"/("review-agent-"+args.report_label+".json" if args.report_label else "review-agent-acceptance.json")).write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"Passed {report['passed']}; failed {report['failed']}");raise SystemExit(bool(report["failed"]))
