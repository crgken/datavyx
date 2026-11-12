"""One-off authorized release preparation; no service stop or database write."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shlex
import subprocess
import sys

def protected_json(path,value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
        json.dump(value,stream,ensure_ascii=False,sort_keys=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--commit',required=True)
    args=parser.parse_args();assert os.geteuid()==0 and len(args.commit)==40
    root=Path('/opt/atlasai-mcp/candidates')/args.commit
    sys.path.insert(0,str(root/'deploy/mcp'));sys.path.insert(0,str(root/'knowledge-management-demo'))
    from release_artifact import verify_installed
    from run_env import read_env
    verify_installed(root,args.commit)
    legacy_python='/opt/atlasai-knowledge/knowledge-management-demo/.venv/bin/python'
    read_code="from dotenv import dotenv_values;import json;v=dotenv_values('/opt/atlasai-knowledge/knowledge-management-demo/.env');print(json.dumps({k:v.get(k) for k in ('KD_NEO4J_URI','KD_NEO4J_DATABASE','KD_NEO4J_USER','KD_NEO4J_PASSWORD')}))"
    db=json.loads(subprocess.check_output([legacy_python,'-c',read_code]))
    assert db['KD_NEO4J_URI']=='bolt://127.0.0.1:17693' and db['KD_NEO4J_PASSWORD']
    candidate=read_env('/etc/atlasai/mcp-candidate.env')
    ledger=Path('/var/lib/atlasai-mcp/purge-ledger');ledger.mkdir(mode=0o700,parents=True,exist_ok=True)
    account=pwd.getpwnam('ubuntu');os.chown(ledger,account.pw_uid,account.pw_gid);ledger.chmod(0o700)
    config=Path('/etc/atlasai/mcp.env')
    if not config.exists():
        assert not list(ledger.iterdir()),'Existing erasure ledger requires its original signing key'
        values={'MCP_MODE':'production','MCP_HOST':'127.0.0.1','MCP_PORT':'8099',
            'MCP_NEO4J_URI':db['KD_NEO4J_URI'],'MCP_NEO4J_DATABASE':db['KD_NEO4J_DATABASE'] or 'neo4j',
            'MCP_NEO4J_USER':db['KD_NEO4J_USER'] or 'neo4j','MCP_NEO4J_PASSWORD':db['KD_NEO4J_PASSWORD'],
            'MCP_AUTH_ISSUER':candidate['MCP_AUTH_ISSUER'],'MCP_AUTH_AUDIENCE':'https://kb.datavyx.com/mcp',
            'MCP_AUTH_CLIENT_IDS':'7CoGooGTzCivdedhDG2P2sjhGbcQJZo1','MCP_RESOURCE_URL':'https://kb.datavyx.com/mcp',
            'MCP_CURSOR_SECRET':secrets.token_urlsafe(48),'MCP_PURGE_LEDGER_SECRET':secrets.token_urlsafe(48),
            'MCP_PURGE_LEDGER_DIR':str(ledger),'MCP_MAX_RECORDS':'10000','MCP_RECORDING_ENABLED':'0',
            'MCP_RECORDING_ACTORS':'3195294ed8d5fd36ee08345fcabd5bab5b888af09ba11f3dc779d13c32cfd9dc'}
        assert all('\n' not in v and '\r' not in v for v in values.values())
        with os.fdopen(os.open(config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
            stream.write('\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n')
    values=read_env(config)
    assert config.stat().st_uid==0 and config.stat().st_mode&0o777==0o600
    assert values['MCP_MODE']=='production' and values['MCP_RESOURCE_URL']=='https://kb.datavyx.com/mcp'
    assert values['MCP_NEO4J_URI']=='bolt://127.0.0.1:17693' and values['MCP_RECORDING_ENABLED']=='0'
    assert values['MCP_NEO4J_PASSWORD']==db['KD_NEO4J_PASSWORD']
    os.environ.update(values)
    from mcp_backend.settings import Settings
    from mcp_backend.neo4j_repository import Repository
    from mcp_backend.migrations.legacy import audit
    repo=Repository(Settings.from_env())
    try:
        datasets=repo.transaction(lambda tx:[r['d'] for r in tx.run('MATCH (w:KDWorkspace) RETURN DISTINCT w.dataset AS d ORDER BY d')])
        reports={d:audit(repo,d) for d in datasets}
        assert reports and all(v.get('ready',True) and not v['blockers'] for v in reports.values())
        owner=repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {actor:$a,enabled:true,role:"owner"}) RETURN count(m) AS n',
            a=values['MCP_RECORDING_ACTORS']).single()['n'])
        assert owner>0,'The verified operator must retain an existing owner binding'
    finally:repo.close()
    home=Path('/home/ubuntu/atlasai-deploy/mcp');tag=args.commit[:7]
    site=Path('/etc/nginx/sites-available/kb.datavyx.com').read_text()
    pieces=site.split('server {');assert len(pieces)==3
    http='server {'+pieces[1];ssl=pieces[2]
    prefix=ssl[:ssl.index('    location ')]
    prefix='\n'.join(line for line in prefix.splitlines() if not line.strip().startswith(('root ','index ')))+'\n'
    fragment=(root/'deploy/mcp/nginx-production.conf').read_text()
    reviewed=home/('reviewed-production-'+tag+'.conf')
    content=http+'server {'+prefix+'    include /opt/atlasai-mcp/candidate/deploy/mcp/nginx-candidate.conf;\n'+fragment+'\n}\n'
    with os.fdopen(os.open(reviewed,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:stream.write(content)
    test=home/('nginx-test-'+tag+'.conf')
    main_conf=Path('/etc/nginx/nginx.conf').read_text();needle='include /etc/nginx/sites-enabled/*;'
    assert main_conf.count(needle)==1
    with os.fdopen(os.open(test,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
        stream.write(main_conf.replace(needle,'include '+str(reviewed)+';'))
    checked=subprocess.run(['nginx','-t','-c',str(test)],capture_output=True,text=True)
    protected_json(home/('formal-'+tag+'-nginx-check.json'),{'returncode':checked.returncode,'stderr':checked.stderr})
    assert checked.returncode==0,'Reviewed gateway syntax failed; inspect protected check report'
    result={'runtime_commit':args.commit,'production_config':str(config),'reviewed_nginx':str(reviewed),
        'nginx_sha256':hashlib.sha256(content.encode()).hexdigest(),'legacy_audits':reports,'owner_binding_valid':True,
        'production_recording_enabled':False,'source_modified':False}
    protected_json(home/('formal-'+tag+'-prepared.json'),result)
    print(json.dumps({'status':'formal_prepared','commit':args.commit,'nginx_sha256':result['nginx_sha256'],
        'audits':[{'counts':r['counts'],'states':r['states'],'number_backfill_count':r['number_backfill_count'],
                   'blockers':r['blockers'],'source_fingerprint':r['source_fingerprint']} for r in reports.values()],
        'owner_binding_valid':True,'recording_enabled':False,'nginx_syntax_valid':True}))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'preparation_failed':type(error).__name__}));sys.exit(1)
