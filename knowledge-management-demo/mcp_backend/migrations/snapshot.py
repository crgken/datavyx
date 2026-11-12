"""Protected logical snapshots for an already quiesced copy; restore only to isolated test DB."""
from ..domain import digest
from ..errors import require


LABELS = ('KDObject', 'KDFact', 'KDSource', 'KDData', 'KDWorkspace', 'KDWorkspaceMember', 'KDMCPRequest', 'KDMCPMigration')
EDGES = {'KD_SUPPORTS': {('KDSource', 'KDObject'), ('KDSource', 'KDFact')},
         'KD_HAS_FACT': {('KDObject', 'KDFact')}, 'KD_TARGET': {('KDFact', 'KDObject')}}


def export_snapshot(repo, dataset):
    """Caller must stop writers on a COPY. This is not an online volume backup."""
    def read(tx):
        revision = repo.revision(tx, dataset)
        nodes = []
        edges = []
        for label in LABELS:
            condition = 'n.id=$d' if label == 'KDData' else 'n.dataset=$d'
            rows = [dict(row['p']) for row in tx.run(f'MATCH (n:{label}) WHERE {condition} '
                'RETURN properties(n) AS p ORDER BY n.id LIMIT $n', d=dataset, n=repo.settings.max_records+1)]
            require(len(rows) <= repo.settings.max_records, 'LIMIT_EXCEEDED', 'The snapshot exceeds its resource limit.', 413)
            nodes.extend({'label': label, 'properties': properties} for properties in rows)
        for typ, pairs in sorted(EDGES.items()):
            for left, right in sorted(pairs):
                edges.extend({'type': typ, 'from_label': left, 'to_label': right, **dict(row)} for row in tx.run(
                    f'MATCH (a:{left} {{dataset:$d}})-[e:{typ}]->(b:{right} {{dataset:$d}}) '
                    'RETURN a.id AS source,b.id AS target,properties(e) AS properties ORDER BY source,target', d=dataset))
        edges.sort(key=lambda e:(e['type'], e['from_label'], e['source'], e['target'], digest(e['properties'])))
        require(repo.revision(tx, dataset) == revision, 'STALE_SNAPSHOT', 'Data changed during snapshot creation.', 409)
        body = {'version': 1, 'dataset': dataset, 'revision': revision, 'nodes': nodes, 'edges': edges,
                'consistency': 'caller_quiesced_copy_required'}
        return {'body': body, 'sha256': digest(body)}
    return repo.transaction(read)


def restore_test_snapshot(repo, snapshot):
    require(repo.settings.mode == 'test' and repo.settings.uri == 'bolt://127.0.0.1:17694',
            'PERMISSION_DENIED', 'Logical restore is restricted to the isolated test database.', 403)
    body = snapshot.get('body', {})
    require(snapshot.get('sha256') == digest(body) and body.get('version') == 1,
            'INVALID_SNAPSHOT', 'The snapshot checksum or version is invalid.')
    dataset = body.get('dataset')
    require(isinstance(dataset, str) and 0 < len(dataset) <= 200, 'INVALID_SNAPSHOT', 'The dataset is invalid.')
    nodes, edges = body.get('nodes', []), body.get('edges', [])
    require(len(nodes) <= len(LABELS)*repo.settings.max_records and len(edges) <= 3*repo.settings.max_records,
            'LIMIT_EXCEEDED', 'The snapshot exceeds restore limits.', 413)
    keys = set()
    for node in nodes:
        label, props = node.get('label'), node.get('properties', {})
        require(label in LABELS and isinstance(props.get('id'), str) and props['id'] and
                (props['id'] == dataset if label == 'KDData' else props.get('dataset') == dataset),
                'INVALID_SNAPSHOT', 'A node is outside the snapshot scope.')
        identity = (label, props['id'])
        require(identity not in keys, 'INVALID_SNAPSHOT', 'Duplicate snapshot node.')
        keys.add(identity)
    for edge in edges:
        pair = (edge.get('from_label'), edge.get('to_label'))
        require(pair in EDGES.get(edge.get('type'), set()) and
                (pair[0], edge.get('source')) in keys and (pair[1], edge.get('target')) in keys,
                'INVALID_SNAPSHOT', 'A relationship is outside the snapshot scope.')
    def restore(tx):
        occupied = tx.run('MATCH (n) WHERE n.dataset=$d OR (n:KDData AND n.id=$d) RETURN count(n) AS n', d=dataset).single()['n']
        require(occupied == 0, 'RESTORE_TARGET_NOT_EMPTY', 'Restore requires an empty target dataset.', 409)
        for node in nodes:
            label, props = node['label'], node['properties']
            exists = tx.run(f'MATCH (n:{label} {{id:$id}}) RETURN n.id AS id', id=props['id']).single()
            require(not exists, 'RESTORE_ID_CONFLICT', 'A snapshot ID already exists in another dataset.', 409)
            tx.run(f'CREATE (n:{label}) SET n=$p', p=props).consume()
        for edge in edges:
            tx.run(f'MATCH (a:{edge["from_label"]} {{id:$a}}),(b:{edge["to_label"]} {{id:$b}}) '
                f'CREATE (a)-[e:{edge["type"]}]->(b) SET e=$p', a=edge['source'], b=edge['target'], p=edge['properties']).consume()
        return {'status': 'restored_test_copy', 'sha256': snapshot['sha256'], 'nodes': len(nodes), 'edges': len(edges)}
    return repo.transaction(restore)
