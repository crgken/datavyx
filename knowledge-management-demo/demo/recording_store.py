"""Source-bound automatic recording and execution-time authorization."""
import json
import time

from .model import digest, encoded, require
from .model import DemoError
from uuid import uuid4


POLICY_VERSION = 2


class RecordingStorage:
    def migrate_recording_policy(self, apply=False):
        def migrate(tx):
            revision = self.lock(tx)
            row = tx.run('MATCH (d:KDData {id:$d}) RETURN d.recording_policy_version AS version', d=self.dataset).single()
            if row['version'] == POLICY_VERSION:
                return {'already_migrated': True, 'activated_objects': 0, 'activated_facts': 0, 'operation_drafts': 0}
            report = {'activated_objects': 0, 'activated_facts': 0, 'operation_drafts': 0, 'issues': []}
            touched = set()
            migrated = {}
            batches = [dict(r['b']) for r in tx.run('MATCH (b:KDBatch {dataset:$d}) RETURN properties(b) AS b ORDER BY b.created_at,b.id', d=self.dataset)]
            planned = []
            for batch in batches:
                items = [i for i in json.loads(batch['items']) if i['status'] == 'pending']
                if not items:
                    continue
                row = tx.run('MATCH (p:KDDraft {id:$id,dataset:$d}) RETURN properties(p) AS p', id=batch['id'],d=self.dataset).single()
                draft = dict(row['p']) if row else None
                payload = json.loads(draft['payload']) if draft else None
                safe = payload is not None and self.recording_decision(tx, payload)['outcome'] == 'saved'
                source_valid = True
                for item in items:
                    if item['type'] not in {'entity','fact'}:
                        safe = False
                        continue
                    try:
                        self.validate_review_source(tx, item['type'], item['target_id'])
                    except DemoError:
                        source_valid = False
                safe = safe and source_valid
                if not safe and (not draft or draft.get('runtime_version') != 2 or not draft.get('owner_actor')):
                    report['issues'].append({'code': 'missing_owned_operation_draft', 'batch_ref': digest(batch['id'])[:16]})
                    continue
                planned.append((batch, items, draft, safe, source_valid))
                if safe:
                    report['activated_objects'] += sum(i['type'] == 'entity' for i in items)
                    report['activated_facts'] += sum(i['type'] == 'fact' for i in items)
                else:
                    report['operation_drafts'] += 1
            if not apply:
                return report
            require(not report['issues'], 'migration_requires_resolution', '迁移发现缺少可归属来源的旧提议，请先处理迁移报告。', 409)
            for batch, items, draft, safe, source_valid in planned:
                original_items = json.loads(batch['items'])
                object_ids = [i['target_id'] for i in items if i['type'] == 'entity']
                fact_ids = [i['target_id'] for i in items if i['type'] == 'fact']
                migrated[batch['id']] = {'outcome':'saved' if safe else 'confirmation_required' if source_valid else 'clarification_required',
                    'saved_pending':False,'clear_draft':True,'policy_version':2,
                    'record_counts':{'objects_created':len(object_ids) if safe else 0,'facts_added':len(fact_ids) if safe else 0,'fields_updated':0,'records_retracted':0}}
                if safe:
                    tx.run("MATCH (o:KDObject {dataset:$d}) WHERE o.id IN $ids SET o.confirmed=true,o.review_status='confirmed',o.record_state='active',o.recording_policy_version=2,o.recorded_by=coalesce(o.recorded_by,$actor),o.recorded_at=coalesce(o.recorded_at,o.created_at)", d=self.dataset,ids=object_ids,actor=batch['actor']).consume()
                    tx.run("MATCH (f:KDFact {dataset:$d}) WHERE f.id IN $ids SET f.review_status='confirmed',f.record_state='active',f.recording_policy_version=2,f.recorded_by=coalesce(f.recorded_by,$actor),f.recorded_at=coalesce(f.recorded_at,f.created_at)",d=self.dataset,ids=fact_ids,actor=batch['actor']).consume()
                else:
                    conflicts = tx.run('MATCH (f:KDFact {dataset:$d,active:true}) WHERE (f.subject_id IN $objects OR f.object_id IN $objects) AND NOT f.id IN $facts RETURN count(f) AS n', d=self.dataset,objects=object_ids,facts=fact_ids).single()['n']
                    require(not conflicts, 'migration_dependency_conflict', '旧提议存在跨批次依赖，请先核对迁移范围。', 409)
                    tx.run("MATCH (o:KDObject {dataset:$d,confirmed:false}) WHERE o.id IN $ids SET o.record_state='action_draft',o.staged_draft_id=$draft",d=self.dataset,ids=object_ids,draft=draft['id']).consume()
                    tx.run("MATCH (f:KDFact {dataset:$d}) WHERE f.id IN $ids SET f.active=false,f.record_state='action_draft',f.staged_draft_id=$draft",d=self.dataset,ids=fact_ids,draft=draft['id']).consume()
                    tx.run("MATCH (p:KDDraft {id:$id,dataset:$d}) SET p.status='pending',p.expires_at=$expires,p.revision=$revision,p.recording_policy_version=2,p.recording_request_id='',p.migrated_from_batch=$id",id=draft['id'],d=self.dataset,expires=time.time()+1800,revision=revision+1).consume()
                    if source_valid and self.recording_decision(tx,json.loads(draft['payload']))['outcome']=='confirmation_required':
                        tx.run("CREATE (:KDApproval {id:$id,dataset:$d,session_id:$s,actor:$actor,draft_id:$draft,tool_call_id:$call,revision:$revision,expires_at:$expires,status:'pending'})",id=str(uuid4()),d=self.dataset,s=draft['owner_session'],actor=draft['owner_actor'],draft=draft['id'],call=str(uuid4()),revision=revision+1,expires=time.time()+1800).consume()
                for item in original_items:
                    if item['status'] == 'pending':
                        item.update(status='recorded' if safe else 'staged', policy_version=POLICY_VERSION)
                tx.run('MATCH (b:KDBatch {id:$id,dataset:$d}) SET b.items=$items,b.recording_policy_version=2',id=batch['id'],d=self.dataset,items=encoded(original_items)).consume()
                tx.run('MERGE (m:KDMutation {id:$id}) ON CREATE SET m.dataset=$d,m.created_at=$now,m.change=$change',id=digest(['recording_policy_migration',self.dataset,batch['id']]),d=self.dataset,now=time.time(),change=encoded({'action':'recording_policy_migration','policy_version':2,'batch_id':batch['id'],'entities':object_ids,'authorization_mode':'policy_migration','original_actor':batch['actor'],'outcome':'recorded' if safe else 'operation_draft'})).consume()
                touched.update(object_ids)
                touched.update(r['id'] for r in tx.run('MATCH (f:KDFact {dataset:$d}) WHERE f.id IN $ids RETURN DISTINCT f.subject_id AS id',d=self.dataset,ids=fact_ids))
            def normalized(value):
                batch_id=value.get('batch_id')
                if batch_id not in migrated:
                    return value
                result={**value,**migrated[batch_id]}
                en=not any('\u4e00'<=c<='\u9fff' for c in value.get('message',''))
                result['message']=('Recorded.' if en else '已记录。') if result['outcome']=='saved' else (
                    'This change has not been applied. Review the operation card.' if en else '本次变更尚未执行，请查看操作卡片。')
                return result
            for r in list(tx.run('MATCH (r:KDPendingRequest {dataset:$d}) RETURN r.id AS id,r.receipt AS value',d=self.dataset)):
                value=json.loads(r['value'])
                if value.get('batch_id') in migrated:
                    tx.run('MATCH (r:KDPendingRequest {id:$id,dataset:$d}) SET r.receipt=$value',id=r['id'],d=self.dataset,value=encoded(normalized(value))).consume()
            for r in list(tx.run("MATCH (j:KDMobileRequest {dataset:$d,status:'succeeded'}) RETURN j.id AS id,j.result AS value",d=self.dataset)):
                value=json.loads(r['value'] or '{}')
                if value.get('batch_id') in migrated:
                    tx.run('MATCH (j:KDMobileRequest {id:$id,dataset:$d}) SET j.result=$value',id=r['id'],d=self.dataset,value=encoded(normalized(value))).consume()
            for r in list(tx.run('MATCH (c:KDConversation {dataset:$d}) WHERE c.execution IS NOT NULL RETURN c.id AS id,c.execution AS value',d=self.dataset)):
                state=json.loads(r['value'])
                for name in ('last_response','last_pending_save'):
                    if (state.get(name) or {}).get('batch_id') in migrated:
                        state['last_save' if name=='last_pending_save' else name]=normalized(state[name])
                state.pop('last_pending_save',None)
                tx.run('MATCH (c:KDConversation {id:$id,dataset:$d}) SET c.execution=$value',id=r['id'],d=self.dataset,value=encoded(state)).consume()
            for r in list(tx.run('MATCH (j:KDImport {dataset:$d}) RETURN j.id AS id,j.payload AS value',d=self.dataset)):
                job=json.loads(r['value'])
                if job.get('batch_id') in migrated:
                    job['status']='saved' if migrated[job['batch_id']]['outcome']=='saved' else 'awaiting_confirmation'
                    job.pop('pending_item_count',None)
                    tx.run('MATCH (j:KDImport {id:$id,dataset:$d}) SET j.payload=$value',id=r['id'],d=self.dataset,value=encoded(job)).consume()
            tx.run("MATCH (a:KDApproval {dataset:$d}) WHERE coalesce(a.revision,-1)<$revision AND a.status IN ['pending','approved'] SET a.status='revoked'",d=self.dataset,revision=revision+1).consume()
            tx.run("MATCH (t:KDReviewToken {dataset:$d,status:'pending'}) SET t.status='revoked'",d=self.dataset).consume()
            self.refresh_effective_facts(tx)
            tx.run('MATCH (d:KDData {id:$d}) SET d.recording_policy_version=2,d.revision=$revision',d=self.dataset,revision=revision+1).consume()
            self.enqueue_indexes(tx,touched,touched)
            return report
        return self.write(migrate) if apply else self.read(migrate)

    def recording_decision(self, tx, payload):
        questions = []
        for obj in payload['objects']:
            if not obj['resolved'] or not obj['certain']:
                label = obj['name'] or obj['identifier']
                questions.append({'question': f'请明确“{label}”的身份或分类。', 'target_ref': obj['ref']})
        if questions:
            return {'outcome': 'clarification_required', 'clarification_required': True, 'questions': questions}
        reasons = []
        if payload.get('entity_updates'):
            reasons.append('change_existing_fields')
        if payload.get('retractions') or payload.get('reviews'):
            reasons.append('change_existing_records')
        for fact in payload['facts']:
            if fact.get('supersedes'):
                reasons.append('replace_existing_value')
        return {'outcome': 'confirmation_required' if reasons else 'saved',
                'confirmation_required': bool(reasons)}

    def save_recording(self, draft_id, scope, request_id='', approval_id=''):
        scope.check(self, 'record')
        if approval_id:
            scope.check(self, 'confirm')
        def write(tx):
            self.lock(tx)
            row = tx.run('MATCH (p:KDDraft {dataset:$d,id:$id}) RETURN properties(p) AS p',
                         d=self.dataset, id=draft_id).single()
            require(row, 'draft_not_found', '变更内容不存在。', 404)
            draft = dict(row['p'])
            require(draft.get('owner_actor') == scope.actor and draft.get('owner_session') == scope.session_id,
                    'scope_denied', '变更不属于当前用户或会话。', 403)
            payload = json.loads(draft['payload'])
            if draft.get('recording_receipt'):
                return {**json.loads(draft['recording_receipt']), 'idempotent': True}
            sources = payload.get('source_messages', [])
            actual = self._task_sources(tx, [s['id'] for s in sources], scope.session_id) if sources else []
            require(actual == sources, 'source_changed', '原始来源已变化，请重新处理。', 409)
            text = next((s['text'] for s in sources if s['id'] == payload['message_id']), '')
            rid = digest([self.dataset, scope.actor, scope.session_id, request_id]) if request_id else ''
            if rid:
                previous = tx.run('MATCH (r:KDPendingRequest {dataset:$d,id:$id}) RETURN r.receipt AS receipt,r.request_hash AS hash',
                                  d=self.dataset, id=rid).single()
                if previous:
                    require(previous['hash'] == digest(text.strip()), 'idempotency_conflict', '同一请求不能保存不同内容。', 409)
                    return {**json.loads(previous['receipt']), 'idempotent': True}
            require(draft['status'] in {'pending', 'committed'}, 'draft_closed', '变更已关闭，请重新发起。', 409)
            if draft['status'] == 'pending':
                require(draft['expires_at'] > time.time(), 'preview_expired', '变更已过期，请重新发起。', 409)
            decision = self.recording_decision(tx, payload)
            tx.run('MATCH (p:KDDraft {id:$id,dataset:$d}) SET p.recording_policy_version=$v,p.recording_request_id=$request',
                   id=draft_id, d=self.dataset, v=POLICY_VERSION, request=request_id).consume()
            if decision['outcome'] == 'clarification_required' or decision['confirmation_required'] and not approval_id:
                return decision
            for source in sources:
                existing = tx.run('MATCH (s:KDSource {dataset:$d,id:$id}) RETURN s.message AS text,coalesce(s.valid,true) AS valid',
                                  d=self.dataset, id=source['id']).single()
                require(not existing or existing['valid'] and existing['text'] == source['text'],
                        'source_invalid', '原始来源缺失或失效，不能执行。', 409)
            new_objects = [o['id'] for o in payload['objects'] if o['new']]
            fact_ids = [f['id'] for f in payload['facts']]
            existing_facts = {r['id'] for r in tx.run("MATCH (f:KDFact {dataset:$d}) WHERE f.id IN $ids AND coalesce(f.record_state,'active')<>'action_draft' RETURN f.id AS id",
                                                     d=self.dataset, ids=fact_ids)}
            result = self._commit(tx, draft_id, approval_id, scope, recording=True, automatic=not approval_id)
            now = time.time()
            for label, ids in (('KDObject', new_objects), ('KDFact', [i for i in fact_ids if i not in existing_facts])):
                tx.run(f'MATCH (n:{label} {{dataset:$d}}) WHERE n.id IN $ids SET n.record_state="active",'
                       'n.recorded_at=$now,n.recorded_by=$actor,n.recording_policy_version=$version',
                       d=self.dataset, ids=ids, now=now, actor=scope.actor, version=POLICY_VERSION).consume()
            warnings = [{'message': ('Estimated or uncertain values are recorded with their original meaning and excluded from precise statistics.'
                if scope.language == 'en' else '估计或不确定内容已保留原意，不计入精确统计。')}]
            if not any(not f['certain'] for f in payload['facts']):
                warnings = []
            if any(f.get('value') and f['value']['type'] == 'number' and not f['value'].get('unit') for f in payload['facts']):
                warnings.append({'message': 'The unit is unspecified; this value is excluded from totals requiring a unit.' if scope.language == 'en' else '数值未提供单位，不计入需要明确单位的合计。'})
            receipt = {**result, 'outcome': 'saved', 'saved_pending': False, 'clear_draft': True,
                       'change_id': draft_id, 'item_count': len(new_objects) + len(fact_ids) - len(existing_facts) + len(payload.get('entity_updates',[])) + len(payload.get('retractions',[])) + len(payload.get('reviews',[])),
                       'entity_refs': list(dict.fromkeys(o['id'] for o in [*payload['objects'], *payload.get('review_objects', [])])),
                       'record_counts': {'objects_created': len(new_objects), 'facts_added': len(fact_ids) - len(existing_facts),
                                         'fields_updated': len(payload.get('entity_updates', [])),
                                         'records_retracted': len(payload.get('retractions', []))},
                       'warnings': warnings,
                       'policy_version': POLICY_VERSION}
            if rid:
                tx.run('CREATE (:KDPendingRequest {id:$id,dataset:$d,receipt:$receipt,request_hash:$hash})',
                       id=rid, d=self.dataset, receipt=encoded(receipt), hash=digest(text.strip())).consume()
            tx.run('MATCH (p:KDDraft {id:$id,dataset:$d}) SET p.recording_receipt=$receipt',
                   id=draft_id,d=self.dataset,receipt=encoded(receipt)).consume()
            cid = digest([self.dataset, scope.session_id])
            row = tx.run('MATCH (c:KDConversation {id:$id,dataset:$d}) RETURN c.execution AS state', id=cid, d=self.dataset).single()
            if row and row['state']:
                state = json.loads(row['state'])
                state.update(pending=None, approval=None, clarification=None, last_save=receipt, last_response=receipt)
                tx.run('MATCH (c:KDConversation {id:$id,dataset:$d}) SET c.execution=$state',
                       id=cid, d=self.dataset, state=encoded(state)).consume()
            return receipt
        return self.write(write)
