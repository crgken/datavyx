"""Complete deterministic target matching, independent of statistical COUNT."""
from collections import Counter
from .domain import digest,eligibility,key
from .errors import require
from .lifecycle import present,current_fact
from .query import _matches


LABELS={'entity':'KDObject','fact':'KDFact','source':'KDSource'}


def select_targets(repo,tx,scope,selection):
    if selection.state!='present':repo.authorize(tx,scope,'history')
    objects,facts=repo.snapshot(tx,scope)
    if selection.state=='all':
        # Explicit historical erasure covers old canonical proposal bodies too;
        # it never activates or executes a historical proposal.
        objects={r['n']['id']:dict(r['n']) for r in tx.run('MATCH (n:KDObject {dataset:$d}) RETURN properties(n) AS n LIMIT $n',d=scope.dataset,n=repo.settings.max_records+1)}
        facts=[dict(r['n']) for r in tx.run('MATCH (n:KDFact {dataset:$d}) RETURN properties(n) AS n LIMIT $n',d=scope.dataset,n=repo.settings.max_records+1)]
        require(max(len(objects),len(facts))<=repo.settings.max_records,'LIMIT_EXCEEDED','Historical selection exceeds its resource limit.',413)
    sources=[dict(r['s']) for r in tx.run("MATCH (s:KDSource {dataset:$d}) WHERE $history OR coalesce(s.record_state,'active')<>'action_draft' RETURN properties(s) AS s LIMIT $n",d=scope.dataset,n=repo.settings.max_records+1,history=selection.state=='all')]
    require(len(sources)<=repo.settings.max_records,'LIMIT_EXCEEDED','Source selection exceeds the resource limit.',413)
    pools={'entity':list(objects.values()),'fact':facts,'source':sources}
    reasons=eligibility(objects,facts) if selection.filters else {}
    selected=[]
    for resource in LABELS if selection.resource=='workspace' else [selection.resource]:
        for row in pools[resource]:
            if selection.state!='all' and bool(row.get('deleted_by')) != (selection.state=='deleted'):continue
            if selection.ids and row['id'] not in selection.ids:continue
            if selection.state=='present':
                if resource=='fact' and not current_fact(row,objects):continue
                if resource=='source' and row.get('history_only'):continue
            if resource=='entity':
                if any(value and key(row.get(field,''))!=key(value) for field,value in (
                    ('kind',selection.kind),('namespace',selection.namespace),('identifier',selection.identifier))):continue
                matches=True
                for condition in selection.filters:
                    slot=[f for f in facts if f['predicate']==condition.predicate and
                        f.get('subject_id' if condition.direction=='out' else 'object_id')==row['id'] and current_fact(f,objects)]
                    require(not any(reasons[f['id']] for f in slot),'AMBIGUOUS_SELECTION',
                            'A target filter depends on conflicting or uncertain facts.',409)
                    if not any(_matches(f,condition) for f in slot):matches=False;break
                if not matches:continue
            if resource=='fact' and (selection.subject_id and row.get('subject_id')!=selection.subject_id
                or selection.predicate and row.get('predicate')!=selection.predicate):continue
            selected.append({'resource':resource,'id':row['id'],'version':row.get('version',1)})
    selected.sort(key=lambda x:(x['resource'],x['id']))
    if selection.ids:
        require({r['id'] for r in selected}==set(selection.ids),'INVALID_REFERENCE',
                'One or more selected targets are unavailable in this workspace.',409)
    require(len(selected)<=1000,'LIMIT_EXCEEDED','A mutation selection may contain at most 1000 resources.',413)
    return selected,digest([scope.dataset,selection.model_dump(),selected]),dict(Counter(r['resource'] for r in selected))
