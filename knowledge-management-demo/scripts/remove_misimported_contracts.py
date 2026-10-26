"""One-off, explicitly authorized cleanup of the 40 name-only HT contracts.

No delete API/tool is added. Freeze the owned app, back up the scoped database,
delete only the verified original objects, invalidate their previews/index jobs,
and restart. Never regenerate an import or confirm any new business record.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart, TextPart
from pydantic_core import to_jsonable_python
from demo.config import Config
from demo.contracts import ExecutionScope
from demo.model import digest, encoded
from demo.store import Store

DELETE_REQUEST = "彻底删除之前误录的这40份合同，"
MANUAL_REQUEST = "不要再重新生成完整的 45 行待确认预览。 我要从头手动处理"
RECEIPT = "已彻底删除误录的 HT-001～HT-040 共40份合同，并取消引用它们的待确认预览。没有生成新的45行预览；其他原有记录保持不变，可以从头手动处理。"


def scoped_snapshot(tx, dataset):
    nodes = [dict(r) for r in tx.run(
        "MATCH (n) WHERE any(label IN labels(n) WHERE label STARTS WITH 'KD') "
        "AND (n.dataset=$d OR (n:KDData AND n.id=$d)) "
        "RETURN elementId(n) AS element_id,labels(n) AS labels,properties(n) AS properties ORDER BY element_id", d=dataset)]
    ids = [n["element_id"] for n in nodes]
    edges = [dict(r) for r in tx.run(
        "MATCH (a)-[r]->(b) WHERE elementId(a) IN $ids AND elementId(b) IN $ids "
        "RETURN elementId(a) AS source,elementId(b) AS target,type(r) AS type,properties(r) AS properties ORDER BY source,target,type",
        ids=ids)]
    return {"nodes": nodes, "edges": edges}


def original_targets(tx, dataset, manifest):
    expected = {o["id"]:o for o in manifest["objects"] if o.get("identifier") in {f"HT-{i:03d}" for i in range(1,41)}}
    assert len(expected) == 40, "Backup manifest must identify exactly 40 original objects"
    objects = [dict(r["o"]) for r in tx.run(
        "MATCH (o:KDObject {dataset:$d}) WHERE o.identifier IN $codes RETURN properties(o) AS o ORDER BY o.identifier",
        d=dataset, codes=[f"HT-{i:03d}" for i in range(1,41)])]
    assert len(objects) == 40 and {o["id"] for o in objects} == set(expected), "Target IDs/count changed; refusing cleanup"
    assert all(o == expected[o["id"]] and o["kind"] == "合同" for o in objects), "Original target fields changed"
    ids = [o["id"] for o in objects]
    assert tx.run("MATCH (f:KDFact {dataset:$d}) WHERE f.subject_id IN $ids OR f.object_id IN $ids RETURN count(f) AS n",
                  d=dataset, ids=ids).single()["n"] == 0, "A target has a fact/relation; refusing deletion"
    assert tx.run("MATCH (o:KDObject {dataset:$d})-[r]-() WHERE o.id IN $ids AND type(r)<>'KD_SUPPORTS' RETURN count(r) AS n",
                  d=dataset, ids=ids).single()["n"] == 0, "Unexpected business relationships exist"
    original = []
    for r in tx.run("MATCH (p:KDDraft {dataset:$d,status:'committed'}) RETURN p.id AS id,p.payload AS payload", d=dataset):
        body = json.loads(r["payload"])
        if {o["id"] for o in body.get("objects", [])} == set(ids) and not body.get("facts"):
            original.append(r["id"])
    assert len(original) == 1, "Expected exactly one original name-only import"
    return objects


def stop_owned(pid):
    assert Path(f"/proc/{pid}/cwd").resolve() == ROOT.resolve()
    assert b"-m\x00demo\x00" in Path(f"/proc/{pid}/cmdline").read_bytes()
    assert f"pid={pid}," in subprocess.check_output(["ss","-ltnp","sport = :8096"], text=True)
    os.kill(pid, signal.SIGINT)
    for _ in range(25):
        if f"pid={pid}," not in subprocess.check_output(["ss","-ltnp","sport = :8096"], text=True):
            return
        time.sleep(1)
    assert Path(f"/proc/{pid}/cwd").resolve() == ROOT.resolve()
    os.kill(pid, signal.SIGTERM)
    for _ in range(10):
        if f"pid={pid}," not in subprocess.check_output(["ss","-ltnp","sport = :8096"], text=True):
            return
        time.sleep(1)
    raise RuntimeError("Owned application did not stop")


def cleanup(store, scope, manifest, backup_path, event_id):
    def write(tx):
        revision = store.lock(tx, manifest["revision"])
        targets = original_targets(tx, store.dataset, manifest)
        ids = {o["id"] for o in targets}
        before = scoped_snapshot(tx, store.dataset)
        backup = {"event_id":event_id, "authorization":[DELETE_REQUEST,MANUAL_REQUEST], "revision":revision,
                  "dataset":store.dataset, "targets":targets, **before}
        # A failed backup write aborts the database transaction.
        with backup_path.open("x", encoding="utf-8") as stream:
            json.dump(backup, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        affected = []
        for r in tx.run("MATCH (p:KDDraft {dataset:$d}) RETURN p.id AS id,p.payload AS payload,p.status AS status", d=store.dataset):
            body = json.loads(r["payload"])
            referenced = {o["id"] for o in [*body.get("objects",[]),*body.get("review_objects",[])]}
            if referenced & ids:
                affected.append(r["id"])
        tx.run("MATCH (p:KDDraft {dataset:$d}) WHERE p.id IN $ids "
               "SET p.previous_status=p.status,p.status=CASE WHEN p.status='committed' THEN 'voided' ELSE 'superseded' END,"
               "p.projection_status='cancelled',p.invalidated_by=$event,p.invalidated_at=$now "
               "REMOVE p.error_code,p.retry_at", d=store.dataset, ids=affected, event=event_id, now=time.time()).consume()
        tx.run("MATCH (a:KDApproval {dataset:$d}) WHERE a.draft_id IN $ids SET a.status='revoked',a.revoked_by=$event",
               d=store.dataset, ids=affected, event=event_id).consume()
        for r in tx.run("MATCH (j:KDImport {dataset:$d}) RETURN j.id AS id,j.payload AS payload", d=store.dataset):
            job = json.loads(r["payload"])
            if job.get("draft_id") in affected or set(job.get("entity_refs",{}).values()) & ids:
                job.update(status="cancelled", processed_rows=0, chunks=[], draft_id="", cancelled_by=event_id,
                           entity_refs={k:v for k,v in job.get("entity_refs",{}).items() if v not in ids})
                tx.run("MATCH (j:KDImport {id:$id,dataset:$d}) SET j.payload=$body,j.updated_at=$now",
                       id=r["id"], d=store.dataset, body=encoded(job), now=time.time()).consume()
        history_additions = to_jsonable_python([
            ModelRequest(parts=[UserPromptPart(content=DELETE_REQUEST),UserPromptPart(content=MANUAL_REQUEST)]),
            ModelResponse(parts=[TextPart(content=RECEIPT)])])
        for r in tx.run("MATCH (c:KDConversation {dataset:$d}) RETURN c.id AS id,c.execution AS execution,c.draft_id AS draft", d=store.dataset):
            if not r["execution"]:
                continue
            state = json.loads(r["execution"])
            pending = state.get("pending") or {}
            impacted = bool(set(state.get("focus_refs",[])) & ids or pending.get("draft_id") in affected)
            if not impacted and r["id"] != digest([store.dataset,scope.session_id]):
                continue
            state.update(version=state["version"]+1, pending=None, approval=None, clarification=None,
                         focus_refs=[i for i in state.get("focus_refs",[]) if i not in ids],
                         last_response={"message":RECEIPT,"clear_draft":True}, request_id=event_id,
                         last_maintenance={"id":event_id,"revision":revision+1}, last_tool_log=[])
            for field in ("selection","selection_record","current_draft"):
                state.pop(field,None)
            if (state.get("last_commit") or {}).get("draft_id") in affected:
                state.pop("last_commit",None)
            state["history"] = [*state.get("history",[]),*history_additions]
            tx.run("MATCH (c:KDConversation {id:$id,dataset:$d}) SET c.execution=$state,c.task='',c.draft_id='',c.updated_at=$now",
                   id=r["id"], d=store.dataset, state=encoded(state), now=time.time()).consume()
        tx.run("MATCH (q:KDQuery {dataset:$d}) WHERE any(i IN q.participants WHERE i IN $ids) "
               "SET q.invalidated_by=$event,q.invalidated_reason='Referenced records permanently deleted by user'",
               d=store.dataset, ids=list(ids), event=event_id).consume()
        tx.run("MATCH (o:KDObject {dataset:$d}) WHERE o.id IN $ids DETACH DELETE o", d=store.dataset, ids=list(ids)).consume()
        tx.run("MATCH (d:KDData {id:$d}) SET d.revision=d.revision+1", d=store.dataset).consume()
        tx.run("CREATE (:KDMutation {id:$id,dataset:$d,created_at:$now,change:$change})", id=event_id, d=store.dataset, now=time.time(),
               change=encoded({"action":"permanent_delete", "entities":sorted(ids), "identifiers":[o["identifier"] for o in targets],
                               "actor":scope.actor, "authorization":[DELETE_REQUEST,MANUAL_REQUEST], "backup":str(backup_path),
                               "invalidated_drafts":affected, "regenerate_preview":False})).consume()
        for role,text in [("user",DELETE_REQUEST),("user",MANUAL_REQUEST),("assistant",RECEIPT)]:
            tx.run("CREATE (:KDMessage {id:$id,dataset:$d,session_id:$s,role:$role,text:$text,created_at:$now})",
                   id=str(uuid4()), d=store.dataset, s=scope.session_id, role=role, text=text, now=time.time()).consume()
        after = scoped_snapshot(tx, store.dataset)
        def props(snapshot,label):
            return {n["properties"]["id"]:n["properties"] for n in snapshot["nodes"] if label in n["labels"]}
        assert props(after,"KDObject") == {k:v for k,v in props(before,"KDObject").items() if k not in ids}
        assert props(after,"KDFact") == props(before,"KDFact")
        assert props(after,"KDSource") == props(before,"KDSource")
        removed_elements = {n["element_id"] for n in before["nodes"] if n["properties"].get("id") in ids}
        assert after["edges"] == [e for e in before["edges"] if e["source"] not in removed_elements and e["target"] not in removed_elements]
        assert len(props(after,"KDObject")) == 8 and len(props(after,"KDFact")) == 16
        assert store.lock(tx) == revision+1
        return {"deleted":len(ids), "remaining_objects":8, "remaining_facts":16, "revision":revision+1,
                "invalidated_drafts":len(affected), "regenerate_preview":False, "backup":str(backup_path),
                "other_objects_facts_sources_unchanged":True, "event_id":event_id}
    return store.write(write)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--pid",type=int,required=True)
    parser.add_argument("--manifest",required=True)
    parser.add_argument("--model-env",required=True)
    parser.add_argument("--confirmation",required=True)
    args=parser.parse_args()
    assert args.confirmation == "DELETE HT-001..HT-040"
    load_dotenv(ROOT/".env")
    config=Config.from_env()
    assert config.uri == "bolt://127.0.0.1:17693" and config.dataset == "default"
    manifest=json.loads(Path(args.manifest).read_text())
    store=Store(config);scope=ExecutionScope(config.dataset);scope.check(store,"confirm")
    # Verify exact targets BEFORE stopping the app; transaction repeats checks.
    store.read(lambda tx:original_targets(tx,config.dataset,manifest))
    event_id=str(uuid4())
    backup_path=ROOT/"artifacts"/("before-delete-40-"+event_id+".json")
    stop_owned(args.pid)
    try:
        report=cleanup(store,scope,manifest,backup_path,event_id)
        (ROOT/"artifacts/delete-40-result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps(report,ensure_ascii=False),flush=True)
    finally:
        store.close()
        env={k:v for k,v in os.environ.items() if v or not k.startswith(("LLM_","EMBEDDING_"))}
        env.update(KD_PORT="8096",GRAPHITI_TELEMETRY_ENABLED="false")
        with (ROOT/"artifacts/live-after-delete-40.log").open("a") as log:
            process=subprocess.Popen([sys.executable,"-u","-m","demo","--model-env",args.model_env],cwd=ROOT,env=env,
                                     stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        (ROOT/"artifacts/live-after-delete-40-process.json").write_text(json.dumps({"pid":process.pid,"port":8096})+"\n")
        for _ in range(40):
            try:
                with urllib.request.urlopen("http://127.0.0.1:8096/api/health",timeout=2) as r:
                    assert json.load(r)["ok"]
                print("Application restarted PID="+str(process.pid),flush=True)
                break
            except Exception:
                if process.poll() is not None:raise RuntimeError("App exited; inspect preserved maintenance log")
                time.sleep(1)
        else:raise RuntimeError("App not ready; inspect preserved maintenance log")


if __name__ == "__main__":
    main()
