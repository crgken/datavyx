"""Lifecycle predicates shared by all read and write paths."""
from .domain import available


def present(resource):
    return not resource.get('deleted_by') and resource.get('record_state','active')!='action_draft'


def current_fact(fact,objects):
    return present(fact) and fact.get('active',True) and all(
        oid in objects and present(objects[oid]) for oid in (fact.get('subject_id'),fact.get('object_id')) if oid)


def current_source(tx,dataset,source):
    if not present(source) or source.get('history_only'):return False
    # A complete mixed source is restricted if ANY supported item became historical,
    # deleted or unavailable. No heuristic text slicing can prove safe disclosure.
    rows=tx.run('MATCH (s:KDSource {dataset:$d,id:$id})-[:KD_SUPPORTS]->(n) '
        'RETURN labels(n) AS labels,properties(n) AS n',d=dataset,id=source['id'])
    for row in rows:
        n=row['n']
        if 'KDObject' in row['labels'] and (not available(n) or n.get('source_visibility_barrier')):return False
        if 'KDFact' in row['labels']:
            if not present(n) or not n.get('active',True):return False
            for oid in (n.get('subject_id'),n.get('object_id')):
                if oid:
                    obj=tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) RETURN properties(o) AS o',d=dataset,id=oid).single()
                    if not obj or not available(obj['o']):return False
    return True
