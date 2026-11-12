"""Export current canonical data in a bounded transaction that ALWAYS rolls back.

Legacy canonical writers lock KDData. Taking those same node locks freezes only
the short export, without stopping services or committing any property change.
This logical validation copy is not the final offline/volume rollback backup.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace


def _datasets(tx, selection=None):
    return [r['d'] for r in tx.run('MATCH (n) WHERE n:KDData OR n:KDWorkspace OR n:KDObject OR n:KDFact OR n:KDSource '
        'WITH DISTINCT CASE WHEN n:KDData THEN n.id ELSE n.dataset END AS d '
        'WHERE d IS NOT NULL AND ($selection IS NULL OR d IN $selection) RETURN d ORDER BY d LIMIT 101', selection=selection)]


def export_locked(repo, selection=None):
    from mcp_backend.domain import digest
    from mcp_backend.errors import require
    from mcp_backend.migrations.snapshot import export_snapshot
    from mcp_backend.neo4j_repository import Repository
    started = time.monotonic()
    with repo.driver.session(database=repo.settings.database) as session:
        tx = session.begin_transaction(timeout=10)
        try:
            if selection is None:
                missing = tx.run('MATCH (n) WHERE (n:KDObject OR n:KDFact OR n:KDSource) '
                    'AND n.dataset IS NULL RETURN count(n) AS n').single()['n']
                require(missing == 0, 'INVALID_SOURCE', 'Unscoped canonical records require investigation.', 409)
            datasets = _datasets(tx, selection)
            require(len(datasets) <= 100, 'LIMIT_EXCEEDED', 'Too many datasets for a bounded validation copy.', 413)
            require(selection is None or set(selection) == set(datasets), 'INVALID_SOURCE', 'An exact dataset was not found.', 404)
            # Reassign the identical required ID, so exported properties stay
            # identical even before rollback. Never create a marker property.
            tx.run('MATCH (n) WHERE (n:KDData AND n.id IN $datasets) OR '
                   '((n:KDWorkspace OR n:KDWorkspaceMember) AND n.dataset IN $datasets) '
                   'WITH n ORDER BY elementId(n) SET n.id=n.id RETURN count(n) AS n', datasets=datasets).consume()
            bindings = [dict(r['p']) for r in tx.run('MATCH (n) WHERE n:KDWorkspace OR n:KDWorkspaceMember '
                'WITH n WHERE n.dataset IN $datasets RETURN properties(n) AS p ORDER BY n.id', datasets=datasets)]
            adapter = SimpleNamespace(settings=repo.settings, transaction=lambda fn: fn(tx), revision=Repository.revision)
            snapshots = [export_snapshot(adapter, dataset) for dataset in datasets]
            for snapshot in snapshots:
                snapshot['body']['consistency'] = 'KDData_node_locks_always_rollback'
                snapshot['sha256'] = digest(snapshot['body'])
            after = [dict(r['p']) for r in tx.run('MATCH (n) WHERE n:KDWorkspace OR n:KDWorkspaceMember '
                'WITH n WHERE n.dataset IN $datasets RETURN properties(n) AS p ORDER BY n.id', datasets=datasets)]
            require(datasets == _datasets(tx, selection) and digest(bindings) == digest(after), 'STALE_SNAPSHOT',
                    'Workspace membership changed during export. Retry.', 409)
            result = {'version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
                'method': 'legacy_KDData_locks_always_rollback', 'snapshots': snapshots,
                'source_properties_committed': False, 'snapshot_sha256': digest(snapshots)}
        finally:
            tx.rollback()
            tx.close()
    return {**result, 'lock_duration_ms': int((time.monotonic()-started)*1000)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', required=True, help='Verified MCP backend path for pure snapshot code.')
    parser.add_argument('--source-env', required=True, help='Protected legacy production DB env; never printed.')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    import sys
    sys.path.insert(0, args.backend)
    # Operator only: use the existing legacy venv's dotenv parser, not its Store/Agent.
    from dotenv import dotenv_values
    from mcp_backend.neo4j_repository import Repository
    from mcp_backend.settings import Settings
    try:
        values = dotenv_values(args.source_env)
        assert values.get('KD_NEO4J_URI', 'bolt://127.0.0.1:17693') == 'bolt://127.0.0.1:17693'
        target = Path(args.output).resolve()
        assert target.is_relative_to(Path('/home/ubuntu/atlasai-deploy/mcp')) and not target.exists()
        settings = Settings(uri='bolt://127.0.0.1:17693', database=values.get('KD_NEO4J_DATABASE', 'neo4j'),
            user=values.get('KD_NEO4J_USER', 'neo4j'), password=values['KD_NEO4J_PASSWORD'],
            issuer='https://dev-zto67ad0zoj5nzmb.au.auth0.com/', audience='https://kb.datavyx.com/mcp',
            resource_url='https://kb.datavyx.com/mcp', clients=('operator-export-only',), cursor_secret='unused-export-only-'*3)
        repo = Repository(settings)
        try: result = export_locked(repo)
        finally: repo.close()
        fd = os.open(target, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream: json.dump(result, stream, ensure_ascii=False, sort_keys=True)
        print(json.dumps({k:v for k,v in result.items() if k != 'snapshots'} | {'datasets': len(result['snapshots'])}))
        return 0
    except Exception as error:
        print(json.dumps({'error': 'Current validation copy failed; transaction rolled back.', 'exception_type': type(error).__name__}))
        return 2


if __name__ == '__main__': raise SystemExit(main())
