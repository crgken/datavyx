"""Start/stop only the isolated UI acceptance process, never the live app."""
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
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from demo.config import Config
from demo.store import Store
from fixtures import commit
info_path=ROOT/"artifacts"/"review-ui-server.json"
parser=argparse.ArgumentParser();parser.add_argument("--stop",action="store_true");args=parser.parse_args()
if args.stop:
    info=json.loads(info_path.read_text());pid=info["pid"]
    cmdline=Path(f"/proc/{pid}/cmdline").read_bytes()
    assert b"-m\x00demo" in cmdline
    environ=Path(f"/proc/{pid}/environ").read_bytes()
    assert b"KD_PORT=8097\x00" in environ and ("KD_DATASET="+info["dataset"]).encode()+b"\x00" in environ
    os.kill(pid,signal.SIGTERM);print("Stopped isolated UI server",pid);raise SystemExit()
dataset="review_ui_"+uuid4().hex
store=Store(Config(uri="bolt://127.0.0.1:17694",password="",dataset=dataset));store.initialize()
seed=commit(store,"待核实记录：周工协作北岸站，旧分类设备待核实。",{"objects":[{"ref":"p","name":"周工","kind":"人员","certain":False,"reason":"身份待核实"},{"ref":"s","name":"北岸站","kind":"研究站","certain":False,"reason":"身份待核实"},{"ref":"d","name":"校准仪","kind":"机构","certain":False,"reason":"类型待核实"}],"facts":[{"subject":"p","predicate":"协作","object":"s","certain":False,"reason":"内容待核实","evidence":"待核实记录：周工协作北岸站，旧分类设备待核实。"}]})
ids={o["ref"]:o["id"] for o in seed["objects"]};store.close()
env={**os.environ,"KD_PORT":"8097","KD_NEO4J_URI":"bolt://127.0.0.1:17694","KD_NEO4J_PASSWORD":"","KD_DATASET":dataset,"GRAPHITI_TELEMETRY_ENABLED":"false"}
# Deliberately prove page operations work without a configured model.
for name in ("LLM_API_KEY","OPENAI_API_KEY","DEEPSEEK_API_KEY"):env.pop(name,None)
with (ROOT/"artifacts"/"review-ui-server.log").open("w") as log:
    process=subprocess.Popen([sys.executable,"-m","demo"],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
info=dict(pid=process.pid,dataset=dataset,ids=ids);info_path.write_text(json.dumps(info,indent=2)+"\n")
for _ in range(30):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8097/api/health",timeout=2) as r:assert json.load(r)["ok"]
        print(json.dumps(info));break
    except Exception:time.sleep(1)
else:raise RuntimeError("Isolated server did not become ready")
