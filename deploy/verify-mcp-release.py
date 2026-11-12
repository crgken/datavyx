"""Read-only production release checks; save bounded evidence without source text or credentials."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

COMMIT='d1ed5d3b1abbe53974c066000bb72e850801626b'
BACKUP=Path('/home/ubuntu/atlasai-deploy/backups/mcp-d1ed5d3-20261112T102743Z')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--phase',choices=('routed','authenticated','opened'),required=True)
    args=parser.parse_args();assert os.geteuid()==0
    root=Path('/opt/atlasai-mcp/current').resolve();assert root.name==COMMIT
    sys.path.insert(0,str(root/'deploy/mcp'));sys.path.insert(0,str(root/'knowledge-management-demo'))
    from run_env import read_env
    from release_artifact import verify_installed
    from mcp_backend.domain import digest
    from mcp_backend.settings import Settings
    from mcp_backend.neo4j_repository import Repository,Scope
    from mcp_backend.permissions import PUBLIC_PERMISSIONS
    from mcp_backend.server import TOOL_INPUTS
    from mcp_backend.query import Queries
    from mcp_backend.ingestion import Ingestion
    from mcp_backend.contracts import QueryInput,ReceiptInput
    verify_installed(root,COMMIT)
    values=read_env('/etc/atlasai/mcp.env');os.environ.update(values)
    assert values['MCP_RECORDING_ENABLED']==('1' if args.phase=='opened' else '0')
    assert values['MCP_RESOURCE_URL']==values['MCP_AUTH_AUDIENCE']=='https://kb.datavyx.com/mcp'
    assert values['MCP_NEO4J_URI']=='bolt://127.0.0.1:17693'
    assert len(TOOL_INPUTS)==11 and 'purge_information' not in TOOL_INPUTS and 'purge' not in PUBLIC_PERMISSIONS
    migration=json.loads((BACKUP/'migration-verification.json').read_text())
    spec=importlib.util.spec_from_file_location('release_migration',Path(__file__).with_name('migrate-mcp-release.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    repo=Repository(Settings.from_env())
    try:
        original,_=repo.transaction(lambda tx:module.snapshot(tx,migration['original_ids']))
        assert digest(original)==migration['canonical_fingerprint_after']
        scope=Scope(values['MCP_RECORDING_ACTORS'],values['MCP_AUTH_CLIENT_IDS'],migration['workspace_id'],
            'default',PUBLIC_PERMISSIONS,int(time.time())+300)
        count=Queries(repo).query_records(scope,QueryInput(operation='COUNT',limit=1));assert count['count']==5
        receipts={key:Ingestion(repo).get_receipt(scope,ReceiptInput(request_id=key))
            for key in ('release-smoke-20261112','release-smoke-delete-20261112')}
        if args.phase!='routed':
            saved=receipts['release-smoke-20261112'];deleted=receipts['release-smoke-delete-20261112']
            assert saved['found'] and deleted['found']
            assert saved['receipt']['status']=='recorded' and saved['receipt']['current_state']=='deleted'
            assert deleted['receipt']['status']=='applied'
    finally:repo.close()
    statuses={};metadata=None
    for path,expected in (('/',200),('/api/mobile/session',410),('/api/mobile/execute',410),
        ('/api/mobile/transcribe',410),('/sw.js',410),('/flutter_service_worker.js',410),
        ('/.well-known/oauth-protected-resource/mcp',200),('/mcp',401)):
        request=urllib.request.Request('https://kb.datavyx.com'+path,
            data=b'{}' if path=='/mcp' else None,headers={'Content-Type':'application/json'})
        try:response=urllib.request.urlopen(request,timeout=10)
        except urllib.error.HTTPError as error:response=error
        with response:
            statuses[path]=response.code;assert response.code==expected
            if path.startswith('/.well-known/'):metadata=json.load(response)
            if path=='/mcp':assert '/.well-known/oauth-protected-resource/mcp' in response.headers['www-authenticate']
    assert metadata['resource']==values['MCP_RESOURCE_URL']
    assert set(metadata['scopes_supported'])=={'knowledge:'+p for p in PUBLIC_PERMISSIONS}
    def state(action,service):return subprocess.run(['systemctl',action,service],capture_output=True,text=True).stdout.strip()
    services={s:{a:state(a,s) for a in ('is-active','is-enabled')} for s in
        ('atlasai-mcp.service','atlasai-knowledge-api.service','atlasai-knowledge-db.service')}
    assert services['atlasai-mcp.service']=={'is-active':'active','is-enabled':'enabled'}
    assert services['atlasai-knowledge-api.service']=={'is-active':'inactive','is-enabled':'disabled'}
    assert services['atlasai-knowledge-db.service']['is-active']=='active'
    listeners=subprocess.check_output(['ss','-Hltn'],text=True)
    assert ':8099 ' in listeners and ':8096 ' not in listeners
    frontend=Path('/opt/atlasai-knowledge/knowledge-web-pwa/build/web/main.dart.js')
    assert hashlib.sha256(frontend.read_bytes()).hexdigest()==json.loads((BACKUP/'preflight.json').read_text())['frontend_sha256']
    report={'phase':args.phase,'runtime_commit':COMMIT,'current_objects':count['count'],'revision':count['revision'],
        'original_content_unchanged':True,'frontend_unchanged':True,'public_tools':sorted(TOOL_INPUTS),
        'permissions':sorted(PUBLIC_PERMISSIONS),'routes':statuses,'services':services,'receipts':receipts,
        'global_recording_enabled':args.phase=='opened'}
    with os.fdopen(os.open(BACKUP/('verification-'+args.phase+'.json'),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as stream:
        json.dump(report,stream,sort_keys=True)
    print(json.dumps({'status':'verified',**{k:v for k,v in report.items() if k not in ('receipts','public_tools')}}))

if __name__=='__main__':main()
