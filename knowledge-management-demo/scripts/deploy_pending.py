"""Start the approved pending-record upgrade after a canonical equality check."""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from dotenv import load_dotenv
from demo.config import Config
from demo.store import Store

root=Path.cwd()
load_dotenv(root/'.env')
assert Config.from_env().uri=='bolt://127.0.0.1:17693'
listeners=subprocess.check_output(['ss','-ltnp','sport = :8096'],text=True)
assert 'LISTEN' not in listeners,'A service already owns 8096. Verify its PID before restarting.'
store=Store(Config.from_env())
def snapshot(tx):
    result={}
    for label in ('KDObject','KDFact','KDSource'):
        result[label]=[dict(r['n']) for r in tx.run(f'MATCH (n:{label} {{dataset:$d}}) RETURN properties(n) AS n ORDER BY n.id',d=store.dataset)]
    result['supports']=[dict(r) for r in tx.run('MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(n) RETURN s.id AS source,n.id AS target,properties(e) AS properties ORDER BY source,target',d=store.dataset)]
    result['revision']=store.lock(tx)
    return result
before=store.read(snapshot)
path=root/'artifacts/live-before-pending-upgrade.json'
assert not path.exists(),'Existing deployment backup must not be overwritten.'
path.write_text(json.dumps(before,ensure_ascii=False,indent=2),encoding='utf-8')
store.initialize()
after=store.read(snapshot)
(root/'artifacts/live-after-pending-upgrade.json').write_text(json.dumps(after,ensure_ascii=False,indent=2),encoding='utf-8')
checks={key:before[key]==after[key] for key in before}
assert all(checks.values()),'Canonical data changed during migration; inspect both preserved snapshots.'
store.close()
env={k:v for k,v in os.environ.items() if v or not k.startswith(('LLM_','EMBEDDING_'))}
env.update(KD_PORT='8096',GRAPHITI_TELEMETRY_ENABLED='false',SEMAPHORE_LIMIT='3')
with (root/'artifacts/live-pending-runtime.log').open('w') as log:
    process=subprocess.Popen([sys.executable,'-u','-m','demo','--model-env',sys.argv[1]],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
(root/'artifacts/live-pending-process.json').write_text(json.dumps({'pid':process.pid,'port':8096}),encoding='utf-8')
for _ in range(40):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8096/api/health',timeout=3) as response:health=json.load(response)['data']
        break
    except Exception:
        if process.poll() is not None:raise RuntimeError('New service exited; backups preserved')
        time.sleep(1)
else:raise RuntimeError('New service did not become ready')
with urllib.request.urlopen('http://127.0.0.1:8096/api/batches') as response:batches=json.load(response)['data']
report={'url':'http://127.0.0.1:8096/','pid':process.pid,'checks':checks,'objects':len(after['KDObject']),'facts':len(after['KDFact']),'revision':after['revision'],'pending_batches':batches['total'],'pending_items':sum(b['pending_count'] for b in batches['rows']),'health':health}
assert health['model_configured']
(root/'docs/live-pending-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False),flush=True)
