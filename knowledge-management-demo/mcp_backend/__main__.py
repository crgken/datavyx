"""MCP-specific entry point, independent from demo.__main__."""
import argparse
import json
import sys
from .errors import KnowledgeError
from .settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-config', action='store_true',help='Validate configuration without connecting or writing.')
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if args.check_config:
            print(json.dumps({'status':'configuration_valid','mode':settings.mode,'version':'0.1.0'}))
            return 0
        from .server import run
        run(settings)
        return 0
    except KnowledgeError as error:
        print(json.dumps({'error':error.public()}),file=sys.stderr)
        return 2
    except ModuleNotFoundError as error:
        if error.name != 'mcp_backend.server':
            raise
        print(json.dumps({'error':{'code':'NOT_IMPLEMENTED','message':'MCP transport unit is not implemented yet.'}}),file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
