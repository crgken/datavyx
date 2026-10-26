"""Preserve live canonical data while replacing only the owned local demo PID."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv
from demo.config import Config
from demo.store import Store

def snapshot():
    # Never initialize/migrate as part of a backup read.
    store=Store(Config.from_env())
    def read(tx):
        meta=tx.run("MATCH (d:KDData {id:$d}) RETURN d.revision AS r,d.review_schema_version AS v",d=store.dataset).single()
        result=dict(dataset=store.dataset,revision=meta["r"],review_schema_version=meta["v"])
        for key,label in [("objects","KDObject"),("facts","KDFact"),("sources","KDSource")]:
            result[key]=[dict(r["n"]) for r in tx.run(f"MATCH (n:{label} {{dataset:$d}}) RETURN properties(n) AS n ORDER BY n.id",d=store.dataset)]
        result["supports"]=[dict(r) for r in tx.run("MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(n) RETURN s.id AS source_id,n.id AS target_id,properties(e) AS evidence ORDER BY source_id,target_id",d=store.dataset)]
        return result
    try:return store.read(read)
    finally:store.close()

def compare(before,after):
    checks={"dataset_unchanged":before["dataset"]==after["dataset"],"revision_migrated_once":after["revision"]==before["revision"]+(0 if before.get("review_schema_version")==3 else 1),
            "sources_unchanged":before["sources"]==after["sources"],"support_quotes_unchanged":before["supports"]==after["supports"]}
    objects={o["id"]:o for o in after["objects"]};facts={f["id"]:f for f in after["facts"]}
    checks["object_ids_unchanged"]=set(objects)=={o["id"] for o in before["objects"]}
    checks["fact_ids_unchanged"]=set(facts)=={f["id"] for f in before["facts"]}
    checks["object_fields_unchanged"]=all(all(objects[o["id"]].get(k)==v for k,v in o.items()) for o in before["objects"])
    checks["fact_fields_unchanged"]=all(all(facts[f["id"]].get(k)==v for k,v in f.items() if k!="confirmed") for f in before["facts"])
    checks["legacy_fact_review_preserved"]=all(facts[f["id"]].get("review_status")==f.get("review_status", "confirmed" if f["confirmed"] else "pending") for f in before["facts"])
    checks["nothing_auto_archived"]=all(objects[o["id"]].get("archived",False)==o.get("archived",False) for o in before["objects"])
    return checks

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--pid",type=int,required=True);parser.add_argument("--model-env",required=True);parser.add_argument("--restart-only",action="store_true");parser.add_argument("--label",default="review");args=parser.parse_args()
    assert 1<=len(args.label)<=40 and all(c.isascii() and (c.isalnum() or c in "-_") for c in args.label)
    load_dotenv(ROOT/".env")
    assert Config.from_env().uri=="bolt://127.0.0.1:17693"
    old_pid=args.pid
    assert Path(f"/proc/{old_pid}/cwd").resolve()==ROOT.resolve()
    assert b"-m\x00demo\x00" in Path(f"/proc/{old_pid}/cmdline").read_bytes()
    listeners=subprocess.check_output(["ss","-ltnp","sport = :8096"],text=True)
    assert f"pid={old_pid}," in listeners
    # Stop first so no request can change records between snapshot and upgrade.
    os.kill(old_pid,signal.SIGINT)
    for _ in range(30):
        if f"pid={old_pid}," not in subprocess.check_output(["ss","-ltnp","sport = :8096"],text=True):break
        time.sleep(1)
    else:
        # Some shell-launched Python processes inherit ignored SIGINT. Verify
        # ownership again before terminating that exact service process.
        assert Path(f"/proc/{old_pid}/cwd").resolve()==ROOT.resolve()
        assert b"-m\x00demo\x00" in Path(f"/proc/{old_pid}/cmdline").read_bytes()
        os.kill(old_pid,signal.SIGTERM)
        for _ in range(10):
            if f"pid={old_pid}," not in subprocess.check_output(["ss","-ltnp","sport = :8096"],text=True):break
            time.sleep(1)
        else:raise RuntimeError("Old owned process did not stop; no second server started")
    suffix="restart" if args.restart_only else "upgrade"
    before=snapshot();(ROOT/"artifacts"/f"live-before-{args.label}-{suffix}.json").write_text(json.dumps(before,ensure_ascii=False,indent=2)+"\n")
    # Empty .env placeholders must not shadow the explicitly supplied model file.
    env={k:v for k,v in os.environ.items() if v or not k.startswith(("LLM_","EMBEDDING_"))}
    env.update(KD_PORT="8096",GRAPHITI_TELEMETRY_ENABLED="false")
    with (ROOT/"artifacts"/f"live-{args.label}-runtime.log").open("w") as log:
        process=subprocess.Popen([sys.executable,"-m","demo","--model-env",args.model_env],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    (ROOT/"artifacts"/f"live-{args.label}-process.json").write_text(json.dumps(dict(pid=process.pid,port=8096))+"\n")
    for _ in range(40):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8096/api/health",timeout=2) as r:health=json.load(r)["data"]
            break
        except Exception:
            if process.poll() is not None:raise RuntimeError("New live process exited; business backup preserved")
            time.sleep(1)
    else:raise RuntimeError("New live process not ready; business backup preserved")
    after=snapshot();(ROOT/"artifacts"/f"live-after-{args.label}-{suffix}.json").write_text(json.dumps(after,ensure_ascii=False,indent=2)+"\n")
    checks=compare(before,after)
    report=dict(pid=process.pid,url="http://127.0.0.1:8096/",checks=checks,objects=len(after["objects"]),facts=len(after["facts"]),revision=after["revision"],health=health)
    name=f"live-{args.label}-verification.json" if args.label!="review" else ("live-review-restart-verification.json" if args.restart_only else "live-review-verification.json")
    (ROOT/"docs"/name).write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps(report,ensure_ascii=False));assert all(checks.values()),"Live data comparison failed; inspect preserved snapshots"
    assert health["model_configured"],"Model file was not loaded; page operations remain available, inspect startup environment"
if __name__=="__main__":main()
