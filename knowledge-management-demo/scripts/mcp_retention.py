"""Root-only, immutable-release retention operations. No public MCP administrative tool."""
import argparse
from dataclasses import replace
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from mcp_backend.account_close import AccountClose
from mcp_backend.account_state import AccountState
from mcp_backend.errors import KnowledgeError,require
from mcp_backend.neo4j_repository import Repository
from mcp_backend.retention import Retention
from mcp_backend.retention_backup import export_graph,write_backup
from mcp_backend.settings import Settings


BACKUP_ROOT=Path('/home/ubuntu/atlasai-deploy/backups/retention')


def guard(settings,commit):
    require(os.name=='posix' and os.getuid()==0 and re.fullmatch('[0-9a-f]{40}',commit or '')
        and os.getenv('ATLASAI_RETENTION_AUTHORIZED_COMMIT')==commit,
        'PERMISSION_DENIED','Retention management requires the authorized root service and exact release.',403)
    require((settings.resource_url,settings.uri) in {
        ('https://kb.datavyx.com/mcp','bolt://127.0.0.1:17693'),
        ('https://kb.datavyx.com/mcp-candidate','bolt://127.0.0.1:17694')},
        'PERMISSION_DENIED','Unexpected production or candidate retention profile.',403)
    root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root/'deploy/mcp'))
    from release_artifact import verify_installed
    verify_installed(root,commit)


def legacy_not_mounted(store):
    from mcp_backend.retention import LEGACY_BUNDLES
    identifiers=subprocess.check_output(['docker','ps','-aq'],text=True).split()
    containers=json.loads(subprocess.check_output(['docker','inspect',*identifiers])) if identifiers else []
    for name in LEGACY_BUNDLES:
        target=store._legacy(name)
        for item in containers:
            for mount in item.get('Mounts',[]):
                source=Path(mount.get('Source','/nonexistent-container-mount'))
                require(not source.is_relative_to(target) and not target.is_relative_to(source),
                    'LEGACY_BACKUP_IN_USE','A container still references a historical backup; no disposal applied.',409)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['audit','initialize-inventory','block','erase','reconcile','backup','rotate',
        'daily','backup-legacy','register-legacy','rotate-legacy','provider-complete','ticket-close','ticket-due','ticket-complete'])
    parser.add_argument('--authorized-commit',required=True)
    parser.add_argument('--actor');parser.add_argument('--verified-case')
    parser.add_argument('--legacy-personal',action='append',default=[])
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--message-id',action='append',default=[]);parser.add_argument('--closed-at',type=float)
    parser.add_argument('--case-digest');parser.add_argument('--provider-receipt')
    parser.add_argument('--provider',choices=['auth0','legacy_copies'])
    parser.add_argument('--legacy-bundle')
    args=parser.parse_args();repo=None
    try:
        settings=Settings.from_env();guard(settings,args.authorized_commit)
        state=AccountState(settings);state.root()  # Never silently initialize or replace the account-state key.
        store=Retention(settings,BACKUP_ROOT)
        if args.action=='initialize-inventory':store.initialize();result={'status':'retention_inventory_initialized'}
        elif args.action=='provider-complete':
            from mcp_backend.domain import digest
            require(args.actor and args.provider and args.provider_receipt and len(args.provider_receipt)>=8,
                'INVALID_ARGUMENT','Record the exact account, provider task and actual completed disposal receipt.')
            with state.locked():
                body=state.load(state.path(args.actor))
                body['provider_tasks'][args.provider]={'state':'completed','completed_at':time.time(),
                    'receipt_digest':digest(args.provider_receipt)};state.write(body)
            result={'status':'provider_processing_recorded'}
        elif args.action=='ticket-close':result=store.close_ticket(args.verified_case,args.message_id,args.closed_at)
        elif args.action=='ticket-complete':store.complete_ticket(args.case_digest,args.provider_receipt);result={'status':'ticket_disposal_recorded'}
        elif args.action=='ticket-due':result={'due_count':len(store.due_tickets())}
        elif args.action=='rotate':result=store.rotate(apply=args.apply,profile=settings.resource_url)
        elif args.action=='register-legacy':
            legacy_not_mounted(store)
            result=store.register_legacy(args.legacy_bundle,['https://kb.datavyx.com/mcp','https://kb.datavyx.com/mcp-candidate','legacy-semantic'])
        elif args.action=='rotate-legacy':legacy_not_mounted(store);result=store.rotate_legacy(apply=args.apply)
        else:
            if args.action=='backup-legacy':
                require(subprocess.run(['systemctl','is-active','--quiet','atlasai-knowledge-api.service']).returncode!=0,
                    'PERMISSION_DENIED','The legacy semantic writer must remain stopped.',403)
                info=json.loads(subprocess.check_output(['docker','inspect','atlas-knowledge-demo-pending-neo4j']))[0]
                require(info['HostConfig']['PortBindings']['7687/tcp']==[{'HostIp':'127.0.0.1','HostPort':'17695'}],
                    'PERMISSION_DENIED','Unexpected legacy semantic database binding.',403)
                credential=next(v.removeprefix('NEO4J_AUTH=') for v in info['Config']['Env'] if v.startswith('NEO4J_AUTH='))
                user,password=('neo4j','unused-verification-only') if credential=='none' else credential.split('/',1)
                settings=replace(settings,uri='bolt://127.0.0.1:17695',user=user,password=password)
            repo=Repository(settings);close=AccountClose(repo)
            if args.action=='block':
                require(args.actor and args.verified_case,'INVALID_ARGUMENT','Use a trusted actor digest and verified support case.')
                body=close.block(args.actor,args.verified_case,args.legacy_personal)
                result={'status':'account_access_blocked','active_data_due':body['active_data_due'],'backup_data_due':body['backup_data_due']}
            elif args.action=='erase':
                require(args.actor,'INVALID_ARGUMENT','An exact verified account actor is required.')
                result=close.erase(args.actor)
            elif args.action=='reconcile':result={'status':'closure_reconciled','processed':len(close.reconcile())}
            elif args.action in {'backup','daily','backup-legacy'}:
                processed=len(close.reconcile()) if args.action=='daily' else 0
                suffix='legacy-semantic' if args.action=='backup-legacy' else 'formal' if settings.resource_url.endswith('/mcp') else 'candidate'
                name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')+'-'+suffix+'.json.gz'
                snapshot=export_graph(repo,quiescent_legacy=args.action=='backup-legacy')
                profile=settings.resource_url
                if args.action=='backup-legacy':
                    from mcp_backend.domain import digest
                    profile='legacy-semantic';snapshot['body']['profile']=profile;snapshot['sha256']=digest(snapshot['body'])
                write_backup(BACKUP_ROOT/name,snapshot);store.register_backup(name)
                from scripts.mcp_backup_verify import verify_backup
                verify_backup(store,name,settings)
                result={'status':'backup_restored_verified','profile':suffix,'closures_processed':processed,
                    'rotation':store.rotate(apply=args.action in {'daily','backup-legacy'},profile=profile),'due_support_cases':len(store.due_tickets())}
                pending=[body for body in state.entries() if any(task['state']!='completed'
                    for task in body['provider_tasks'].values())]
                result['accounts_needing_provider_processing']=len(pending)
                require(not any(body['active_data_due']<=time.time() for body in pending),'ACCOUNT_PROVIDER_RETENTION_DUE',
                    'Account closure provider/copy processing is overdue; inspect the protected case queue.',503)
                require(result['due_support_cases']==0,'SUPPORT_RETENTION_DUE',
                    'Closed support cases require verified mailbox disposal; the daily job is not complete.',503)
            else:
                repo.transaction(state.check_restore)
                result={'status':'retention_audit','closed_accounts':len(state.entries()),'backups':len(store._read()['backups']),
                    'rotation':store.rotate(profile=settings.resource_url),'due_support_cases':len(store.due_tickets())}
        # No contents, credentials, mail message IDs, account list or raw support case references in journal.
        if 'actor' in result:result={k:v for k,v in result.items() if k!='actor'}
        if args.action=='ticket-close':result={'status':'closed_ticket_registered','expires_at':result['expires_at']}
        print(json.dumps(result));return 0
    except KnowledgeError as error:
        print(json.dumps({'error':{'code':error.code,'message':error.message}}));return 2
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError):
        print(json.dumps({'error':{'code':'RETENTION_OPERATION_FAILED','message':'Inspect the protected operation; no sensitive values were printed.'}}));return 2
    finally:
        if repo:repo.close()


if __name__=='__main__':raise SystemExit(main())
