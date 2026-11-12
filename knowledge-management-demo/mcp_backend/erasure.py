"""Content erasure and backup reapplication share the same finite resource plan."""
import time
from .domain import encoded
from .errors import require


def related_events(events,resources):
    sources=set(resources['source']);bound=set(resources['entity'])|set(resources['fact'])
    chosen={}
    deadline=time.monotonic()+5
    while True:
        require(time.monotonic()<deadline,'LIMIT_EXCEEDED','Erasure closure exceeds its computation budget.',413)
        before=len(chosen),len(sources)
        for event in events:
            if (event['target_id'] in bound|sources or set(event.get('entity_ids',[])) & set(resources['entity'])
                or any(fid in event.get('payload','') for fid in resources['fact'])):
                chosen[event['id']]=event;sources.update(event.get('source_ids',[]))
        if before==(len(chosen),len(sources)):break
    return list(chosen.values()),sources


def read_events(repo,tx,dataset):
    events=[dict(r['e']) for r in tx.run('MATCH (e:KDMCPChange {dataset:$d}) RETURN properties(e) AS e LIMIT $n',
        d=dataset,n=repo.settings.max_records+1)]
    require(len(events)<=repo.settings.max_records and sum(len(e.get('payload','')) for e in events)<=16*1024*1024,
            'LIMIT_EXCEEDED','Erasure history exceeds the atomic resource limit.',413)
    return events


def erase_content(tx,dataset,resources,events):
    for resource,label in {'entity':'KDObject','fact':'KDFact','source':'KDSource'}.items():
        tx.run(f'MATCH (n:{label} {{dataset:$d}}) WHERE n.id IN $ids DETACH DELETE n',
            d=dataset,ids=resources[resource]).consume()
    tx.run('MATCH (e:KDMCPChange {dataset:$d}) WHERE e.id IN $ids DETACH DELETE e',
        d=dataset,ids=[e['id'] for e in events]).consume()
    erased=set().union(*map(set,resources.values()))
    requests=list(tx.run('MATCH (r:KDMCPRequest {dataset:$d}) WHERE '
        'any(id IN $ids WHERE id IN coalesce(r.affected_ids,[]) OR id IN coalesce(r.source_ids,[]) OR r.receipt CONTAINS id) '
        'OR r.id IN $operations RETURN r.id AS id',d=dataset,ids=sorted(erased),operations=sorted({e['operation_id'] for e in events})))
    for request in requests:
        tx.run('MATCH (r:KDMCPRequest {dataset:$d,id:$id}) SET r.purged=true,r.fingerprint="",r.request_id="",'
            'r.source_ids=[],r.affected_ids=[],r.resource_versions="[]",r.receipt=$receipt',d=dataset,id=request['id'],
            receipt=encoded({'schema_version':'2.0','status':'purged','receipt_id':request['id']})).consume()


def reapply_committed(repo,dataset,authorized=False):
    from .purge_ledger import PurgeLedger
    require(repo.settings.mode=='test' or authorized,'PERMISSION_DENIED','Production erasure recovery requires release authorization.',403)
    ledger=PurgeLedger(repo.settings);entries=ledger.entries(dataset)
    require(all(e['state']!='prepared' for e in entries),'RECOVERY_REQUIRED','Resolve pending erasure intents before restoring a backup.',503)
    for body in entries:
        if body['state']!='committed':continue
        def apply(tx):
            repo.revision(tx,dataset,write=True)
            events=read_events(repo,tx,dataset)
            chosen,_=related_events(events,body['resources'])
            erase_content(tx,dataset,body['resources'],chosen)
            tx.run('MERGE (p:KDMCPPurgeLedger {dataset:$d,id:$id}) SET p.plan_digest=$digest,p.resources=$resources',
                d=dataset,id=body['operation_id'],digest=body['plan_digest'],resources=encoded(body['resources'])).consume()
            tx.run('MATCH (d:KDData {id:$d}) SET d.revision=coalesce(d.revision,0)+1',d=dataset).consume()
        repo.transaction(apply)
    repo.transaction(lambda tx:ledger.check(tx,dataset))
