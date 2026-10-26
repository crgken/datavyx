"""Mobile operations behind Auth0 validation and local membership checks.

One runtime serves dataset-scoped workspaces over the same two Neo4j instances.
Canonical records, rather than semantic projections, supply browse counts.
HTTP bodies never supply scope.
"""
import asyncio
import json
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from neo4j import GraphDatabase

from .contracts import ExecutionScope
from .model import DemoError, digest, encoded, require
from .service import Service


PERMISSIONS = {'read', 'propose', 'record_pending', 'confirm'}
FIELDS = {
    'conversations': {'offset', 'limit'}, 'conversation_create': set(),
    'messages': {'offset', 'limit'}, 'conversation_objects': {'offset', 'limit', 'revision'},
    'neighbors': {'id', 'offset', 'limit', 'revision'},
    'detail': {'id', 'offset', 'limit', 'revision', 'history'},
    'sources': {'id', 'offset', 'limit'}, 'graph': {'revision'},
    'chat_submit': {'request_id', 'message', 'retry'}, 'request_status': {'request_id'},
    'resolve': {'draft_id', 'choices'}, 'draft': {'id'},
    'batches': {'offset', 'limit'}, 'batch': {'id'},
    'batch_review': {'batch_id', 'item_ids', 'action'}, 'batch_apply': {'token'},
    'query': {'query'}, 'records': {'query'}, 'audit': {'id', 'offset', 'limit'},
    'import': {'id'}, 'import_control': {'job_id', 'action'},
    'review_preview': {'review'}, 'review_impact': {'id', 'offset', 'limit'},
    'confirm': {'draft_id', 'approval_id', 'revision'},
    'cancel_preview': {'draft_id'}, 'health': set(), 'index_retry': set(),
}


@dataclass(frozen=True)
class MobileScope(ExecutionScope):
    workspace_id: str = ''

    def check(self, store, permission):
        super().check(store, permission)
        # A model task may outlive a role change. Recheck before guarded actions.
        from .mobile_auth import ROLES
        row = store.read(lambda tx: tx.run(
            'MATCH (m:KDWorkspaceMember {dataset:$d,actor:$a,workspace_id:$w,enabled:true}) '
            'RETURN m.role AS role', d=self.dataset, a=self.actor,
            w=self.workspace_id).single())
        require(row and permission in ROLES.get(row['role'], []),
                'permission_denied', '当前操作没有相应权限。', 403)


class MobileService(Service):
    """Share the stateless model client, never history, scope or approvals."""
    def __init__(self, gateway, scope):
        super().__init__(gateway.store, gateway.pipeline, scope=scope)
        self.gateway = gateway

    @property
    def runtime(self):
        if self.gateway.runtime is None:
            from .agent_runtime import AgentRuntime
            self.gateway.runtime = AgentRuntime(self.store, self.pipeline)
        return self.gateway.runtime


class MobileGateway:
    def __init__(self, store, pipeline, workspace_id=''):
        self.store, self.pipeline = store, pipeline
        self.workspace_id = workspace_id or store.dataset
        self.initialized = False
        self.running = set()
        self.conversation_locks = {}
        self.import_services = {}
        self.runtime = None
        self.tasks = set()
        self._workspace_gateways = {}
        self._workspace_gateways_lock = threading.Lock()

    def _gateway_for(self, context):
        workspace_id, dataset = context['tenant'], context['dataset']
        require(isinstance(workspace_id, str) and workspace_id and
                isinstance(dataset, str) and dataset,
                'permission_denied', '工作空间上下文无效。', 403)
        if self.workspace_id == workspace_id and self.store.dataset == dataset:
            return self
        with self._workspace_gateways_lock:
            gateway = self._workspace_gateways.get(workspace_id)
            if gateway is None:
                gateway = MobileGateway(self.store.for_dataset(dataset), self.pipeline, workspace_id)
                self._workspace_gateways[workspace_id] = gateway
            require(gateway.store.dataset == dataset,
                    'permission_denied', '工作空间数据绑定不匹配。', 403)
            return gateway

    def initialize(self):
        if self.initialized:
            return
        tenant = self.workspace_id
        binding = 'knowledge-mobile-instance-owner:' + tenant
        require(self.store.config.uri != self.store.config.pending_uri,
                'tenant_configuration_invalid', '已确认与待确认语义库必须隔离。', 503)

        def claim(tx, lane):
            row = tx.run('MERGE (b:KDMobileBinding {id:$id}) ON CREATE SET b.tenant=$tenant '
                         'SET b.lane=coalesce(b.lane,$lane) RETURN b.tenant AS tenant,b.lane AS lane',
                         id=binding, tenant=tenant, lane=lane).single()
            require(row['tenant'] == tenant and row['lane'] == lane, 'tenant_configuration_invalid',
                    '此数据库实例已绑定其他客户或语义通道，不能复用。', 503)

        workspace = self.store.read(lambda tx: tx.run(
            'MATCH (w:KDWorkspace {id:$id,dataset:$dataset,status:"active"}) RETURN w.id AS id',
            id=tenant, dataset=self.store.dataset).single())
        require(workspace, 'workspace_access_required', '工作空间不存在或已停用。', 403)

        self.store.write(lambda tx: claim(tx, 'approved'))
        with GraphDatabase.driver(self.store.config.pending_uri,
                auth=(self.store.config.user, self.store.config.password),
                connection_timeout=5) as pending:
            with pending.session(database=self.store.config.database) as session:
                session.execute_write(claim, 'pending')
        with self.store.driver.session(database=self.store.config.database) as session:
            for label in ('KDMobileConversation', 'KDMobileRequest'):
                session.run(f'CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS '
                            f'FOR (n:{label}) REQUIRE n.id IS UNIQUE').consume()
        self.store.write(lambda tx: tx.run(
            "MATCH (j:KDMobileRequest {dataset:$d,status:'running'}) "
            "SET j.status='interrupted',j.error=$error,j.updated_at=$now",
            d=self.store.dataset, error=encoded({'code': 'request_interrupted',
                'message': '处理被中断，请核对已保存内容后重试。'}), now=time.time()).consume())
        self.initialized = True

    def scope(self, context, payload):
        require(isinstance(context, dict) and set(context) == {'tenant', 'dataset', 'actor', 'permissions'},
                'permission_denied', '访问受限。', 403)
        require(context['tenant'] == self.workspace_id and context['dataset'] == self.store.dataset and
                isinstance(context['actor'], str) and 0 < len(context['actor']) <= 200,
                'permission_denied', '访问受限。', 403)
        permissions = context['permissions']
        require(isinstance(permissions, list) and all(p in PERMISSIONS for p in permissions),
                'permission_denied', '访问受限。', 403)
        language = payload.get('language', 'zh')
        require(language in {'zh', 'en'}, 'invalid_language', '语言参数无效。')
        conversation_id = payload.get('conversation_id', '')
        require(isinstance(conversation_id, str) and len(conversation_id) <= 100,
                'invalid_input', '会话参数无效。')
        scope = MobileScope(self.store.dataset, conversation_id, context['actor'],
                            frozenset(permissions), language, self.workspace_id)
        scope.check(self.store, 'read')
        return scope

    def own_conversation(self, scope):
        row = self.store.read(lambda tx: tx.run(
            'MATCH (c:KDMobileConversation {id:$id,dataset:$d,actor:$actor}) '
            'RETURN properties(c) AS c', id=scope.session_id,
            d=scope.dataset, actor=scope.actor).single())
        require(row, 'not_available', '内容不可用或当前账号无访问权限。', 404)
        return dict(row['c'])

    @staticmethod
    def bounds(payload):
        offset, limit = payload.get('offset', 0), payload.get('limit', 25)
        require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100,
                'invalid_input', '分页参数无效。')
        return offset, limit

    async def dispatch(self, raw):
        require(isinstance(raw, dict) and set(raw) == {'context', 'operation', 'payload'},
                'invalid_input', '请求格式无效。')
        context = raw['context']
        require(isinstance(context, dict) and set(context) == {'tenant', 'dataset', 'actor', 'permissions'},
                'permission_denied', '访问受限。', 403)
        gateway = self._gateway_for(context)
        if gateway is not self:
            return await gateway._dispatch(raw)
        return await self._dispatch(raw)

    async def _dispatch(self, raw):
        operation, payload = raw['operation'], raw['payload']
        require(isinstance(operation, str) and operation in FIELDS and isinstance(payload, dict),
                'invalid_input', '请求格式无效。')
        require(set(payload) <= FIELDS[operation] | {'language', 'conversation_id'},
                'invalid_input', '请求包含不允许的参数。')
        scope = self.scope(raw['context'], payload)
        if operation in {'resolve', 'review_preview', 'cancel_preview', 'import_control'}:
            scope.check(self.store, 'propose')
        if operation in {'confirm', 'batch_review', 'batch_apply', 'index_retry'}:
            scope.check(self.store, 'confirm')
        self.initialize()
        offset, limit = self.bounds(payload)
        if operation == 'conversation_create':
            conversation = {'id': str(uuid4()), 'dataset': scope.dataset,
                'actor': scope.actor, 'title': '', 'refs': [], 'created_at': time.time()}
            self.store.write(lambda tx: tx.run('CREATE (c:KDMobileConversation) SET c=$c',
                                               c=conversation).consume())
            return conversation
        if operation == 'conversations':
            def listing(tx):
                total = tx.run('MATCH (c:KDMobileConversation {dataset:$d,actor:$a}) RETURN count(c) AS n',
                    d=scope.dataset, a=scope.actor).single()['n']
                rows = [dict(r['c']) for r in tx.run('MATCH (c:KDMobileConversation {dataset:$d,actor:$a}) '
                    'RETURN properties(c) AS c ORDER BY c.created_at DESC,c.id SKIP $offset LIMIT $limit',
                    d=scope.dataset, a=scope.actor, offset=offset, limit=limit)]
                return self.store.page_result(rows, total, offset, limit, None)
            return self.store.read(listing)
        self.own_conversation(scope)
        service = MobileService(self, scope)
        service.chat_lock = self.conversation_locks.setdefault(scope.session_id, asyncio.Lock())
        if operation == 'chat_submit':
            return self.submit(service, payload)
        if operation == 'request_status':
            return self.request_status(scope, payload.get('request_id'))
        if operation == 'messages':
            def messages(tx):
                total = tx.run('MATCH (m:KDMessage {dataset:$d,session_id:$s}) RETURN count(m) AS n',
                    d=scope.dataset, s=scope.session_id).single()['n']
                rows = [dict(r['m']) for r in tx.run('MATCH (m:KDMessage {dataset:$d,session_id:$s}) '
                    'RETURN properties(m) AS m ORDER BY m.created_at DESC,m.id SKIP $offset LIMIT $limit',
                    d=scope.dataset, s=scope.session_id, offset=offset, limit=limit)]
                return self.store.page_result(rows[::-1], total, offset, limit, None)
            result = self.store.read(messages)
            return {**result, 'response': self.store.conversation(scope).get('last_response')}
        if operation in {'conversation_objects', 'neighbors'}:
            return self.browse(scope, payload, neighbors=operation == 'neighbors')
        if operation == 'detail':
            oid = self.available_object(payload.get('id'))
            return self.store.detail(oid, offset, limit, payload.get('revision'), payload.get('history', False))
        if operation == 'sources':
            return self.store.sources(payload.get('id'), offset, limit)
        if operation == 'graph':
            return self.store.graph(payload.get('revision'))
        if operation == 'draft': return service.public_draft(payload.get('id'))
        if operation == 'batches': return self.store.batches(scope, offset, limit)
        if operation == 'batch': return self.store.read(lambda tx: self.store._batch(tx, payload.get('id'), scope))
        if operation == 'batch_review':
            scope.check(self.store, 'confirm')
            return self.store.prepare_batch_review(scope, payload.get('batch_id'), payload.get('item_ids'), payload.get('action'))
        if operation == 'batch_apply':
            scope.check(self.store, 'confirm')
            return self.store.apply_batch_review(scope, payload.get('token'))
        if operation == 'query': return self.store.query(payload.get('query'))
        if operation == 'records': return self.store.search_records(payload.get('query'))
        if operation == 'audit': return self.store.audit(payload.get('id'), offset, limit)
        if operation == 'import':
            owner = self.import_services.get(scope.session_id, service)
            return owner.bulk.get(payload.get('id'))
        if operation == 'import_control':
            owner = self.import_services.get(scope.session_id, service)
            result = await owner.bulk.control(payload.get('job_id'), payload.get('action'))
            self.track_import(owner, payload.get('job_id'))
            return result
        if operation == 'review_impact': return service.review_impact(payload.get('id'), offset, limit)
        if operation == 'health': return self.store.health()
        if operation == 'index_retry':
            scope.check(self.store, 'confirm')
            return self.store.retry_indexes(scope)
        if operation == 'resolve': return await service.resolve(payload.get('draft_id'), payload.get('choices'))
        if operation == 'review_preview': return await service.prepare_review(payload.get('review'))
        if operation == 'confirm':
            return await service.confirm(payload.get('draft_id'), payload.get('approval_id'), payload.get('revision'))
        if operation == 'cancel_preview': return await service.cancel_preview(payload.get('draft_id'))
        raise DemoError('invalid_input', '操作无效。')

    def available_object(self, oid):
        require(isinstance(oid, str) and 0 < len(oid) <= 100, 'not_available', '内容不可用。', 404)
        obj = self.store.read(lambda tx: self.store.object(tx, oid))
        require(not obj.get('archived'), 'not_available', '内容不可用。', 404)
        return oid

    def browse(self, scope, payload, neighbors=False):
        offset, limit = self.bounds(payload)
        def read(tx):
            revision = self.store.lock(tx, payload.get('revision'))
            objects = {r['o']['id']: self.store.entity_view(dict(r['o'])) for r in tx.run(
                'MATCH (o:KDObject {dataset:$d}) WHERE NOT coalesce(o.archived,false) '
                'RETURN properties(o) AS o', d=scope.dataset)}
            relations = [dict(r['f']) for r in tx.run('MATCH (f:KDFact {dataset:$d,active:true}) '
                'WHERE f.object_id<>\'\' AND f.object_id<>f.subject_id RETURN properties(f) AS f', d=scope.dataset)]
            relations = [f for f in relations if f['subject_id'] in objects and f['object_id'] in objects]
            roots = set()
            for row in tx.run('MATCH (b:KDBatch {dataset:$d,session_id:$s,actor:$a}) RETURN b.items AS items',
                d=scope.dataset, s=scope.session_id, a=scope.actor):
                for item in json.loads(row['items']):
                    data = item['data']
                    if item['type'] == 'entity': roots.add(item['target_id'])
                    if item['type'] == 'fact': roots.update([data.get('subject_id'), data.get('object_id')])
                    if item['type'] == 'update': roots.add(item['target_id'])
            conversation = tx.run('MATCH (c:KDMobileConversation {id:$id,dataset:$d,actor:$a}) '
                'RETURN c.refs AS refs', id=scope.session_id, d=scope.dataset, a=scope.actor).single()
            require(conversation, 'not_available', '内容不可用或当前账号无访问权限。', 404)
            roots.update(conversation['refs'] or [])
            if not neighbors:
                # Query rows are paginated, but KDQuery retains the complete
                # participant snapshot. Only follow IDs in this user's own
                # completed conversation requests, including older requests.
                query_ids = set()
                for row in tx.run('MATCH (j:KDMobileRequest {dataset:$d,actor:$a,session_id:$s,status:\'succeeded\'}) '
                    'RETURN j.result AS result', d=scope.dataset, a=scope.actor, s=scope.session_id):
                    result = json.loads(row['result'])
                    query_id = (result.get('result') or {}).get('query_id')
                    if query_id:
                        query_ids.add(query_id)
                if query_ids:
                    for row in tx.run('MATCH (q:KDQuery {dataset:$d}) WHERE q.id IN $ids '
                        'RETURN q.participants AS participants', d=scope.dataset, ids=list(query_ids)):
                        roots.update(row['participants'] or [])
            current = payload.get('id') if neighbors else None
            if neighbors:
                require(current in objects, 'not_available', '内容不可用或当前账号无访问权限。', 404)
                selected = {f['object_id'] if f['subject_id'] == current else f['subject_id']
                            for f in relations if current in (f['subject_id'], f['object_id'])}
            else:
                selected = roots & objects.keys()
            selected = sorted(selected)
            rows = []
            for oid in selected[offset:offset+limit]:
                touching = [f for f in relations if oid in (f['subject_id'], f['object_id'])]
                unique = {f['object_id'] if f['subject_id'] == oid else f['subject_id'] for f in touching}
                obj = {**objects[oid], 'related_object_count': len(unique), 'relation_count': len(touching),
                       'has_pending': not objects[oid]['confirmed'] or any(f.get('review_status') != 'confirmed' for f in touching)}
                relevant = [f for f in touching if current in (f['subject_id'], f['object_id'])] if neighbors else []
                rows.append({'object': obj, 'relations': [{'id': f['id'], 'predicate': f['predicate'],
                    'direction': 'out' if f['subject_id'] == current else 'in',
                    'source': objects[f['subject_id']], 'target': objects[f['object_id']],
                    'review_status': f.get('review_status', 'pending')} for f in relevant]})
            return {**self.store.page_result(rows, len(selected), offset, limit, revision),
                    'object': objects.get(current), 'count_scope': 'visible_active_including_pending'}
        return self.store.read(read)

    def request_status(self, scope, request_id):
        require(isinstance(request_id, str) and 0 < len(request_id) <= 100, 'invalid_input', '请求标识无效。')
        row = self.store.read(lambda tx: tx.run('MATCH (j:KDMobileRequest {id:$id,dataset:$d,actor:$a,session_id:$s}) '
            'RETURN properties(j) AS j', id=digest([scope.dataset, scope.actor, scope.session_id, request_id]),
            d=scope.dataset, a=scope.actor, s=scope.session_id).single())
        if not row: return {'status': 'not_found'}
        job = dict(row['j'])
        return {'status': job['status'], 'result': json.loads(job.get('result', 'null')),
                'error': json.loads(job.get('error', 'null'))}

    def submit(self, service, payload):
        scope = service.scope
        scope.check(self.store, 'read')
        request_id, text = payload.get('request_id'), payload.get('message')
        require(isinstance(request_id, str) and 0 < len(request_id) <= 100 and
                isinstance(text, str) and 0 < len(text.strip()) <= 16000, 'invalid_input', '消息格式无效。')
        require(type(payload.get('retry', False)) is bool, 'invalid_input', '重试参数无效。')
        jid = digest([scope.dataset, scope.actor, scope.session_id, request_id])
        def reserve(tx):
            row = tx.run('MERGE (j:KDMobileRequest {id:$id}) ON CREATE SET j.dataset=$d,j.actor=$a,'
                'j.session_id=$s,j.input_hash=$hash,j.status=\'running\',j.created_at=$now '
                'RETURN j.input_hash AS hash,j.created_at AS created', id=jid, d=scope.dataset,
                a=scope.actor, s=scope.session_id, hash=digest(text.strip()), now=time.time()).single()
            require(row['hash'] == digest(text.strip()), 'idempotency_conflict', '同一请求不能提交不同内容。', 409)
            if payload.get('retry'):
                tx.run("MATCH (j:KDMobileRequest {id:$id}) WHERE j.status IN ['failed','interrupted'] "
                       "SET j.status='running',j.error='null',j.updated_at=$now",
                       id=jid, now=time.time()).consume()
        self.store.write(reserve)
        if jid not in self.running:
            status = self.request_status(scope, request_id)
            if status['status'] == 'running':
                self.running.add(jid)
                task = asyncio.create_task(self.process(service, jid, request_id, text))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        return {'request_id': request_id, **self.request_status(scope, request_id)}

    async def process(self, service, jid, request_id, text):
        try:
            result = await service.chat(text, request_id)
            if result.get('import_job'):
                self.track_import(service, result['import_job']['id'])
            refs = list(result.get('entity_refs', []))
            if result.get('detail'): refs.append(result['detail']['object']['id'])
            for row in (result.get('result') or {}).get('rows', []):
                if isinstance(row, dict):
                    oid = (row.get('object') or row).get('id')
                    if oid: refs.append(oid)
            def complete(tx):
                tx.run('MATCH (j:KDMobileRequest {id:$id}) SET j.status=\'succeeded\',j.result=$result,'
                       'j.updated_at=$now', id=jid, result=encoded(result), now=time.time()).consume()
                tx.run('MATCH (c:KDMobileConversation {id:$id}) SET c.title=CASE WHEN c.title=\'\' '
                       'THEN $title ELSE c.title END,c.refs=reduce(ids=coalesce(c.refs,[]),x IN $refs | '
                       'CASE WHEN x IN ids THEN ids ELSE ids+x END)', id=service.scope.session_id,
                       title=text[:100], refs=refs).consume()
            self.store.write(complete)
        except Exception as exc:
            receipt = self.store.pending_receipt(service.scope, request_id, text)
            if receipt:
                result = {**receipt, 'saved_pending': True, 'clear_draft': True,
                    'message': ('Saved as unconfirmed. Check the pending records for details.'
                        if service.scope.language == 'en' else '已保存为待确认，请在待确认记录中核对。')}
                self.store.write(lambda tx: tx.run("MATCH (j:KDMobileRequest {id:$id}) "
                    "SET j.status='succeeded',j.result=$result,j.updated_at=$now",
                    id=jid, result=encoded(result), now=time.time()).consume())
                return
            error = {'code': exc.code, 'message': str(exc)} if isinstance(exc, DemoError) else {
                'code': 'service_unavailable', 'message': '服务执行失败，原始输入已保留。'}
            self.store.write(lambda tx: tx.run('MATCH (j:KDMobileRequest {id:$id}) '
                'SET j.status=\'failed\',j.error=$error,j.updated_at=$now',
                id=jid, error=encoded(error), now=time.time()).consume())
        finally:
            self.running.discard(jid)

    def track_import(self, service, job_id):
        task = service.bulk.tasks.get(job_id)
        if task is None or self.import_services.get(service.scope.session_id) is service:
            return
        self.import_services[service.scope.session_id] = service

        async def cleanup():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            finally:
                if self.import_services.get(service.scope.session_id) is service:
                    self.import_services.pop(service.scope.session_id, None)

        asyncio.create_task(cleanup())

    async def close(self):
        for gateway in self._workspace_gateways.values():
            await gateway.close()
        tasks = list(self.tasks)
        tasks.extend(task for service in self.import_services.values() for task in service.bulk.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.runtime is not None:
            await self.runtime.close()
