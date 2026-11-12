"""Start only the existing isolated 17694 database; run inside Linux/WSL."""
import json
import subprocess
import time

name='atlas-knowledge-demo-test-neo4j'
info=json.loads(subprocess.check_output(['docker','inspect',name]))[0]
assert info['Config']['Image']=='neo4j:5.26-community'
assert info['HostConfig']['PortBindings']['7687/tcp']==[{'HostIp':'127.0.0.1','HostPort':'17694'}]
assert 'NEO4J_AUTH=none' in info['Config']['Env']
subprocess.run(['docker','start',name],check=True,stdout=subprocess.DEVNULL)
for attempt in range(40):
    try:
        result=subprocess.run(['docker','exec',name,'timeout','7','cypher-shell','RETURN 1'],capture_output=True,timeout=9)
        if result.returncode==0:
            print('READY isolated Neo4j 17694',flush=True)
            break
    except subprocess.TimeoutExpired:pass
    time.sleep(1)
else:raise RuntimeError('Isolated test Neo4j did not become ready.')
