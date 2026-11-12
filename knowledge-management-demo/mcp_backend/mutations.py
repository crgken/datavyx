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
from .lifecycle import present,current_fact
from .selection import LABELS,select_targets


class Mutations:
    def __init__(self,repository):self.repo=repository

    def _execute(self,scope,data,operation,apply):
        rid=request_key(scope,data.request_id)
        fingerprint=digest([operation,data.model_dump(exclude={'workspace_id'})])
        def run(tx,revision):
            previous=tx.run('MATCH (r:KDMCPRequest {dataset:$d,id:$id}) RETURN properties(r) AS r',
                            d=scope.dataset,id=rid).single()
            if previous:
                old=previous['r']
                require(not old.get('purged'),'PREVIOUSLY_PURGED','This request was purged and cannot be replayed.',409)
                require(old.get('fingerprint')==fingerprint,'IDEMPOTENCY_CONFLICT','This request ID belongs to another body or operation.',409)
                return {**json.loads(old['receipt']),'idempotent':True}
            require(self.repo.settings.mode=='test' or self.repo.settings.recording_enabled
                    or scope.actor in self.repo.settings.recording_actors,'MAINTENANCE',
                    'New writes are temporarily disabled. Queries and receipts remain available.',503)
            objects,facts=self.repo.snapshot(tx,scope)
            ctx={'scope':scope,'data':data,'operation':operation,'rid':rid,'revision':revision,
                'objects':objects,'facts':{f['id']:f for f in facts},'versions':{i:o.get('version',1) for i,o in objects.items()},
                'source_ids':{s.ref:uuid4().hex for s in data.sources},'sources_created':False,
                'changed':{'entity':set(),'fact':set(),'source':set()},'touched':set(),
                'matched':0,'applied':0,'relations_hidden':0,'now':time.time()}
            apply(tx,ctx)
            changed=any(ctx['changed'].values())
            after=revision+int(changed)
            receipt={'schema_version':'2.0','request_id':data.request_id,'receipt_id':rid,'operation':operation,
                'status':'applied' if changed else 'noop','revision_before':revision,'revision':after,
                'matched_count':ctx['matched'],'applied_count':ctx['applied'],
                'objects_changed':len(ctx['changed']['entity']),'facts_changed':len(ctx['changed']['fact']),
                'sources_changed':len(ctx['changed']['source'])+int(ctx['sources_created'])*len(data.sources),
                'relations_hidden':ctx['relations_hidden'],'recoverable':operation=='delete',
                'selection_complete':True,'idempotent':False}
            tx.run('CREATE (r:KDMCPRequest) SET r=$r',r={'id':rid,'dataset':scope.dataset,
                'workspace_id':scope.workspace_id,'actor':scope.actor,'client_id':scope.client_id,
                'request_id':data.request_id,'fingerprint':fingerprint,'receipt':encoded(receipt),
                'operation':operation,'created_at':ctx['now'],
                'affected_ids':sorted(set().union(*ctx['changed'].values())),
                'source_ids':list(ctx['source_ids'].values()) if ctx['sources_created'] else []}).consume()
            if changed:
                tx.run('MATCH (d:KDData {id:$d}) SET d.revision=$revision',d=scope.dataset,revision=after).consume()
            return receipt
        return self.repo.scoped(scope,run,operation)

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
                    'cardinality':cardinality,'certainty':'asserted','active':True,'version':1,
                    'record_state':'active','recording_policy_version':2,'created_at':ctx['now'],
                    'recorded_at':ctx['now'],'recorded_by':scope.actor,'origin_channel':'mcp',
                    'supersedes':[f['id'] for f in selected]}
                candidate['value_key']=value_key(candidate)
                require(candidate['value_key'] not in {f['value_key'] for f,_ in replacements},
                        'INVALID_ARGUMENT','Replacement values must be distinct.')
                replacements.append((candidate,replacement.quotes))
            if ({(value_key(f),certainty(f)) for f in selected}=={(f['value_key'],'asserted') for f,_ in replacements}):continue
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
