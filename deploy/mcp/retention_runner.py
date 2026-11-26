"""Run the authorized retention release against the fixed formal/candidate profiles."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from run_env import read_env
from release_artifact import verify_installed


def main():
    try:
        if os.getuid()!=0 or len(sys.argv)!=2 or sys.argv[1] not in {'audit','daily'}:raise ValueError()
        commit=os.getenv('ATLASAI_RETENTION_AUTHORIZED_COMMIT','')
        if not re.fullmatch('[0-9a-f]{40}',commit):raise ValueError()
        release=Path('/opt/atlasai-mcp/current').resolve()
        verify_installed(release,commit)
        candidate=Path('/opt/atlasai-mcp/candidate').resolve()
        # An old candidate process must not bypass the shared closed-account barrier.
        verify_installed(candidate,candidate.name)
        backend=release/'knowledge-management-demo';failures=0;formal_env=None
        for filename,uri,resource in [('mcp.env','bolt://127.0.0.1:17693','https://kb.datavyx.com/mcp'),
            ('mcp-candidate.env','bolt://127.0.0.1:17694','https://kb.datavyx.com/mcp-candidate')]:
            path=Path('/etc/atlasai')/filename
            if path.is_symlink() or path.stat().st_uid!=0 or path.stat().st_mode&0o777!=0o600:raise ValueError()
            values=read_env(path)
            if values.get('MCP_NEO4J_URI')!=uri or values.get('MCP_RESOURCE_URL')!=resource:raise ValueError()
            env={'PATH':os.environ.get('PATH',''),'PYTHONDONTWRITEBYTECODE':'1',**values,
                'ATLASAI_RETENTION_AUTHORIZED_COMMIT':commit}
            if filename=='mcp.env':formal_env=env
            result=subprocess.run([str(backend/'.venv-mcp/bin/python'),'-m','scripts.mcp_retention',sys.argv[1],
                '--authorized-commit',commit],cwd=backend,env=env)
            failures+=result.returncode!=0
        if sys.argv[1]=='daily':
            result=subprocess.run([str(backend/'.venv-mcp/bin/python'),'-m','scripts.mcp_retention','backup-legacy',
                '--authorized-commit',commit],cwd=backend,env=formal_env)
            failures+=result.returncode!=0
        command=[str(backend/'.venv-mcp/bin/python'),'-m','scripts.mcp_retention','rotate-legacy','--authorized-commit',commit]
        if sys.argv[1]=='daily' and not failures:command+=['--apply']
        failures+=subprocess.run(command,cwd=backend,env=formal_env).returncode!=0
        return 2 if failures else 0
    except (ValueError,OSError,KeyError,subprocess.SubprocessError):
        print(json.dumps({'error':'Retention runner configuration or release validation failed.'}));return 2


if __name__=='__main__':raise SystemExit(main())
