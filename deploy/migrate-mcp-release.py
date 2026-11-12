"""Explicit authorized migration after a verified offline backup, before opening MCP."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

LABELS=('KDObject','KDFact','KDSource','KDWorkspace','KDWorkspaceMember','KDMCPRequest')
EDGES=('KD_HAS_FACT','KD_TARGET','KD_SUPPORTS')

def snapshot(tx,ids=None):
    rows=[];selected={}
    for label in LABELS:
        nodes=[dict(r['n']) for r in tx.run(f'MATCH (n:{label}) WHERE $ids IS NULL OR n.id IN $ids '
            'RETURN properties(n) AS n ORDER BY n.id',ids=None if ids is None else ids[label])]
        selected[label]=[n['id'] for n in nodes]
        if label=='KDFact':nodes=[{k:v for k,v in n.items() if k!='number_scaled'} for n in nodes]
        rows.append({'label':label,'nodes':nodes})
    all_ids=[i for values in selected.values() for i in values]
    for edge in EDGES:
        values=[dict(r) for r in tx.run(f'MATCH (a)-[e:{edge}]->(b) WHERE a.id IN $ids AND b.id IN $ids '
            'RETURN a.id AS a,b.id AS b,properties(e) AS properties ORDER BY a,b',ids=all_ids)]
        values.sort(key=lambda row:json.dumps(row,sort_keys=True,ensure_ascii=False))
        rows.append({'edge':edge,'rows':values})
    return rows,selected

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--commit',required=True)
    parser.add_argument('--backup',required=True);parser.add_argument('--backup-sha',required=True)
    args=parser.parse_args()
    assert os.geteuid()==0 and re.fullmatch('[0-9a-f]{40}',args.commit)
    assert os.getenv('ATLASAI_RELEASE_AUTHORIZED_COMMIT')==args.commit
    backup=Path(args.backup).resolve()
    assert backup.parent==Path('/home/ubuntu/atlasai-deploy/backups') and backup.name.startswith('mcp-'+args.commit[:7]+'-')
    manifest_path=backup/'manifest.json'
    assert hashlib.sha256(manifest_path.read_bytes()).hexdigest()==args.backup_sha
    manifest=json.loads(manifest_path.read_text());assert manifest['runtime_commit']==args.commit
    assert len(manifest['checks'])==2 and all(c['status']=='consistent' for c in manifest['checks'])
    for archive in manifest['archives']:
        assert hashlib.sha256(Path(archive['path']).read_bytes()).hexdigest()==archive['sha256']
    assert subprocess.run(['systemctl','is-active','--quiet','atlasai-knowledge-api.service']).returncode!=0
    result_path=backup/'migration-verification.json'
    assert not result_path.exists(),'Migration already recorded; inspect it before reapplying'
    root=Path('/opt/atlasai-mcp/candidates')/args.commit
    sys.path.insert(0,str(root/'deploy/mcp'));sys.path.insert(0,str(root/'knowledge-management-demo'))
    from release_artifact import verify_installed
    from run_env import read_env
    from mcp_backend.settings import Settings
    from mcp_backend.neo4j_repository import Repository,Scope
    from mcp_backend.migrations.legacy import audit,migrate
    from mcp_backend.domain import digest
    from mcp_backend.query import Queries
    from mcp_backend.contracts import QueryInput
    from mcp_backend.permissions import PUBLIC_PERMISSIONS
    verify_installed(root,args.commit)
    values=read_env('/etc/atlasai/mcp.env')
    assert values['MCP_MODE']=='production' and values['MCP_NEO4J_URI']=='bolt://127.0.0.1:17693'
    assert values['MCP_RECORDING_ENABLED']=='0';os.environ.update(values)
    frozen=json.loads((backup/'frozen-audits.json').read_text())
    repo=Repository(Settings.from_env())
    try:
        for dataset,original in frozen.items():
            assert audit(repo,dataset)['fingerprint']==original['fingerprint'],'Frozen canonical data changed before migration'
        before,ids=repo.transaction(snapshot)
        repo.migrate()
        migrations={d:migrate(repo,d,r['fingerprint']) for d,r in frozen.items()}
        after,_=repo.transaction(snapshot)
        assert digest(before)==digest(after),'Original IDs, source text, states or relations changed unexpectedly'
        audited={d:audit(repo,d) for d in frozen}
        assert all(r['ready'] and r['source_fingerprint']==frozen[d]['source_fingerprint'] for d,r in audited.items())
        owner=values['MCP_RECORDING_ACTORS']
        binding=repo.transaction(lambda tx:dict(tx.run('MATCH (m:KDWorkspaceMember {actor:$a,enabled:true,role:"owner"}) '
            'MATCH (w:KDWorkspace {id:m.workspace_id,status:"active"}) RETURN w.id AS id,w.dataset AS dataset LIMIT 1',a=owner).single()))
        scope=Scope(owner,values['MCP_AUTH_CLIENT_IDS'],binding['id'],binding['dataset'],PUBLIC_PERMISSIONS,int(time.time())+300)
        count=Queries(repo).query_records(scope,QueryInput(operation='COUNT',limit=1))
        report={'runtime_commit':args.commit,'backup_manifest_sha256':args.backup_sha,
            'canonical_fingerprint_before':digest(before),'canonical_fingerprint_after':digest(after),
            'original_ids':ids,'migrations':migrations,'post_audits':audited,'original_content_unchanged':True,
            'current_objects':count['count'],'revision':count['revision'],'workspace_id':binding['id'],'recording_enabled':False}
        with os.fdopen(os.open(result_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:json.dump(report,stream,sort_keys=True)
        print(json.dumps({'status':'production_migration_verified','runtime_commit':args.commit,
            'original_content_unchanged':True,'canonical_fingerprint':digest(after),'current_objects':count['count'],
            'revision':count['revision'],'number_backfilled':sum(r['number_backfilled'] for r in migrations.values()),
            'recording_enabled':False,'verification':str(result_path)}))
    finally:repo.close()

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'migration_failed':type(error).__name__,'writes_remain_closed':True}));sys.exit(1)
