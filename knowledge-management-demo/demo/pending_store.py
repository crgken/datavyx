"""Durable pending records, immutable review selections and transactional outbox.

Human review, semantic certainty and statistical eligibility are separate axes.
Drafts are parser checkpoints; batches are durable business records.
"""
import json
import time
from uuid import uuid4

from .model import digest, encoded, key, require


def new_id():
    return str(uuid4())


class PendingStorage:
    def pending_proposals(self,tx,oid=None):
        proposals=[]
        for row in tx.run('MATCH (b:KDBatch {dataset:$d}) RETURN b.id AS id,b.items AS items',d=self.dataset):
            for item in json.loads(row['items']):
                if item['status']!='pending' or item['type'] not in {'update','operation'}:
                    continue
                root=item['target_id']
                if item['type']=='operation' and item['data']['target_type']=='fact':
                    root=self.fact(tx,root)['subject_id']
                if oid is None or root==oid:
                    proposals.append({'batch_id':row['id'],'subject_id':root,**item})
        return proposals

    def search_records(self,raw):
        from .model import RecordQuery
        q=RecordQuery.model_validate(raw)
        def read(tx):
            revision=self.lock(tx,q.revision)
            condition=['o.dataset=$d','NOT coalesce(o.archived,false)']
            params={'d':self.dataset}
            proposals=self.pending_proposals(tx)
            params['pending_ids']=list({p['subject_id'] for p in proposals})
            if q.kind:
                condition.append('o.kind=$kind');params['kind']=q.kind
            if q.text:
                params['proposal_matches']=list({p['subject_id'] for p in proposals if p['type']=='update' and p['data']['field'] in {'name','identifier'} and key(q.text) in key(p['data']['value'])})
                condition.append('(toLower(o.name) CONTAINS $text OR toLower(o.identifier) CONTAINS $text OR o.id IN $proposal_matches)');params['text']=key(q.text)
            if q.object_ids:
                condition.append('o.id IN $ids');params['ids']=q.object_ids
            pending="(o.confirmed=false OR o.id IN $pending_ids OR EXISTS { MATCH (f:KDFact {dataset:$d,subject_id:o.id,active:true,review_status:'pending'}) })"
            if q.status!='all':
                condition.append(pending if q.status=='pending' else 'NOT '+pending)
            for n,f in enumerate(q.filters):
                params['p'+str(n)]=f.predicate
                field='object_id' if f.direction=='in' else 'subject_id'
                test=''
                if f.op!='exists':
                    params['v'+str(n)]=f.object_id if f.direction=='in' else digest({'object_id':f.object_id}) if f.object_id else f.value.fields()['value_key']
                    test=f" AND f.{'subject_id' if f.direction=='in' else 'value_key'} {'=' if f.op=='eq' else '<>'} $v{n}"
                condition.append(f"EXISTS {{ MATCH (f:KDFact {{dataset:$d,{field}:o.id,predicate:$p{n},active:true}}) WHERE true{test} }}")
            base='MATCH (o:KDObject) WHERE '+' AND '.join(condition)
            total=tx.run(base+' RETURN count(o) AS n',**params).single()['n']
            objects=[dict(r['o']) for r in tx.run(base+' RETURN properties(o) AS o ORDER BY o.id SKIP $offset LIMIT $limit',**params,offset=q.offset,limit=q.limit)]
            rows=[]
            for obj in objects:
                facts=[self.fact_view(tx,dict(r['f'])) for r in tx.run('MATCH (f:KDFact {dataset:$d,subject_id:$id,active:true}) RETURN properties(f) AS f ORDER BY f.id LIMIT 101',d=self.dataset,id=obj['id'])]
                rows.append({'object':self.entity_view(obj),'facts':facts[:100],'facts_truncated':len(facts)>100,'pending_proposals':[p for p in proposals if p['subject_id']==obj['id']]})
            return {**self.page_result(rows,total,q.offset,q.limit,revision),'record_query':q.model_dump(),'query_id':new_id(),'scope':'普通查询包含已确认及待确认记录；逐条标记。精准统计仅使用已确认且有效数据。'}
        return self.read(read)

    def initialize_pending_schema(self):
        with self.driver.session(database=self.config.database) as session:
            for label in ('KDBatch', 'KDReviewToken', 'KDIndexJob', 'KDIndexHead','KDPendingRequest'):
                session.run(f'CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE').consume()
        def migrate(tx):
            self.lock(tx)
            row = tx.run('MATCH (d:KDData {id:$d}) RETURN d.pending_schema_version AS v', d=self.dataset).single()
            if row['v'] == 1:
                return
            # Migrate only stored records. Never read/replay messages or drafts.
            entities = [dict(r['o']) for r in tx.run("MATCH (o:KDObject {dataset:$d,confirmed:false}) WHERE NOT coalesce(o.archived,false) RETURN properties(o) AS o", d=self.dataset)]
            facts = [dict(r['f']) for r in tx.run("MATCH (f:KDFact {dataset:$d,active:true,review_status:'pending'}) RETURN properties(f) AS f", d=self.dataset)]
            if entities or facts:
                items = [self.review_item('entity', o['id'], o) for o in entities]
                items += [self.review_item('fact', f['id'], f) for f in facts]
                self._create_batch(tx, 'migration:'+self.dataset, 'default', 'local-user', items, [], {'migration':True})
            tx.run('MATCH (d:KDData {id:$d}) SET d.pending_schema_version=1', d=self.dataset).consume()
            self.enqueue_indexes(tx)
        self.write(migrate)

    @staticmethod
    def review_item(kind, target_id, data):
        return {'id':new_id(), 'type':kind, 'target_id':target_id, 'status':'pending', 'data':data}

    def _create_batch(self, tx, bid, session, actor, items, sources, metadata):
        tx.run("CREATE (:KDBatch {id:$id,dataset:$d,session_id:$s,actor:$a,items:$items,sources:$sources,metadata:$meta,created_at:$now})",
               id=bid,d=self.dataset,s=session,a=actor,items=encoded(items),sources=sources,meta=encoded(metadata),now=time.time()).consume()

    def pending_receipt(self,scope,request_id,text=None):
        scope.check(self,'read')
        if not request_id:return None
        def read(tx):
            row=tx.run('MATCH (r:KDPendingRequest {dataset:$d,id:$id}) RETURN r.receipt AS receipt,r.request_hash AS request_hash',d=self.dataset,id=digest([self.dataset,scope.actor,scope.session_id,request_id])).single()
            require(not row or text is None or row['request_hash']==digest(text.strip()),'idempotency_conflict','同一请求标识不能用于不同的消息。',409)
            return json.loads(row['receipt']) if row else None
        return self.read(read)

    def save_pending(self, draft_id, scope, request_id=''):
        scope.check(self, 'record_pending')
        def write(tx):
            revision = self.lock(tx)
            rid=digest([self.dataset,scope.actor,scope.session_id,request_id]) if request_id else ''
            row = tx.run('MATCH (p:KDDraft {dataset:$d,id:$id}) RETURN properties(p) AS p', d=self.dataset,id=draft_id).single()
            require(row, 'draft_not_found', '待保存内容不存在。',404)
            draft = dict(row['p'])
            require(draft.get('owner_session') == scope.session_id and draft.get('owner_actor') == scope.actor,
                    'scope_denied','内容不属于当前操作人或会话。',403)
            payload = json.loads(draft['payload'])
            message=tx.run("MATCH (m:KDMessage {dataset:$d,id:$id,session_id:$s,role:'user'}) RETURN m.text AS text",d=self.dataset,id=payload['message_id'],s=scope.session_id).single()
            require(message,'source_invalid','本轮原始消息不存在。',409)
            request_hash=digest(message['text'])
            if rid:
                previous=tx.run('MATCH (r:KDPendingRequest {dataset:$d,id:$id}) RETURN r.receipt AS receipt,r.request_hash AS hash',d=self.dataset,id=rid).single()
                if previous:
                    require(previous['hash']==request_hash,'idempotency_conflict','同一请求标识不能保存不同消息。',409)
                    return {**json.loads(previous['receipt']),'idempotent':True}
            if draft['status'] == 'stored_pending':
                return {'batch_id':draft_id, 'revision':revision, 'idempotent':True}
            require(draft['status'] == 'pending' and draft['expires_at'] > time.time(), 'draft_closed','解析草稿已关闭或过期。',409)
            require(draft['revision'] == revision,'preview_stale','数据已变化，请重新核验本次内容。',409)
            require(all(o['resolved'] for o in payload['objects']), 'identity_unresolved','请先澄清对象身份。')
            bulk = payload.get('bulk', {})
            require(not bulk or (bulk['complete'] and bulk['processed_rows'] == bulk['row_count'] and not bulk['missing']), 'table_incomplete','表格覆盖不完整，尚未保存。')
            # Revalidate immutable sources at the transaction boundary.
            sources = payload.get('source_messages', [])
            actual = self._task_sources(tx,[s['id'] for s in sources],scope.session_id)
            require(actual == sources, 'source_changed','原始消息已变化。',409)
            self.verify_review_snapshots(tx, payload)
            objects = {o['ref']:o for o in payload['objects']}
            items, now = [], time.time()
            for source in sources:
                tx.run('MERGE (s:KDSource {id:$id}) ON CREATE SET s.dataset=$d,s.message=$text,s.created_at=$now',
                       id=source['id'],d=self.dataset,text=source['text'],now=now).consume()
            updates = {u['ref'] for u in payload.get('entity_updates', [])}
            for obj in objects.values():
                if obj['new']:
                    require(obj['ref'] not in updates,'invalid_entity_update','新对象不能同时更正已有字段。')
                    values = {k:obj[k] for k in ('id','name','kind','identifier','namespace','identity_key','reason')}
                    values.update(dataset=self.dataset,created_at=now,confirmed=False,review_status='pending',semantic_certain=obj['certain'],aliases=[key(obj['name'])],archived=False)
                    tx.run('CREATE (o:KDObject) SET o=$v',v=values).consume()
                    items.append(self.review_item('entity',obj['id'],values))
                else:
                    current = self.object(tx,obj['id'])
                    require(not current.get('archived',False),'object_archived','对象已归档。',409)
                tx.run('MATCH (o:KDObject {dataset:$d,id:$id}), (s:KDSource {dataset:$d}) WHERE s.id IN $sources MERGE (s)-[:KD_SUPPORTS]->(o)',
                       d=self.dataset,id=obj['id'],sources=[s['id'] for s in sources]).consume()
            for fact in payload['facts']:
                fields = self.fact_fields(fact,objects)
                sid = objects[fact['subject']]['id']
                row = tx.run('MATCH (f:KDFact {dataset:$d,subject_id:$sid,predicate:$p,active:true,value_key:$v}) RETURN properties(f) AS f ORDER BY f.created_at,f.id LIMIT 1',
                             d=self.dataset,sid=sid,p=fact['predicate'],v=fields['value_key']).single()
                existing = dict(row['f']) if row else None
                # A new precise assertion must not reuse an approximate value.
                if existing and fields['value_type']=='number' and fact['certain'] and not existing.get('numeric_precise',False):
                    existing = None
                fid = existing['id'] if existing else fact['id']
                if not existing:
                    if fact['mode']=='replace':
                        tx.run("MATCH (f:KDFact {dataset:$d,subject_id:$sid,predicate:$p,active:true,review_status:'pending'}) SET f.active=false,f.replaced_by=$new,f.ended_at=$now",d=self.dataset,sid=sid,p=fact['predicate'],new=fid,now=now).consume()
                    values = {**fields,'id':fid,'dataset':self.dataset,'subject_id':sid,'predicate':fact['predicate'],
                              'confirmed':False,'active':True,'review_status':'pending','numeric_precise':fact['certain'],
                              'semantic_certain':fact['certain'],'reason':fact['reason'],'cardinality':fact['cardinality'],
                              'created_at':now,'pending_supersedes':fact['supersedes'],'batch_id':draft_id}
                    tx.run('CREATE (f:KDFact) SET f=$v WITH f MATCH (o:KDObject {id:$sid,dataset:$d}) MERGE (o)-[:KD_HAS_FACT]->(f) WITH f OPTIONAL MATCH (t:KDObject {id:$target,dataset:$d}) FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | MERGE (f)-[:KD_TARGET]->(t))',
                           v=values,sid=sid,d=self.dataset,target=fields['object_id']).consume()
                    items.append(self.review_item('fact',fid,values))
                for quote in fact['sources']:
                    tx.run('MATCH (s:KDSource {dataset:$d,id:$source}), (f:KDFact {dataset:$d,id:$id}) MERGE (s)-[e:KD_SUPPORTS]->(f) SET e.quote=coalesce(e.quote,$q),e.quotes=CASE WHEN $q IN coalesce(e.quotes,[]) THEN e.quotes ELSE coalesce(e.quotes,[])+[$q] END',
                           d=self.dataset,source=quote['message_id'],id=fid,q=quote['quote']).consume()
            for update in payload.get('entity_updates',[]):
                oid = objects[update['ref']]['id']
                current = self.object(tx,oid)
                require(current.get(update['field'],'')==update['previous'],'entity_changed','待更正字段已变化。',409)
                items.append(self.review_item('update',oid,update))
            for retraction in payload.get('retractions',[]):
                items.append(self.review_item('operation',retraction['fact_id'],{'target_type':'fact','target_id':retraction['fact_id'],'action':'retract','reason':retraction['reason'],'sources':retraction['sources']}))
            for op in payload.get('reviews',[]):
                items.append(self.review_item('operation',op['target_id'],{k:v for k,v in op.items() if k in {'target_type','target_id','action','reason','sources','new_kind','classification_error'}}))
            for item in items:
                if item['type'] in {'update','operation'}:
                    item['base_fingerprint']=self._item_fingerprint(tx,item)
            self._create_batch(tx,draft_id,scope.session_id,scope.actor,items,[s['id'] for s in sources],{'bulk':bulk,'source':payload['source']})
            self.refresh_effective_facts(tx)
            tx.run("MATCH (p:KDDraft {dataset:$d,id:$id}), (d:KDData {id:$d}) SET p.status='stored_pending',p.stored_at=$now,d.revision=d.revision+1",
                   d=self.dataset,id=draft_id,now=now).consume()
            tx.run('CREATE (:KDMutation {id:$id,dataset:$d,created_at:$now,change:$c})',id=new_id(),d=self.dataset,now=now,
                   c=encoded({'action':'save_pending','actor':scope.actor,'batch_id':draft_id,'entities':[o['id'] for o in objects.values()]})).consume()
            if bulk:
                row = tx.run('MATCH (j:KDImport {dataset:$d,id:$id,session_id:$s,actor:$a}) RETURN j.payload AS p',d=self.dataset,id=bulk['import_id'],s=scope.session_id,a=scope.actor).single()
                require(row,'import_not_found','导入任务不存在。',409)
                job = json.loads(row['p'])
                job.update(status='saved_pending',draft_id=draft_id,batch_id=draft_id,pending_item_count=len(items),entity_refs={o['ref']:o['id'] for o in objects.values()})
                tx.run('MATCH (j:KDImport {id:$id}) SET j.payload=$p',id=bulk['import_id'],p=encoded(job)).consume()
            touched={o['id'] for o in objects.values()}
            touched.update(o['id'] for o in payload.get('review_objects',[]))
            self.enqueue_indexes(tx,touched)
            receipt={'batch_id':draft_id,'revision':revision+1,'idempotent':False,'item_count':len(items),'entity_refs':list(dict.fromkeys(o['id'] for o in [*objects.values(),*payload.get('review_objects',[])]))}
            if rid:
                tx.run('CREATE (:KDPendingRequest {id:$id,dataset:$d,receipt:$receipt,request_hash:$hash})',id=rid,d=self.dataset,receipt=encoded(receipt),hash=request_hash).consume()
            # Finish the input task atomically. A crash before the assistant's
            # receipt must not leave a completed bulk job blocking conversation.
            cid=digest([self.dataset,scope.session_id])
            row=tx.run('MATCH (c:KDConversation {id:$id,dataset:$d}) RETURN c.execution AS state',id=cid,d=self.dataset).single()
            if row and row['state']:
                state=json.loads(row['state'])
                state.update(pending=None,approval=None,clarification=None,focus_refs=receipt['entity_refs'][:8],last_pending_save=receipt)
                tx.run('MATCH (c:KDConversation {id:$id,dataset:$d}) SET c.execution=$state',id=cid,d=self.dataset,state=encoded(state)).consume()
            return receipt
        return self.write(write)

    def _batch(self, tx, bid, scope):
        row = tx.run('MATCH (b:KDBatch {dataset:$d,id:$id,session_id:$s,actor:$a}) RETURN properties(b) AS b',d=self.dataset,id=bid,s=scope.session_id,a=scope.actor).single()
        require(row,'batch_not_found','待确认批次不存在或不属于当前操作范围。',404)
        b = dict(row['b'])
        b['items'],b['metadata'] = json.loads(b['items']),json.loads(b['metadata'])
        fact_ids={i['target_id'] for i in b['items'] if i['type']=='fact' or i['type']=='operation' and i['data']['target_type']=='fact'}
        facts={r['f']['id']:dict(r['f']) for r in tx.run('MATCH (f:KDFact {dataset:$d}) WHERE f.id IN $ids RETURN properties(f) AS f',d=self.dataset,ids=list(fact_ids))}
        object_ids={i['target_id'] for i in b['items'] if i['target_id'] not in fact_ids}
        object_ids.update(oid for f in facts.values() for oid in (f['subject_id'],f.get('object_id')) if oid)
        objects={r['o']['id']:dict(r['o']) for r in tx.run('MATCH (o:KDObject {dataset:$d}) WHERE o.id IN $ids RETURN properties(o) AS o',d=self.dataset,ids=list(object_ids))}
        label=lambda oid:(objects.get(oid,{}).get('name') or objects.get(oid,{}).get('identifier') or '对象')
        # Other review entry points operate on the same canonical records.
        for item in b['items']:
            fid=item['target_id'] if item['target_id'] in fact_ids else ''
            current=facts.get(fid) if fid else objects.get(item['target_id'])
            require(current,'review_target_missing','原记录已删除，不能继续操作此批次。',409)
            item['target_label']=label(current['subject_id'] if fid else item['target_id'])
            if fid:
                item['subject_label']=label(current['subject_id'])
                item['object_label']=label(current['object_id']) if current.get('object_id') else ''
            if item['type'] in {'entity','fact'}:
                item['data']=current
            else:
                item['current_record']=current
            if item['status']!='pending' or item['type'] not in {'entity','fact'}:
                continue
            if item['type']=='entity':
                if current['confirmed']: item['status']='confirmed'
                elif current.get('archived',False): item['status']='withdrawn'
            elif not current['active']: item['status']='withdrawn' if current.get('retracted_by') else 'superseded'
            elif current['review_status']=='confirmed': item['status']='confirmed'
        return b

    def batches(self, scope, offset=0, limit=25, include_closed=False):
        scope.check(self,'read')
        self.page_bounds(offset,limit)
        def read(tx):
            version=self.lock(tx)
            rows=[]
            for r in tx.run('MATCH (b:KDBatch {dataset:$d,session_id:$s,actor:$a}) RETURN b.id AS id ORDER BY b.created_at DESC,b.id',d=self.dataset,s=scope.session_id,a=scope.actor):
                b=self._batch(tx,r['id'],scope)
                b['pending_count']=sum(i['status']=='pending' for i in b['items'])
                if b['pending_count'] or include_closed:
                    rows.append(b)
            return self.page_result(rows[offset:offset+limit],len(rows),offset,limit,version)
        return self.read(read)

    def _item_fingerprint(self, tx, item):
        kind,oid,data = item['type'],item['target_id'],item['data']
        if kind=='update':
            obj=self.object(tx,oid)
            return digest({k:obj.get(k) for k in ('id',data['field'],'archived')})
        if kind=='operation':
            obj=self.object(tx,oid) if data['target_type']=='entity' else self.fact(tx,oid)
        else:
            obj=self.object(tx,oid) if kind=='entity' else self.fact(tx,oid)
        if kind=='entity' or (kind=='operation' and data['target_type']=='entity'):
            return digest({k:obj.get(k) for k in ('id','kind','name','identifier','namespace','confirmed','archived')})
        slot=[dict(r['f']) for r in tx.run("MATCH (f:KDFact {dataset:$d,subject_id:$sid,predicate:$p,active:true,review_status:'confirmed'}) RETURN properties(f) AS f ORDER BY f.id",d=self.dataset,sid=obj['subject_id'],p=obj['predicate'])]
        return digest({'fact':obj,'slot':slot})

    def prepare_batch_review(self, scope, batch_id, item_ids, action):
        scope.check(self,'propose')
        require(action in {'confirm','withdraw'},'invalid_action','请选择确认或撤回。')
        require(isinstance(item_ids,list) and 0<len(item_ids)<=7000 and len(set(item_ids))==len(item_ids) and all(isinstance(i,str) for i in item_ids),'invalid_selection','请选择明确的记录范围。')
        def write(tx):
            self.lock(tx)
            batch=self._batch(tx,batch_id,scope)
            items=[i for i in batch['items'] if i['id'] in item_ids]
            require(len(items)==len(item_ids) and all(i['status']=='pending' for i in items),'review_changed','所选记录已处理，请刷新。',409)
            if action=='confirm':
                require(all(not i.get('base_fingerprint') or i['base_fingerprint']==self._item_fingerprint(tx,i) for i in items),'proposal_stale','更正提议的原记录已变化，请修改提议后重新核对。',409)
            snapshots={i['id']:self._item_fingerprint(tx,i) for i in items}
            token=new_id()
            tx.run("CREATE (:KDReviewToken {id:$id,dataset:$d,session_id:$s,actor:$a,batch_id:$b,items:$items,snapshots:$snapshots,action:$action,status:'pending',expires_at:$expires})",
                   id=token,d=self.dataset,s=scope.session_id,a=scope.actor,b=batch_id,items=item_ids,snapshots=encoded(snapshots),action=action,expires=time.time()+1800).consume()
            return {'token':token,'batch_id':batch_id,'action':action,'items':items,'expires_in':1800}
        return self.write(write)

    def apply_batch_review(self, scope, token):
        scope.check(self,'confirm')
        require(isinstance(token,str) and 0<len(token)<=100,'invalid_token','确认凭据无效。')
        def write(tx):
            revision=self.lock(tx)
            row=tx.run('MATCH (t:KDReviewToken {dataset:$d,id:$id,session_id:$s,actor:$a}) RETURN properties(t) AS t',d=self.dataset,id=token,s=scope.session_id,a=scope.actor).single()
            require(row,'scope_denied','确认凭据不属于当前操作范围。',403)
            t=dict(row['t'])
            if t['status']=='applied':
                return {'idempotent':True,'revision':revision,'message':'这批操作已完成，没有重复写入。'}
            require(t['status']=='pending' and t['expires_at']>time.time(),'review_expired','确认范围已过期，请重新核对。',409)
            batch=self._batch(tx,t['batch_id'],scope)
            items=[i for i in batch['items'] if i['id'] in t['items']]
            snapshots=json.loads(t['snapshots'])
            require(all(i['status']=='pending' and snapshots[i['id']]==self._item_fingerprint(tx,i) for i in items),'review_changed','所选记录或当前值已变化，请重新核对。',409)
            if t['action']=='confirm' and batch['sources']:
                messages=self._task_sources(tx,batch['sources'],scope.session_id)
                for message in messages:
                    source=tx.run('MATCH (s:KDSource {dataset:$d,id:$id}) RETURN properties(s) AS s',d=self.dataset,id=message['id']).single()
                    require(source and source['s']['message']==message['text'] and source['s'].get('valid',True),'source_invalid','原始来源已失效，不能确认。',409)
            for item in items:
                if t['action']=='confirm' and item['type'] in {'entity','fact'}:
                    supports=[dict(r) for r in tx.run('MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(n {dataset:$d,id:$id}) RETURN s.message AS text,coalesce(s.valid,true) AS valid,coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END) AS quotes',d=self.dataset,id=item['target_id'])]
                    require(supports and all(s['valid'] and all(q in s['text'] for q in s['quotes']) for s in supports),'source_invalid','该条信息的原始来源缺失或失效。',409)
            now=time.time()
            operations=[]
            for item in items:
                kind,oid,data=item['type'],item['target_id'],item['data']
                if t['action']=='withdraw':
                    if kind=='fact':
                        f=self.fact(tx,oid)
                        require(f['review_status']=='pending','review_changed','已确认记录请使用正式撤回流程。',409)
                        tx.run('MATCH (f:KDFact {dataset:$d,id:$id}) SET f.active=false,f.retracted_by=$token,f.ended_at=$now',d=self.dataset,id=oid,token=token,now=now).consume()
                    elif kind=='entity':
                        require(not self.object(tx,oid)['confirmed'],'review_changed','已确认对象请使用归档流程。',409)
                        # Never hide other still-current records as a side effect.
                        active=tx.run('MATCH (f:KDFact {dataset:$d,active:true}) WHERE f.subject_id=$id OR f.object_id=$id RETURN f.id AS id',d=self.dataset,id=oid)
                        withdrawing={i['target_id'] for i in items if i['type']=='fact'}
                        require(all(r['id'] in withdrawing for r in active),'entity_in_use','该对象仍有关联信息，请先选择一并撤回，或只撤回相关信息。',409)
                        tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) SET o.archived=true,o.withdrawn_at=$now',d=self.dataset,id=oid,now=now).consume()
                elif kind=='update':
                    obj=self.object(tx,oid)
                    require(obj.get(data['field'],'')==data['previous'],'entity_changed','字段已变化。',409)
                    values={data['field']:data['value']}
                    final={**obj,**values}
                    values['identity_key']=digest([key(final['kind']),key(final['namespace']),key(final['identifier'])]) if final['identifier'] else 'new:'+oid
                    require(not tx.run('MATCH (o:KDObject {dataset:$d,identity_key:$identity}) WHERE o.id<>$id RETURN o.id AS id LIMIT 1',d=self.dataset,identity=values['identity_key'],id=oid).single(),'duplicate_identity','更正后的身份与其他对象冲突。',409)
                    tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) SET o += $v',d=self.dataset,id=oid,v=values).consume()
                elif kind=='operation':
                    operations.append(data)
                else:
                    current=self.object(tx,oid) if kind=='entity' else self.fact(tx,oid)
                    if kind=='fact':
                        require(current['active'],'review_changed','信息已撤回或被更正。',409)
                        olds=current.get('pending_supersedes',[])
                        current_slot=[r['id'] for r in tx.run("MATCH (f:KDFact {dataset:$d,subject_id:$sid,predicate:$p,active:true,review_status:'confirmed'}) WHERE f.id<>$id RETURN f.id AS id",d=self.dataset,sid=current['subject_id'],p=current['predicate'],id=oid)]
                        require(current.get('cardinality','one')=='many' or set(current_slot)==set(olds),'slot_changed','当前单值已变化，请核验更正范围。',409)
                    if (kind=='entity' and not current['confirmed']) or (kind=='fact' and current['review_status']!='confirmed'):
                        operations.append({'target_type':kind,'target_id':oid,'action':'confirm','reason':'用户核实所选待确认记录','sources':[],'new_kind':'','classification_error':False})
                item.update(status='confirmed' if t['action']=='confirm' else 'withdrawn',reviewed_at=now,reviewed_by=scope.actor)
            reviews=[]
            if operations:
                reviews,impact,entities=self.prepare_review_operations(tx,operations)
                self.apply_reviews(tx,{'reviews':reviews},token,scope.actor)
            self.refresh_effective_facts(tx)
            durable_items=[{k:v for k,v in i.items() if k not in {'target_label','subject_label','object_label','current_record'}} for i in batch['items']]
            tx.run('MATCH (b:KDBatch {id:$id,dataset:$d}) SET b.items=$items',id=batch['id'],d=self.dataset,items=encoded(durable_items)).consume()
            tx.run("MATCH (t:KDReviewToken {id:$id}), (d:KDData {id:$d}) SET t.status='applied',t.applied_at=$now,d.revision=d.revision+1",id=token,d=self.dataset,now=now).consume()
            touched,dependencies=set(),set()
            for item in items:
                if item['type'] in {'entity','update'} or item['type']=='operation' and item['data']['target_type']=='entity':
                    touched.add(item['target_id']);dependencies.add(item['target_id'])
                else:
                    f=self.fact(tx,item['target_id']);touched.add(f['subject_id'])
            tx.run('CREATE (:KDMutation {id:$id,dataset:$d,created_at:$now,change:$c})',id=token,d=self.dataset,now=now,c=encoded({'action':t['action'],'actor':scope.actor,'batch_id':batch['id'],'items':t['items'],'entities':sorted(touched),'reviews':reviews,'entity_refs':{i['data']['ref']:i['target_id'] for i in items if i['type']=='update'},'entity_updates':[i['data'] for i in items if i['type']=='update' and t['action']=='confirm']})).consume()
            self.enqueue_indexes(tx,touched,dependencies)
            return {'idempotent':False,'revision':revision+1,'message':f"已{'确认' if t['action']=='confirm' else '撤回'}所选 {len(items)} 项信息。","clear_draft":True}
        return self.write(write)
