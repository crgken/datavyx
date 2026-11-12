"""Operator-only audit/apply CLI. Not registered as an MCP tool."""
import argparse
import json
from mcp_backend.errors import KnowledgeError
from mcp_backend.migrations.legacy import audit, migrate
from mcp_backend.neo4j_repository import Repository
from mcp_backend.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', required=True, help='Exact operator-selected dataset; no wildcard.')
    parser.add_argument('--apply', action='store_true', help='Explicitly apply only audited additive migration.')
    parser.add_argument('--expected-fingerprint', default='')
    args = parser.parse_args()
    repo = None
    try:
        repo = Repository(Settings.from_env())
        if args.apply:
            if len(args.expected_fingerprint) != 64:
                parser.error('--apply requires the exact audit --expected-fingerprint')
            result = migrate(repo, args.dataset, args.expected_fingerprint)
        else:
            result = audit(repo, args.dataset)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get('ready', True) else 3
    except KnowledgeError as error:
        print(json.dumps({'error': error.public()}))
        return 2
    finally:
        if repo: repo.close()


if __name__ == '__main__': raise SystemExit(main())
