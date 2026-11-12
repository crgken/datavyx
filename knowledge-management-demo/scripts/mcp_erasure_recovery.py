"""Administrative erasure audit/reconciliation; never exposed as an MCP tool."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from mcp_backend.settings import Settings
from mcp_backend.neo4j_repository import Repository
from mcp_backend.purge_ledger import PurgeLedger
from mcp_backend.erasure import reapply_committed
from mcp_backend.errors import KnowledgeError,require


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',required=True)
    parser.add_argument('--action',choices=['audit','reconcile','apply'],default='audit')
    parser.add_argument('--authorized-commit',default='')
    args=parser.parse_args();repo=None
    try:
        settings=Settings.from_env();authorized=False
        if settings.mode=='production' and args.action!='audit':
            require(bool(re.fullmatch('[0-9a-f]{40}',args.authorized_commit))
                and os.getenv('ATLASAI_RELEASE_AUTHORIZED_COMMIT')==args.authorized_commit
                and settings.uri=='bolt://127.0.0.1:17693' and not settings.recording_enabled,
                'PERMISSION_DENIED','Production erasure recovery requires an authorized closed maintenance window.',403)
            root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root/'deploy/mcp'))
            from release_artifact import verify_installed
            verify_installed(root,args.authorized_commit)
            require(subprocess.run(['systemctl','is-active','--quiet','atlasai-knowledge-api.service']).returncode!=0,
                    'PERMISSION_DENIED','The old writer must be stopped before recovery.',403)
            authorized=True
        repo=Repository(settings);ledger=PurgeLedger(settings)
        if args.action=='reconcile':ledger.reconcile_prepared(repo,args.dataset)
        if args.action=='apply':reapply_committed(repo,args.dataset,authorized)
        entries=ledger.entries(args.dataset)
        repo.transaction(lambda tx:ledger.check(tx,args.dataset))
        print(json.dumps({'status':'erasure_reconciled','manifests':len(entries),'action':args.action}))
        return 0
    except KnowledgeError as error:
        print(json.dumps({'error':error.public()}));return 2
    finally:
        if repo:repo.close()


if __name__=='__main__':raise SystemExit(main())
