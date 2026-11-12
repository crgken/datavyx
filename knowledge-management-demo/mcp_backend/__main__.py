"""MCP-specific entry point, independent from demo.__main__."""
import argparse
import json
import sys
from .errors import KnowledgeError
from .settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-config', action='store_true',help='Validate configuration without connecting or writing.')
    parser.add_argument('--migrate', action='store_true',help='Explicitly apply idempotent database constraints/version.')
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if args.check_config:
            print(json.dumps({'status':'configuration_valid','mode':settings.mode,'version':'0.1.0'}))
            return 0
        if args.migrate:
            from .neo4j_repository import Repository
            repo = Repository(settings)
            try: repo.migrate()
            finally: repo.close()
            print(json.dumps({'status':'schema_ready','schema_version':1}))
            return 0
        from .server import run
        run(settings)
        return 0
    except KnowledgeError as error:
        print(json.dumps({'error':error.public()}),file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
