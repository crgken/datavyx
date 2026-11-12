"""MCP-specific entry point, independent from demo.__main__."""
import argparse
import json
import sys
from .errors import KnowledgeError
from .settings import Settings
from . import __version__


def main():
    parser = argparse.ArgumentParser()
    action=parser.add_mutually_exclusive_group()
    action.add_argument('--check-config', action='store_true',help='Validate configuration without connecting or writing.')
    action.add_argument('--migrate', action='store_true',help='Explicitly apply idempotent database constraints/version.')
    action.add_argument('--rollback-check',action='store_true',help='Read-only check before returning to a legacy writer.')
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if args.check_config:
            print(json.dumps({'status':'configuration_valid','mode':settings.mode,'version':__version__}))
            return 0
        if args.migrate:
            from .neo4j_repository import Repository
            repo = Repository(settings)
            try: repo.migrate()
            finally: repo.close()
            print(json.dumps({'status':'schema_ready','schema_version':1,'lifecycle_version':2}))
            return 0
        if args.rollback_check:
            from .neo4j_repository import Repository
            from .errors import require
            repo=Repository(settings)
            try:
                count=repo.transaction(lambda tx:tx.run('MATCH (n) WHERE n:KDMCPRequest OR n:KDMCPChange OR n:KDMCPPurgeLedger '
                    'OR ((n:KDObject OR n:KDFact OR n:KDSource) AND (n.deleted_by IS NOT NULL OR n.superseded_by IS NOT NULL '
                    'OR coalesce(n.source_visibility_barrier,false) OR coalesce(n.version,1)>1)) RETURN count(n) AS n').single()['n'])
                from .purge_ledger import PurgeLedger
                require(count==0 and not PurgeLedger(settings).entries(),'ROLLBACK_INCOMPATIBLE',
                    'MCP writes or erasure manifests exist. Use lifecycle-compatible code; do not return to the legacy writer.',409)
            finally:repo.close()
            print(json.dumps({'status':'legacy_rollback_compatible'}));return 0
        from .server import run
        run(settings)
        return 0
    except KnowledgeError as error:
        print(json.dumps({'error':error.public()}),file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
