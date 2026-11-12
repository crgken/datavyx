"""Authorized one-off final offline backup; does not migrate, purge or open MCP writes."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time

CONTAINERS={'atlas-knowledge-demo-neo4j':'atlas-knowledge-demo_demo-neo4j',
            'atlas-knowledge-demo-pending-neo4j':'atlas-knowledge-demo_demo-pending-neo4j'}
SITE=Path('/etc/nginx/sites-available/kb.datavyx.com')
API='atlasai-knowledge-api.service'

def run(args,**kwargs):return subprocess.run(args,check=True,capture_output=True,text=True,**kwargs)
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def save(path,value):
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:json.dump(value,stream,sort_keys=True)
def private_copy(source,target):
    shutil.copyfile(source,target);target.chmod(0o600)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--commit',required=True);parser.add_argument('--check-only',action='store_true')
    args=parser.parse_args();assert os.geteuid()==0 and len(args.commit)==40
    assert os.getenv('ATLASAI_RELEASE_AUTHORIZED_COMMIT')==args.commit
    root=Path('/opt/atlasai-mcp/candidates')/args.commit
    sys.path.insert(0,str(root/'deploy/mcp'));sys.path.insert(0,str(root/'knowledge-management-demo'))
    from release_artifact import verify_installed
    from run_env import read_env
    from mcp_backend.settings import Settings
    from mcp_backend.neo4j_repository import Repository
    from mcp_backend.migrations.legacy import audit
    verify_installed(root,args.commit)
    prepared=json.loads((Path('/home/ubuntu/atlasai-deploy/mcp')/('formal-'+args.commit[:7]+'-prepared.json')).read_text())
    reviewed=Path(prepared['reviewed_nginx']);assert sha(reviewed)==prepared['nginx_sha256']
    values=read_env('/etc/atlasai/mcp.env')
    assert values['MCP_MODE']=='production' and values['MCP_NEO4J_URI']=='bolt://127.0.0.1:17693'
    assert values['MCP_RECORDING_ENABLED']=='0';os.environ.update(values)
    def wait_database():
        probe=Repository(Settings.from_env());deadline=time.monotonic()+90
        try:
            while True:
                try:probe.driver.verify_connectivity();return
                except Exception:
                    assert time.monotonic()<deadline,'Canonical database did not become ready'
                    time.sleep(1)
        finally:probe.close()
    assert subprocess.run(['systemctl','is-active','--quiet',API]).returncode==0
    mounts={}
    for name,volume in CONTAINERS.items():
        row=json.loads(run(['docker','inspect',name]).stdout)[0]
        mount=next(m for m in row['Mounts'] if m['Destination']=='/data')
        assert row['State']['Running'] and mount['Type']=='volume' and mount['Name']==volume
        path=Path(mount['Source']).resolve()
        assert path.is_relative_to(Path('/var/lib/docker/volumes')) and path.name=='_data' and path.parent.name==volume
        mounts[name]=str(path)
    if args.check_only:
        print(json.dumps({'status':'backup_preflight_valid','commit':args.commit,'databases':len(mounts),
            'writes_closed':True,'reviewed_nginx_verified':True}));return
    os.umask(0o077)
    backup=Path('/home/ubuntu/atlasai-deploy/backups')/('mcp-'+args.commit[:7]+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    assert backup.parent==Path('/home/ubuntu/atlasai-deploy/backups') and not backup.exists()
    backup.mkdir(mode=0o700)
    configs=backup/'configs';configs.mkdir(mode=0o700)
    files=[SITE,Path('/etc/systemd/system')/(API+'.service'),Path('/etc/systemd/system/atlasai-knowledge-db.service'),
        Path('/opt/atlasai-knowledge/knowledge-management-demo/.env'),Path('/etc/atlasai/mcp.env')]
    files.extend(Path('/home/ubuntu/atlasai-deploy').glob('*model*.env'))
    legacy_root=Path('/opt/atlasai-knowledge/knowledge-management-demo')
    files.extend(p for name in ('compose.yml','compose.yaml','docker-compose.yml','docker-compose.yaml')
                 if (p:=legacy_root/name).is_file())
    saved=[]
    for source in files:
        if source.is_file():
            dest=configs/(hashlib.sha256(str(source).encode()).hexdigest()[:12]+'-'+source.name.lstrip('.'))
            private_copy(source,dest);saved.append({'source':str(source),'backup':str(dest),'sha256':sha(dest)})
    private_copy(SITE,backup/'kb.datavyx.com.original')
    frontend=Path('/opt/atlasai-knowledge/knowledge-web-pwa/build/web/main.dart.js')
    frontend_hash=sha(frontend)
    gate=backup/'maintenance-gate.conf'
    gate.write_text(reviewed.read_text().replace('proxy_pass http://127.0.0.1:8099;','return 503;')
        .replace('/opt/atlasai-mcp/current/deploy/mcp',str(root/'deploy/mcp')))
    gate.chmod(0o600)
    save(backup/'preflight.json',{'runtime_commit':args.commit,'configs':saved,'frontend_sha256':frontend_hash,
        'script_sha256':sha(__file__),'reviewed_nginx_sha256':prepared['nginx_sha256']})
    gated=False
    try:
        shutil.copyfile(gate,SITE);run(['nginx','-t']);run(['systemctl','reload','nginx']);gated=True
        deadline=time.monotonic()+45
        while run(['ss','-Htn','state','established','( sport = :8096 )']).stdout.strip():
            assert time.monotonic()<deadline,'Old HTTP requests did not drain within the maintenance bound'
            time.sleep(1)
        run(['systemctl','stop',API],timeout=45)
        assert subprocess.run(['systemctl','is-active','--quiet',API]).returncode!=0
        repo=Repository(Settings.from_env())
        try:
            unscoped=repo.transaction(lambda tx:tx.run('MATCH (n) WHERE (n:KDObject OR n:KDFact OR n:KDSource) '
                'AND n.dataset IS NULL RETURN count(n) AS n').single()['n'])
            assert unscoped==0
            datasets=repo.transaction(lambda tx:[r['d'] for r in tx.run('MATCH (w:KDWorkspace) RETURN DISTINCT w.dataset AS d ORDER BY d')])
            frozen={d:audit(repo,d) for d in datasets};assert all(r['ready'] for r in frozen.values())
        finally:repo.close()
        save(backup/'frozen-audits.json',frozen)
        run(['docker','stop','-t','60',*CONTAINERS],timeout=145)
        archives=[]
        for name,path in mounts.items():
            assert not json.loads(run(['docker','inspect',name]).stdout)[0]['State']['Running']
            archive=backup/(name+'.tar.gz')
            with tarfile.open(archive,'w:gz') as stream:stream.add(path,arcname='.')
            archive.chmod(0o600);archives.append({'container':name,'path':str(archive),'sha256':sha(archive)})
        code=backup/'legacy-backend-code.tar.gz'
        with tarfile.open(code,'w:gz') as stream:
            stream.add('/opt/atlasai-knowledge/knowledge-management-demo/demo',arcname='demo',
                filter=lambda item:None if '__pycache__' in Path(item.name).parts else item)
            for name in ('requirements.txt','requirements.lock.txt'):
                if (legacy_root/name).is_file():stream.add(legacy_root/name,arcname=name)
        code.chmod(0o600)
        run(['docker','start',*CONTAINERS],timeout=60)
        wait_database()
        checks=[]
        for item in archives:
            restored=backup/('restore-check-'+item['container']);restored.mkdir(mode=0o700)
            with tarfile.open(item['path'],'r:gz') as stream:stream.extractall(restored,filter='data')
            reports=backup/('check-reports-'+item['container']);reports.mkdir(mode=0o700)
            checked=subprocess.run(['docker','run','--rm','--pull=never','--network','none','--user','0:0',
                '-e','HEAP_SIZE=256m','-v',str(restored)+':/data','-v',str(reports)+':/reports',
                '--entrypoint','neo4j-admin','neo4j:5.26-community','database','check',
                '--from-path-data=/data/databases','--from-path-txn=/data/transactions',
                '--max-off-heap-memory=256m','--threads=1','--report-path=/reports','neo4j'],capture_output=True,text=True,timeout=180)
            log=reports/'command.log';log.write_text(checked.stdout+'\n'+checked.stderr);log.chmod(0o600)
            assert checked.returncode==0,'Backup copy consistency check failed; inspect protected report'
            assert sha(item['path'])==item['sha256']
            checks.append({'container':item['container'],'status':'consistent','log':str(log)})
        assert sha(frontend)==frontend_hash
        manifest={'runtime_commit':args.commit,'method':'writers_stopped_clean_database_stop_offline_volume_archive',
            'created_utc':datetime.now(timezone.utc).isoformat(),'archives':archives,'checks':checks,'configs':saved,
            'source_audits':str(backup/'frozen-audits.json'),'legacy_code_sha256':sha(code),
            'frontend_sha256':frontend_hash,'recording_enabled':False,'old_api_stopped':True,
            'reviewed_nginx':str(reviewed),'reviewed_nginx_sha256':prepared['nginx_sha256'],'script_sha256':sha(__file__)}
        save(backup/'manifest.json',manifest)
        print(json.dumps({'status':'final_backup_verified','backup':str(backup),'manifest_sha256':sha(backup/'manifest.json'),
            'archives':[{'container':i['container'],'sha256':i['sha256']} for i in archives],
            'consistency_checks':len(checks),'frontend_unchanged':True,'old_api_stopped':True}))
    except Exception:
        run(['docker','start',*CONTAINERS],timeout=60)
        wait_database()
        shutil.copyfile(backup/'kb.datavyx.com.original',SITE);run(['nginx','-t'])
        if gated:run(['systemctl','reload','nginx'])
        run(['systemctl','start',API],timeout=60)
        raise

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'backup_failed':type(error).__name__,'production_switch_not_applied':True}));sys.exit(1)
