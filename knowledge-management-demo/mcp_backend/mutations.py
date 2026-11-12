"""Deterministic lifecycle transactions. No approval state or model inference."""
from copy import deepcopy
import json
import time
from uuid import uuid4
from .contracts import RecordInput,NumberValue
from .domain import (digest,encoded,identity_key,literal_fields,validate_record,fact_value,
                     value_key,certainty,available)
from .errors import require
from .ingestion import Ingestion,request_key
from .lifecycle import present,current_fact,operation_state
from .paging import Pager
from .purge_ledger import PurgeLedger
from .erasure import related_events,read_events,erase_content
from .unit_labels import unit_label_matches
from collections import Counter
import hashlib
import hmac
from .selection import LABELS,select_targets


class Mutations:
    def __init__(self,repository):self.repo=repository

    def _execute(self,scope,data,operation,apply):
        rid=request_key(scope,data.request_id)
        body=[operation,data.model_dump(exclude={'workspace_id'})]
        fingerprint=(hmac.new((self.repo.settings.purge_ledger_secret or self.repo.settings.cursor_secret).encode(),encoded(body).encode(),hashlib.sha256).hexdigest()
                     if operation=='purge' else digest(body))
        def run(tx,revision):
            previous=tx.run('MATCH (r:KDMCPRequest {dataset:$d,id:$id}) RETURN properties(r) AS r',
                            d=scope.dataset,id=rid).single()
            if previous:
                old=previous['r']
                require(not old.get('purged'),'PREVIOUSLY_PURGED','This request was purged and cannot be replayed.',409)
                require(old.get('fingerprint')==fingerprint,'IDEMPOTENCY_CONFLICT','This request ID belongs to another body or operation.',409)
                return {**json.loads(old['receipt']),'idempotent':True,
                        'current_state':operation_state(tx,scope.dataset,old)}
            require(self.repo.settings.mode=='test' or self.repo.settings.recording_enabled
                    or scope.actor in self.repo.settings.recording_actors,'MAINTENANCE',
                    'New writes are temporarily disabled. Queries and receipts remain available.',503)
            objects,facts=self.repo.snapshot(tx,scope)
            ctx={'scope':scope,'data':data,'operation':operation,'rid':rid,'revision':revision,
                'objects':objects,'facts':{f['id']:f for f in facts},'versions':{i:o.get('version',1) for i,o in objects.items()},
                'source_ids':{s.ref:uuid4().hex for s in data.sources},'sources_created':False,
                'changed':{'entity':set(),'fact':set(),'source':set()},'touched':set(),
                'target_counts':{},'removed_counts':{},
                'matched':0,'applied':0,'relations_hidden':0,'now':time.time()}
            apply(tx,ctx)
            changed=any(ctx['changed'].values())
            after=revision+int(changed)
            receipt={'schema_version':'2.0','request_id':data.request_id,'receipt_id':rid,'operation':operation,
                'status':'applied' if changed else 'noop','revision_before':revision,'revision':after,
                'matched_count':ctx['matched'],'applied_count':ctx['applied'],
                'objects_changed':len(ctx['changed']['entity']),'facts_changed':len(ctx['changed']['fact']),
                'sources_changed':len(ctx['changed']['source']),
                'sources_recorded':int(ctx['sources_created'])*len(data.sources),
                'relations_hidden':ctx['relations_hidden'],'recoverable':operation=='delete',
                'selection_complete':True,'idempotent':False,'current_state':'current',
                'target_counts':ctx['target_counts'],'removed_counts':ctx['removed_counts']}
            versions=[]
            for resource,label in LABELS.items():
                versions.extend({'label':label,'id':r['id'],'version':r['version']} for r in tx.run(
                    f'MATCH (n:{label} {{dataset:$d}}) WHERE n.id IN $ids RETURN n.id AS id,coalesce(n.version,1) AS version',
                    d=scope.dataset,ids=sorted(ctx['changed'][resource])))
            tx.run('CREATE (r:KDMCPRequest) SET r=$r',r={'id':rid,'dataset':scope.dataset,
                'workspace_id':scope.workspace_id,'actor':scope.actor,'client_id':scope.client_id,
                'request_id':data.request_id,'fingerprint':fingerprint,'receipt':encoded(receipt),
                'operation':operation,'created_at':ctx['now'],
                'resource_versions':encoded(versions),
                'affected_ids':sorted(set().union(*ctx['changed'].values())),
                'source_ids':list(ctx['source_ids'].values()) if ctx['sources_created'] else []}).consume()
            if changed:
                tx.run('MATCH (d:KDData {id:$d}) SET d.revision=$revision',d=scope.dataset,revision=after).consume()
            return receipt
        result=self.repo.scoped(scope,run,operation,{'mcp_operation':rid,'mcp_operation_type':operation})
        if operation=='purge' and result['status']=='applied':PurgeLedger(self.repo.settings).confirm(scope.dataset,rid)
        return result

    def _ensure_sources(self,tx,ctx):
        if ctx['sources_created'] or ctx['operation']=='purge':return
        scope=ctx['scope']
        count=tx.run('MATCH (s:KDSource {dataset:$d}) RETURN count(s) AS n',d=scope.dataset).single()['n']
        require(count+len(ctx['data'].sources)<=self.repo.settings.max_records,'LIMIT_EXCEEDED','Source limit exceeded.',413)
        for source in ctx['data'].sources:
            tx.run('CREATE (s:KDSource) SET s=$s',s={'id':ctx['source_ids'][source.ref],
                'dataset':scope.dataset,'message':source.text,'external_ref':source.external_ref,
                'created_at':ctx['now'],'recorded_at':ctx['now'],'recorded_by':scope.actor,
                'origin_channel':'mcp','valid':True,'history_only':True,'version':1}).consume()
        ctx['sources_created']=True

    def _journal(self,tx,ctx,resource,target,before,after,related=()):
        self._ensure_sources(tx,ctx)
        tx.run('CREATE (e:KDMCPChange) SET e=$e',e={'id':uuid4().hex,'dataset':ctx['scope'].dataset,
            'operation_id':ctx['rid'],'operation':ctx['operation'],'resource':resource,'target_id':target,
            'entity_ids':sorted(set(related)|({target} if resource=='entity' else set())),
            'source_ids':list(ctx['source_ids'].values()),'recorded_at':ctx['now'],'actor':ctx['scope'].actor,
            'payload':encoded({'before':before,'after':after})}).consume()

    def _touch(self,tx,ctx,ids):
        for oid in set(ids)-{None,''}:
            if oid not in ctx['touched']:
                ctx['touched'].add(oid)
                if oid in ctx['objects']:
                    ctx['changed']['entity'].add(oid)
                    ctx['objects'][oid]['version']=ctx['versions'][oid]+1
                    tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) SET o.version=$version',
                        d=ctx['scope'].dataset,id=oid,version=ctx['objects'][oid]['version']).consume()

    def update_information(self,scope,data):
        return self._execute(scope,data,'update',self._update)

    def _update(self,tx,ctx):
        scope=ctx['scope'];data=ctx['data'];ctx['matched']=len(data.changes)
        ctx['target_counts']={'entity':len({c.entity_id for c in data.changes})}
        for change in data.changes:
            obj=ctx['objects'].get(change.entity_id)
            require(obj and present(obj),'INVALID_REFERENCE','The target object is unavailable.',409)
            require(ctx['versions'][obj['id']]==change.expected_version,'VERSION_CONFLICT',
                    'The object changed. Read its current version before retrying.',409)
            if change.action=='set_entity_fields':
                values=change.fields.model_dump(exclude_unset=True)
                after={**obj,**values}
                require(bool(after.get('name','').strip() or after.get('identifier','').strip()),
                        'INVALID_ARGUMENT','An object needs a name or explicit identifier.')
                identity=identity_key(after['kind'],after.get('namespace',''),after.get('identifier',''))
                after['identity_key']=identity or 'new:'+obj['id']
                if all(obj.get(k)==v for k,v in after.items()):continue
                require(not identity or not tx.run('MATCH (o:KDObject {dataset:$d,identity_key:$key}) '
                    'WHERE o.id<>$id RETURN o.id AS id LIMIT 1',d=scope.dataset,key=identity,id=obj['id']).single(),
                    'IDENTITY_CONFLICT','The new explicit identity is already reserved.',409)
                before=deepcopy(obj)
                after['source_visibility_barrier']=True
                self._touch(tx,ctx,[obj['id']]);after['version']=obj['version']
                tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) SET o += $values',
                    d=scope.dataset,id=obj['id'],values={**values,'identity_key':after['identity_key'],'source_visibility_barrier':True}).consume()
                obj.update(after);ctx['changed']['entity'].add(obj['id']);ctx['applied']+=1
                self._journal(tx,ctx,'entity',obj['id'],before,after)
                Ingestion._support(tx,scope,'KDObject',obj['id'],change.quotes,ctx['source_ids'])
                continue
            current=[f for f in ctx['facts'].values() if f['subject_id']==obj['id']
                and f['predicate']==change.predicate and current_fact(f,ctx['objects'])]
            selected=[f for f in current if f['id'] in change.fact_ids]
            require(len(selected)==len(change.fact_ids),'INVALID_REFERENCE','A target fact is missing or no longer current.',409)
            if change.action=='replace_slot':
                require({f['id'] for f in current}==set(change.fact_ids),'INCOMPLETE_SCOPE',
                        'Slot replacement must name every current statement, including conflicts.',409)
            if change.action=='replace_relation':require(bool(selected[0].get('object_id')),'INVALID_ARGUMENT','Target is not a relation.')
            expected_shape={(bool(f.get('object_id')),fact_value(f).get('type'),f.get('cardinality','one')) for f in selected}
            require(len({f.get('cardinality','one') for f in selected})==1,'SCHEMA_CONFLICT','Incompatible historical cardinalities.',409)
            cardinality=selected[0].get('cardinality','one')
            require(cardinality=='many' or len(change.replacements)==1,'SCHEMA_CONFLICT','A single-valued slot needs one replacement.',409)
            replacements=[]
            for replacement in change.replacements:
                value=replacement.value
                if replacement.inherit_unit:
                    units={fact_value(f).get('unit','') for f in selected if fact_value(f).get('type')=='number'}
                    require(len(units)==1 and all(fact_value(f).get('type')=='number' for f in selected),
                            'AMBIGUOUS_UNIT','The target facts do not establish one inherited unit.',409)
                    value=value.model_copy(update={'unit':next(iter(units))})
                elif value is not None:
                    validate_record(RecordInput(request_id=data.request_id,sources=data.sources,
                        entities=[{'ref':'target','action':'reference','entity_id':obj['id']}],
                        facts=[{'subject_ref':'target','predicate':change.predicate,'value':value.model_dump(),
                            'quotes':[q.model_dump() for q in replacement.quotes]}]))
                    if isinstance(value,NumberValue):
                        old_units={fact_value(f).get('unit','') for f in selected if fact_value(f).get('type')=='number'}
                        if len(old_units)==1:
                            original_unit=next(iter(old_units))
                            if unit_label_matches(original_unit,value.unit) or unit_label_matches(value.unit,original_unit):
                                # Evidence is checked against the submitted label above;
                                # preserve an equivalent old label to avoid splitting sums.
                                value=value.model_copy(update={'unit':original_unit})
                target=ctx['objects'].get(replacement.object_id) if replacement.object_id else None
                if replacement.object_id:require(target and available(target),'INVALID_REFERENCE','The relation target is unavailable.',409)
                shape=(bool(target),'entity' if target else value.type,cardinality)
                # fact_value for relations has type=entity.
                require(shape in expected_shape,'SCHEMA_CONFLICT','A correction cannot change the predicate value/target type.',409)
                if target:
                    old_kinds={ctx['objects'][f['object_id']]['kind'] for f in selected}
                    require(target['kind'] in old_kinds,'SCHEMA_CONFLICT','Relation target kind cannot change.',409)
                literal,scaled=({},None) if target else literal_fields(value)
                candidate={'id':uuid4().hex,'dataset':scope.dataset,'subject_id':obj['id'],
                    'predicate':change.predicate,'object_id':target['id'] if target else '',
                    'value_json':'' if target else encoded(literal),'number_scaled':scaled,
                    'value_type':'object' if target else literal['type'],'unit':literal.get('unit',''),
                    'cardinality':cardinality,'certainty':replacement.certainty,'active':True,'version':1,
                    'record_state':'active','recording_policy_version':2,'created_at':ctx['now'],
                    'recorded_at':ctx['now'],'recorded_by':scope.actor,'origin_channel':'mcp',
                    'supersedes':[f['id'] for f in selected]}
                candidate['value_key']=value_key(candidate)
                require(candidate['value_key'] not in {f['value_key'] for f,_ in replacements},
                        'INVALID_ARGUMENT','Replacement values must be distinct.')
                replacements.append((candidate,replacement.quotes))
            if ({(value_key(f),certainty(f)) for f in selected}=={(f['value_key'],f['certainty']) for f,_ in replacements}):continue
            self._ensure_sources(tx,ctx)
            before=deepcopy(selected)
            for old in selected:
                tx.run('MATCH (f:KDFact {dataset:$d,id:$id}) SET f.active=false,f.superseded_by=$r,f.version=coalesce(f.version,1)+1',
                       d=scope.dataset,id=old['id'],r=ctx['rid']).consume()
                old.update(active=False,superseded_by=ctx['rid'],version=old.get('version',1)+1)
                ctx['changed']['fact'].add(old['id'])
            for new,quotes in replacements:
                tx.run('CREATE (f:KDFact) SET f=$f WITH f MATCH (o:KDObject {dataset:$d,id:$subject}) '
                    'CREATE (o)-[:KD_HAS_FACT]->(f) WITH f OPTIONAL MATCH (t:KDObject {dataset:$d,id:$target}) '
                    'FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | CREATE (f)-[:KD_TARGET]->(t))',
                    f=new,d=scope.dataset,subject=obj['id'],target=new['object_id']).consume()
                ctx['facts'][new['id']]=new;ctx['changed']['fact'].add(new['id'])
                Ingestion._support(tx,scope,'KDFact',new['id'],quotes,ctx['source_ids'])
            self._touch(tx,ctx,[obj['id']]+[f.get('object_id') for f in selected]+[f['object_id'] for f,_ in replacements])
            ctx['changed']['entity'].add(obj['id']);ctx['applied']+=1
            self._journal(tx,ctx,'entity',obj['id'],before,[f for f,_ in replacements],
                [obj['id']]+[f.get('object_id') for f in selected if f.get('object_id')]
                +[f['object_id'] for f,_ in replacements if f['object_id']])
            require(len(ctx['facts'])<=self.repo.settings.max_records,'LIMIT_EXCEEDED','Fact history exceeds the resource limit.',413)

    def _selection(self,tx,ctx):
        data=ctx['data'];scope=ctx['scope']
        require(data.expected_revision==ctx['revision'],'STALE_SELECTION','The workspace changed after target selection.',409)
        targets,signature,_=select_targets(self.repo,tx,scope,data.selection)
        require(len(targets)==data.expected_count and signature==data.selection_digest,'INCOMPLETE_SCOPE',
                'The complete target range or count differs from the supplied selection.',409)
        if len(targets)>1 or data.selection.resource=='workspace':self.repo.authorize(tx,scope,'bulk')
        ctx['matched']=len(targets)
        ctx['target_counts']=dict(Counter(t['resource'] for t in targets))
        return targets

    def _source_dependencies(self,tx,ctx,source_ids,entity_ids,fact_ids):
        scope=ctx['scope']
        for sid in source_ids:
            for row in tx.run('MATCH (:KDSource {dataset:$d,id:$id})-[:KD_SUPPORTS]->(n) '
                'RETURN labels(n) AS labels,properties(n) AS n',d=scope.dataset,id=sid):
                node=row['n'];labels=row['labels']
                if 'KDObject' in labels:
                    if node['id'] in entity_ids or not present(node):continue
                elif 'KDFact' in labels:
                    if (node['id'] in fact_ids or node.get('subject_id') in entity_ids or node.get('object_id') in entity_ids
                        or not current_fact(node,ctx['objects'])):continue
                else:continue
                remaining=tx.run('MATCH (s:KDSource {dataset:$d})-[:KD_SUPPORTS]->(n {dataset:$d,id:$id}) '
                    'WHERE NOT s.id IN $removed AND coalesce(s.valid,true) AND s.deleted_by IS NULL '
                    'RETURN count(DISTINCT s) AS n',d=scope.dataset,id=node['id'],removed=sorted(source_ids)).single()['n']
                require(remaining>0,'SOURCE_DEPENDENCY','A selected source is the last evidence for a record outside the target range.',409)

    def delete_information(self,scope,data):
        return self._execute(scope,data,'delete',self._delete)

    def _delete(self,tx,ctx):
        require(ctx['data'].selection.state=='present','INVALID_ARGUMENT','Ordinary deletion selects present resources.')
        targets=self._selection(tx,ctx);scope=ctx['scope']
        entities={t['id'] for t in targets if t['resource']=='entity'}
        facts={t['id'] for t in targets if t['resource']=='fact'}
        sources={t['id'] for t in targets if t['resource']=='source'}
        self._source_dependencies(tx,ctx,sources,entities,facts)
        affected=[f for f in ctx['facts'].values() if current_fact(f,ctx['objects']) and
            (f['id'] in facts or f['subject_id'] in entities or f.get('object_id') in entities)]
        ctx['relations_hidden']=sum(bool(f.get('object_id')) for f in affected)
        ctx['changed']['fact'].update(f['id'] for f in affected)
        changed_ids=entities|{f['id'] for f in affected}
        for r in tx.run('MATCH (s:KDSource {dataset:$d})-[:KD_SUPPORTS]->(n) WHERE n.id IN $ids '
                        'RETURN DISTINCT s.id AS id',d=scope.dataset,ids=sorted(changed_ids)):
            ctx['changed']['source'].add(r['id'])
        self._touch(tx,ctx,[oid for f in affected for oid in (f.get('subject_id'),f.get('object_id'))])
        for target in targets:
            resource=target['resource'];label=LABELS[resource];tid=target['id']
            row=tx.run(f'MATCH (n:{label} {{dataset:$d,id:$id}}) RETURN properties(n) AS n',d=scope.dataset,id=tid).single()
            require(row,'INVALID_REFERENCE','A target disappeared.',409)
            before=dict(row['n'])
            if resource=='entity':before['version']=ctx['versions'][tid]
            after={**before,'deleted_by':ctx['rid'],'deleted_at':ctx['now']}
            if resource=='entity':
                self._touch(tx,ctx,[tid]);after['version']=ctx['objects'][tid]['version']
            else:after['version']=before.get('version',1)+1
            tx.run(f'MATCH (n:{label} {{dataset:$d,id:$id}}) SET n.deleted_by=$op,n.deleted_at=$at,n.version=$version',
                   d=scope.dataset,id=tid,op=ctx['rid'],at=ctx['now'],version=after['version']).consume()
            ctx['changed'][resource].add(tid);ctx['applied']+=1
            related=([before.get('subject_id'),before.get('object_id')] if resource=='fact' else [])
            related=[i for i in related if i]
            self._journal(tx,ctx,resource,tid,before,after,related)
            if resource=='entity':ctx['objects'][tid].update(after)
            if resource=='fact':ctx['facts'][tid].update(after)

    def restore_information(self,scope,data):
        return self._execute(scope,data,'restore',self._restore)

    def _restore(self,tx,ctx):
        scope=ctx['scope'];data=ctx['data']
        require(data.expected_revision==ctx['revision'],'STALE_SELECTION','The workspace changed before restoration.',409)
        row=tx.run('MATCH (r:KDMCPRequest {dataset:$d,id:$id,operation:"delete"}) RETURN properties(r) AS r',
            d=scope.dataset,id=data.delete_receipt_id).single()
        require(row and not row['r'].get('purged'),'INVALID_REFERENCE','This deletion is unavailable or permanently purged.',409)
        changes=[dict(r['e']) for r in tx.run('MATCH (e:KDMCPChange {dataset:$d,operation_id:$op,operation:"delete"}) '
            'RETURN properties(e) AS e ORDER BY e.resource,e.target_id LIMIT 1001',d=scope.dataset,op=data.delete_receipt_id)]
        require(len(changes)<=1000,'LIMIT_EXCEEDED','This restoration exceeds the atomic limit.',413)
        if len(changes)>1:self.repo.authorize(tx,scope,'bulk')
        ctx['matched']=len(changes);restore=[]
        ctx['target_counts']=dict(Counter(e['resource'] for e in changes))
        for event in changes:
            resource=event['resource'];label=LABELS[resource]
            target=tx.run(f'MATCH (n:{label} {{dataset:$d,id:$id}}) RETURN properties(n) AS n',d=scope.dataset,id=event['target_id']).single()
            require(target,'RESTORE_CONFLICT','A resource was permanently removed after deletion.',409)
            node=dict(target['n'])
            require(not node.get('deleted_by') or node['deleted_by']==data.delete_receipt_id,
                    'RESTORE_CONFLICT','A later deletion owns this resource; it cannot be undone by this request.',409)
            if node.get('deleted_by')==data.delete_receipt_id:restore.append((event,node))
        before_current={f['id'] for f in ctx['facts'].values() if current_fact(f,ctx['objects'])}
        future_entities={e['target_id'] for e,_ in restore if e['resource']=='entity'}
        for event,node in restore:
            if event['resource']=='fact' and node.get('object_id'):
                require(all(oid in ctx['objects'] and (present(ctx['objects'][oid]) or oid in future_entities)
                    for oid in (node['subject_id'],node['object_id'])),
                    'RESTORE_DEPENDENCY','A relation endpoint is deleted or missing outside this restoration.',409)
        for event,before in restore:
            resource=event['resource'];label=LABELS[resource];tid=before['id']
            after={k:v for k,v in before.items() if k not in {'deleted_by','deleted_at'}}
            if resource=='entity':
                self._touch(tx,ctx,[tid]);after['version']=ctx['objects'][tid]['version']
            else:after['version']=before.get('version',1)+1
            tx.run(f'MATCH (n:{label} {{dataset:$d,id:$id}}) REMOVE n.deleted_by,n.deleted_at SET n.version=$version',
                d=scope.dataset,id=tid,version=after['version']).consume()
            ctx['changed'][resource].add(tid);ctx['applied']+=1
            related=[i for i in (before.get('subject_id'),before.get('object_id')) if i]
            self._journal(tx,ctx,resource,tid,before,after,related)
            if resource=='entity':ctx['objects'][tid]=after
            if resource=='fact':ctx['facts'][tid]=after
        reactivated=[f for f in ctx['facts'].values() if f['id'] not in before_current and current_fact(f,ctx['objects'])]
        ctx['changed']['fact'].update(f['id'] for f in reactivated)
        self._touch(tx,ctx,[oid for f in reactivated for oid in (f.get('subject_id'),f.get('object_id'))])
        affected=set().union(ctx['changed']['entity'],ctx['changed']['fact'])
        for r in tx.run('MATCH (s:KDSource {dataset:$d})-[:KD_SUPPORTS]->(n) WHERE n.id IN $ids '
                        'RETURN DISTINCT s.id AS id',d=scope.dataset,ids=sorted(affected)):
            if r['id'] not in ctx['source_ids'].values():ctx['changed']['source'].add(r['id'])
        if restore:
            tx.run('MATCH (r:KDMCPRequest {dataset:$d,id:$id}) SET r.restored_by=$restored',
                   d=scope.dataset,id=data.delete_receipt_id,restored=ctx['rid']).consume()

    def get_history(self,scope,data):
        def read(tx,revision):
            if data.entity_id:
                require(tx.run('MATCH (n:KDObject {dataset:$d,id:$id}) RETURN n.id AS id',d=scope.dataset,id=data.entity_id).single(),
                    'NOT_FOUND','The history target is unavailable.',404)
            if data.source_id:
                require(tx.run('MATCH (n:KDSource {dataset:$d,id:$id}) RETURN n.id AS id',d=scope.dataset,id=data.source_id).single(),
                    'NOT_FOUND','The history target is unavailable.',404)
            rows=[dict(r['e']) for r in tx.run('MATCH (e:KDMCPChange {dataset:$d}) WHERE '
                '($entity IS NOT NULL AND ($entity IN e.entity_ids OR (e.resource="entity" AND e.target_id=$entity))) OR '
                '($source IS NOT NULL AND ($source IN e.source_ids OR (e.resource="source" AND e.target_id=$source))) OR '
                '($op IS NOT NULL AND e.operation_id=$op) RETURN properties(e) AS e ORDER BY e.recorded_at,e.id LIMIT $n',
                d=scope.dataset,entity=data.entity_id,source=data.source_id,op=data.operation_id,n=self.repo.settings.max_records+1)]
            require(len(rows)<=self.repo.settings.max_records,'LIMIT_EXCEEDED','History exceeds the resource limit.',413)
            public=[{k:v for k,v in row.items() if k not in {'dataset','payload'}}|json.loads(row['payload']) for row in rows]
            return {**Pager(self.repo.settings.cursor_secret).page(public,scope,revision,data,'history',data.cursor),'schema_version':'2.0'}
        return self.repo.scoped(scope,read,'history')

    def purge_information(self,scope,data):
        return self._execute(scope,data,'purge',self._purge)

    def _purge(self,tx,ctx):
        targets=self._selection(tx,ctx);scope=ctx['scope'];selection=ctx['data'].selection
        require(selection.state=='all','INCOMPLETE_SCOPE','Permanent erasure requires a full historical selection (state=all).',409)
        if not targets:return
        entities={t['id'] for t in targets if t['resource']=='entity'}
        facts={t['id'] for t in targets if t['resource']=='fact'}
        sources={t['id'] for t in targets if t['resource']=='source'}
        facts.update(r['id'] for r in tx.run('MATCH (f:KDFact {dataset:$d}) '
            'WHERE f.subject_id IN $objects OR f.object_id IN $objects RETURN f.id AS id',d=scope.dataset,objects=sorted(entities)))
        bound=entities|facts
        sources.update(r['id'] for r in tx.run('MATCH (s:KDSource {dataset:$d})-[:KD_SUPPORTS]->(n) '
            'WHERE n.id IN $ids RETURN DISTINCT s.id AS id',d=scope.dataset,ids=sorted(bound)))
        events,sources=related_events(read_events(self.repo,tx,scope.dataset),
            {'entity':entities,'fact':facts,'source':sources})
        # Shared raw blobs and shared instruction sources cannot be partly erased
        # without rewriting evidence. Refuse any external dependency, even historical.
        for row in tx.run('MATCH (s:KDSource {dataset:$d})-[:KD_SUPPORTS]->(n) '
            'WHERE s.id IN $sources RETURN n.id AS id',d=scope.dataset,sources=sorted(sources)):
            require(row['id'] in bound,'SOURCE_DEPENDENCY','A shared source supports a record outside the erasure range.',409)
        event_ids={e['id'] for e in events}
        outside=tx.run('MATCH (e:KDMCPChange {dataset:$d}) WHERE any(s IN e.source_ids WHERE s IN $sources) '
            'AND NOT e.id IN $events RETURN count(e) AS n',d=scope.dataset,sources=sorted(sources),events=sorted(event_ids)).single()['n']
        require(outside==0,'SOURCE_DEPENDENCY','A shared instruction source supports history outside the erasure range.',409)
        resources={'entity':sorted(entities),'fact':sorted(facts),'source':sorted(sources)}
        require(sum(map(len,resources.values()))<=3*self.repo.settings.max_records,'LIMIT_EXCEEDED','Erasure exceeds the atomic resource limit.',413)
        ledger=PurgeLedger(self.repo.settings);manifest=ledger.prepare(scope.dataset,ctx['rid'],resources)
        # After this durable intent exists, failures remain fail-closed until retry
        # or administrative reconciliation proves the transaction outcome.
        neighbours={oid for f in ctx['facts'].values() if f['id'] in facts
                    for oid in (f.get('subject_id'),f.get('object_id')) if oid and oid not in entities}
        self._touch(tx,ctx,neighbours)
        erase_content(tx,scope.dataset,resources,events)
        for resource in LABELS:ctx['changed'][resource].update(resources[resource])
        tx.run('CREATE (p:KDMCPPurgeLedger) SET p=$p',p={'id':ctx['rid'],'dataset':scope.dataset,
            'plan_digest':manifest['plan_digest'],'resources':encoded(resources),'created_at':ctx['now']}).consume()
        ctx['removed_counts']={r:len(ids) for r,ids in resources.items()};ctx['applied']=len(targets)
