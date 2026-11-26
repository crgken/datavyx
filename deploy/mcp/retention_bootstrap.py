"""Initialize the external retention state before either new MCP process starts."""
import argparse
import json
import os
from pathlib import Path
import pwd
import sys
from run_env import read_env
from release_artifact import verify_installed


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--commit',required=True)
    args=parser.parse_args()
    try:
        if os.getuid()!=0 or os.getenv('ATLASAI_RETENTION_AUTHORIZED_COMMIT')!=args.commit:raise ValueError()
        release=Path(__file__).resolve().parents[2]
        verify_installed(release,args.commit)
        backend=release/'knowledge-management-demo';sys.path.insert(0,str(backend))
        values=read_env('/etc/atlasai/mcp.env')
        if values.get('MCP_PURGE_LEDGER_DIR')!='/var/lib/atlasai-mcp/purge-ledger':raise ValueError()
        os.environ.update(values)
        from mcp_backend.settings import Settings
        from mcp_backend.account_state import AccountState
        from mcp_backend.retention import Retention
        settings=Settings.from_env();state=AccountState(settings)
        root=Path('/var/lib/atlasai-mcp/account-state')
        if not root.exists():AccountState.initialize(settings)
        state.root()  # Existing corrupt state/key is never replaced.
        user=pwd.getpwnam('ubuntu')
        os.chown(root,user.pw_uid,user.pw_gid);os.chown(root/'signing-key',user.pw_uid,user.pw_gid)
        backups=Path('/home/ubuntu/atlasai-deploy/backups/retention')
        backups.mkdir(mode=0o700,exist_ok=True)
        if backups.is_symlink() or backups.resolve()!=backups:raise ValueError()
        store=Retention(settings,backups)
        if not store.path.exists():store.initialize()
        store._read()
        print(json.dumps({'status':'external_retention_state_ready','reinitialized':False,'database_modified':False}));return 0
    except Exception:
        print(json.dumps({'error':'Protected retention bootstrap failed; inspect existing state, never reset its key.'}));return 2


if __name__=='__main__':raise SystemExit(main())
