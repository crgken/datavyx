"""Read-only canonical backup before pending-record migration."""
import json
from pathlib import Path
from demo.config import Config
from demo.store import Store

store = Store(Config.from_env())
def read(tx):
    nodes = [dict(r) for r in tx.run("MATCH (n) WHERE any(l IN labels(n) WHERE l STARTS WITH 'KD') RETURN labels(n) AS labels,properties(n) AS properties ORDER BY n.id")]
    edges = [dict(r) for r in tx.run("MATCH (a)-[r]->(b) WHERE type(r) STARTS WITH 'KD' RETURN a.id AS source,b.id AS target,type(r) AS type,properties(r) AS properties")]
    return {"nodes":nodes,"edges":edges}
data = store.read(read)
path = Path('artifacts/before-auto-pending-20261020.json')
if path.exists():
    raise SystemExit('Backup already exists; refusing to overwrite')
path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({"backup":str(path),"objects":sum('KDObject' in n['labels'] for n in data['nodes']),"facts":sum('KDFact' in n['labels'] for n in data['nodes'])}))
store.close()
