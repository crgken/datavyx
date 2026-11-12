"""Load a protected MCP-only env file without shell expansion or secret output."""
import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess


def read_env(path):
    result = {}
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        parts = shlex.split(line, comments=True, posix=True)
        if not parts: continue
        if len(parts) != 1 or '=' not in parts[0]: raise ValueError('Invalid environment line.')
        key, value = parts[0].split('=', 1)
        if not re.fullmatch('MCP_[A-Z0-9_]+', key) or key in result or '\x00' in value:
            raise ValueError('Only unique MCP environment keys are allowed.')
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', required=True); parser.add_argument('--python', required=True)
    parser.add_argument('--backend', required=True)
    parser.add_argument('--action', choices=['check-config', 'migrate'], required=True)
    parser.add_argument('--candidate', action='store_true')
    parser.add_argument('--production-closed', action='store_true')
    args = parser.parse_args()
    try:
        values = read_env(args.env)
        if args.candidate and not (values.get('MCP_MODE') == 'test'
            and values.get('MCP_NEO4J_URI') == 'bolt://127.0.0.1:17694'
            and values.get('MCP_PORT') == '8098'
            and values.get('MCP_RESOURCE_URL') == 'https://kb.datavyx.com/mcp-candidate'
            and values.get('MCP_AUTH_AUDIENCE') == values.get('MCP_RESOURCE_URL')):
            raise ValueError('Candidate configuration must use its isolated resource, port and database.')
        if args.production_closed and not (values.get('MCP_MODE') == 'production'
            and values.get('MCP_NEO4J_URI') == 'bolt://127.0.0.1:17693'
            and values.get('MCP_PORT') == '8099'
            and values.get('MCP_RESOURCE_URL') == 'https://kb.datavyx.com/mcp'
            and values.get('MCP_AUTH_AUDIENCE') == values.get('MCP_RESOURCE_URL')
            and values.get('MCP_RECORDING_ENABLED', '0') == '0'):
            raise ValueError('Production preparation requires the canonical resource and closed recording.')
        # No dotenv interpolation, shell, inherited model env or command-string composition.
        return subprocess.run([args.python, '-m', 'mcp_backend', '--'+args.action],
            cwd=args.backend, env={'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1', **values}).returncode
    except (ValueError, OSError):
        print('Protected MCP configuration failed validation; no values were printed.')
        return 2


if __name__ == '__main__': raise SystemExit(main())
