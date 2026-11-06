"""Application-owned persistence and hard transaction/approval boundaries."""
import json
import time
from uuid import uuid4

from .model import digest, encoded, key, require


class ExecutionStorage:
    def _operation_targets(self, tx, selection, revision):
        require(selection.get('scope') in {'workspace', 'filtered'} and selection.get('action') in {'archive', 'restore'},
                'unsupported_operation_scope', '此批量操作的范围或动作尚不支持，请明确目标范围。', 409)
        kind, namespace = selection.get('kind', ''), selection.get('namespace', '')
        require(selection['scope'] != 'workspace' or not kind and not namespace,
                'invalid_operation_scope', '整个空间的操作不能隐含缩小目标范围。', 409)
        require(selection['scope'] != 'filtered' or bool(kind or namespace),
                'invalid_operation_scope', '筛选范围缺少明确条件，不能按整个空间执行。', 409)
        if kind:
            kinds = [r['kind'] for r in tx.run("MATCH (o:KDObject {dataset:$d}) WHERE toLower(o.kind)=toLower($kind) "
                "AND coalesce(o.record_state,'active')<>'action_draft' RETURN DISTINCT o.kind AS kind",d=self.dataset,kind=kind)]
            require(len(kinds)==1, 'invalid_operation_scope', '对象类型无法唯一对应当前空间，请明确类型后重新生成完整预览。', 409)
            kind = kinds[0]
            selection = {**selection,'kind':kind}
        rows = tx.run("MATCH (o:KDObject {dataset:$d}) WHERE coalesce(o.record_state,'active')<>'action_draft' "
            "AND coalesce(o.archived,false)=$archived AND ($kind='' OR o.kind=$kind) "
            "AND ($namespace='' OR o.namespace=$namespace) RETURN o.id AS id ORDER BY id LIMIT 501",
            d=self.dataset, archived=selection['action']=='restore', kind=kind, namespace=namespace)
        ids = [r['id'] for r in rows]
        require(len(ids) <= 500, 'operation_scope_too_large',
                '本次范围超过 500 条，尚未生成或执行任何部分操作；请缩小范围。', 409)
        return {'dataset':self.dataset, 'selection':selection, 'revision':revision,
                'target_ids':ids, 'target_count':len(ids), 'fingerprint':digest(ids)}

    def operation_targets(self, scope, selection, revision):
        scope.check(self, 'propose')
        def read(tx):
            return self._operation_targets(tx, selection, self.lock(tx, revision))
        return self.read(read)

    def verify_scope_coverage(self, tx, payload, coverage):
        require(coverage.get('dataset') == self.dataset, 'scope_denied', '操作清单不属于当前空间。', 403)
        revision = self.lock(tx, coverage['revision'])
        current = self._operation_targets(tx, coverage['selection'], revision)
        require(current == coverage, 'operation_scope_changed', '操作范围已变化，请重新生成完整预览。', 409)
        operations = payload.get('reviews', [])
        actual = [o['target_id'] for o in operations if o['target_type']=='entity' and o['action']==coverage['selection']['action']]
        require(len(actual)==len(operations)==coverage['target_count'] and sorted(actual)==coverage['target_ids']
                and not any(payload.get(k) for k in ('objects','facts','entity_updates','retractions')),
                'operation_scope_incomplete', '操作清单没有完整覆盖请求范围，或包含额外变更；禁止提交部分结果。', 409)

    def initialize_execution_schema(self):
        with self.driver.session(database=self.config.database) as session:
            for label in ("KDApproval", "KDMutation", "KDImport"):
                session.run(f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE").consume()
        self.write(lambda tx: tx.run("MATCH (d:KDData {id:$d}) SET d.schema_version=coalesce(d.schema_version,2)", d=self.dataset).consume())

    @staticmethod
    def validate_quotes(quotes, source_text):
        require(quotes and all(q["message_id"] in source_text and q["quote"] in source_text[q["message_id"]] for q in quotes),
                "invalid_evidence", "引用来源不属于本次允许的原始用户消息，或摘录不匹配。")

    def find_entities(self, lookup):
        def read(tx):
            params = {"d": self.dataset, "text": key(lookup.text), "identifier": key(lookup.identifier),
                      "namespace": lookup.namespace, "kind": lookup.kind, "limit": lookup.limit}
            where = ("o.dataset=$d AND coalesce(o.record_state,'active')<>'action_draft' AND ($text='' OR toLower(o.name) CONTAINS $text OR toLower(o.identifier) CONTAINS $text OR $text IN coalesce(o.aliases,[])) "
                     "AND ($identifier='' OR toLower(o.identifier)=$identifier) AND ($namespace='' OR o.namespace=$namespace) AND ($kind='' OR o.kind=$kind)")
            total = tx.run("MATCH (o:KDObject) WHERE " + where + " RETURN count(o) AS n", **params).single()["n"]
            rows = [dict(r["o"]) for r in tx.run("MATCH (o:KDObject) WHERE " + where + " RETURN properties(o) AS o ORDER BY o.id LIMIT $limit", **params)]
            return {"rows": rows, "total": total, "truncated": total > len(rows), "revision": self.lock(tx)}
        return self.read(read)

    def operation_context(self, scope):
        """Current owned operation states, independent of model message history."""
        scope.check(self, 'read')
        def read(tx):
            revision = self.lock(tx)
            now = time.time()
            rows = tx.run("MATCH (p:KDDraft {dataset:$d,owner_session:$s,owner_actor:$a,runtime_version:2}) "
                "OPTIONAL MATCH (a:KDApproval {dataset:$d,draft_id:p.id,session_id:$s,actor:$a}) "
                "WHERE a.status IN ['pending','approved'] AND a.expires_at>$now AND a.revision=p.revision "
                "RETURN p.id AS draft_id,p.status AS status,p.revision AS revision,p.expires_at AS expires_at,p.created_at AS created_at,count(a)>0 AS ready_for_review "
                "ORDER BY created_at DESC,draft_id", d=self.dataset,s=scope.session_id,a=scope.actor,now=now)
            active, inactive = [], []
            for row in rows:
                item = dict(row)
                if item['status'] == 'pending':
                    if item['expires_at'] <= now:
                        item['status'] = 'expired'
                    elif item['revision'] != revision:
                        item['status'] = 'stale'
                if item['status'] == 'pending':
                    active.append(item)
                elif len(inactive) < 8:
                    item['ready_for_review'] = False
                    inactive.append(item)
            return {'revision':revision,'active':active[:25],'active_total':len(active),
                    'active_truncated':len(active)>25,'inactive':inactive}
        return self.read(read)

    def conversation(self, scope):
        scope.check(self, "read")
        def read(tx):
            row = tx.run("MATCH (c:KDConversation {id:$id,dataset:$d}) RETURN c.execution AS state",
                         id=digest([self.dataset, scope.session_id]), d=self.dataset).single()
            if row and row["state"]:
                return json.loads(row["state"])
            # Migrate an actual pending legacy draft/question, never reopen a
            # committed or cancelled draft. Focus refs survive completion.
            task = self.task(scope.session_id)
            if not task:
                latest = tx.run("MATCH (m:KDMessage {dataset:$d,session_id:$s}) RETURN m.id AS id,m.role AS role ORDER BY m.created_at DESC,m.id LIMIT 1",
                                d=self.dataset, s=scope.session_id).single()
                if latest and latest["role"] == "user":
                    completed = tx.run("MATCH (p:KDDraft {dataset:$d,status:'committed'}) WHERE p.payload CONTAINS $id RETURN p.id AS id LIMIT 1",
                                       d=self.dataset, id=latest["id"]).single()
                    if not completed:
                        task = {"status": "interrupted", "draft_id": "", "source_message_ids": [latest["id"]]}
            focus = tx.run("MATCH (p:KDDraft {dataset:$d,status:'committed'}),(m:KDMessage {session_id:$s,dataset:$d,role:'user'}) WHERE p.payload CONTAINS m.id RETURN p.payload AS payload ORDER BY p.committed_at DESC LIMIT 1", d=self.dataset, s=scope.session_id).single()
            refs = [o["id"] for o in json.loads(focus["payload"])["objects"]] if focus else []
            return {"version": 0, "history": [], "focus_refs": refs, "pending": task,
                    "approval": None, "last_response": None, "request_id": "", "schema_version": 2}
        return self.read(read)

    def save_conversation(self, scope, state, expected_version):
        scope.check(self, "read")
        def write(tx):
            row = tx.run("MERGE (c:KDConversation {id:$id}) SET c.dataset=$d,c.session_id=$s,c.lock=true RETURN c.execution AS state",
                         id=digest([self.dataset, scope.session_id]), d=self.dataset, s=scope.session_id).single()
            previous = json.loads(row["state"]) if row["state"] else {"version": 0}
            require(previous["version"] == expected_version, "conversation_changed", "会话已被另一个请求更新，请刷新后重试。", 409)
            updated = {**previous, **state, "version": expected_version + 1}
            for name in ('last_save','last_commit'):
                if previous.get(name):
                    updated[name] = previous[name]
            tx.run("MATCH (c:KDConversation {id:$id}) SET c.execution=$state,c.updated_at=$now",
                   id=digest([self.dataset, scope.session_id]), state=encoded(updated), now=time.time()).consume()
            return updated
        return self.write(write)

    def import_job(self, scope, job_id, missing_ok=False):
        scope.check(self, "read")
        def read(tx):
            row = tx.run("MATCH (j:KDImport {id:$id,dataset:$d,session_id:$s,actor:$actor}) RETURN j.payload AS payload",
                         id=job_id, d=self.dataset, s=scope.session_id, actor=scope.actor).single()
            require(row or missing_ok, "import_not_found", "导入任务不存在或不属于当前范围。", 404)
            return json.loads(row["payload"]) if row else None
        return self.read(read)

    def save_import_job(self, scope, job):
        scope.check(self, "propose")
        def write(tx):
            row = tx.run("MERGE (j:KDImport {id:$id}) ON CREATE SET j.dataset=$d,j.session_id=$s,j.actor=$actor "
                         "RETURN j.dataset=$d AND j.session_id=$s AND j.actor=$actor AS owned",
                         id=job["id"], d=self.dataset, s=scope.session_id, actor=scope.actor).single()
            require(row["owned"], "scope_denied", "导入任务不属于当前执行者。", 403)
            tx.run("MATCH (j:KDImport {id:$id}) SET j.payload=$payload,j.updated_at=$now", id=job["id"], payload=encoded(job), now=time.time()).consume()
        self.write(write)

    def prepare_execution_change(self, change, message_id, source_ids, scope, revision, bulk=None, coverage=None):
        scope.check(self, "propose")
        def write(tx):
            if coverage is not None:
                self.verify_scope_coverage(tx, change, coverage)
            payload = self._prepare(tx, change, message_id, source_ids, revision)
            if coverage is not None:
                payload['scope_coverage'] = coverage
                tx.run('MATCH (p:KDDraft {id:$id,dataset:$d}) SET p.scope_fingerprint=$fingerprint',
                       id=payload['draft_id'],d=self.dataset,fingerprint=digest(coverage)).consume()
            if bulk is not None:
                require(bulk["complete"] and bulk["processed_rows"] == bulk["row_count"] and not bulk["missing"],
                        "table_incomplete", "整表尚未完整处理，禁止创建可确认预览。")
                payload.update(origin="bulk", bulk=bulk)
            if bulk is not None or coverage is not None:
                tx.run("MATCH (p:KDDraft {id:$id,dataset:$d}) SET p.payload=$payload", id=payload["draft_id"], d=self.dataset,
                       payload=encoded({k:v for k,v in payload.items() if k not in {"draft_id", "revision", "expires_at"}})).consume()
            row = tx.run("MATCH (m:KDMessage {id:$id,dataset:$d,role:'user',session_id:$s}) RETURN m.id AS id",
                         id=message_id, d=self.dataset, s=scope.session_id).single()
            require(row, "scope_denied", "本轮消息不属于当前会话。", 403)
            tx.run("MATCH (p:KDDraft {id:$id,dataset:$d}) SET p.runtime_version=2,p.owner_session=$s,p.owner_actor=$actor",
                   id=payload["draft_id"], d=self.dataset, s=scope.session_id, actor=scope.actor).consume()
            return {**payload, "runtime_version": 2, "status": "pending"}
        return self.write(write)

    def bind_approval(self, scope, draft_id, tool_call_id):
        scope.check(self, "propose")
        approval_id = str(uuid4())
        def write(tx):
            row = tx.run("MATCH (p:KDDraft {id:$id,dataset:$d,status:'pending',runtime_version:2,owner_session:$s,owner_actor:$actor}) RETURN p.revision AS revision,p.expires_at AS expires",
                         id=draft_id, d=self.dataset, s=scope.session_id, actor=scope.actor).single()
            require(row, "invalid_approval", "无法对当前会话以外或已关闭的预览申请确认。", 403)
            tx.run("CREATE (:KDApproval {id:$id,dataset:$d,session_id:$s,actor:$actor,draft_id:$draft,tool_call_id:$call,revision:$revision,expires_at:$expires,status:'pending'})",
                   id=approval_id, d=self.dataset, s=scope.session_id, actor=scope.actor, draft=draft_id, call=tool_call_id,
                   revision=row["revision"], expires=row["expires"]).consume()
            return {"id": approval_id, "draft_id": draft_id, "tool_call_id": tool_call_id, "revision": row["revision"]}
        return self.write(write)

    def approve(self, scope, draft_id, approval_id, revision):
        scope.check(self, "confirm")
        require(isinstance(approval_id, str) and 0 < len(approval_id) <= 100 and type(revision) is int,
                "invalid_approval", "确认授权或版本格式不正确。")
        def write(tx):
            row = tx.run("MATCH (a:KDApproval {id:$id,dataset:$d,session_id:$s,actor:$actor,draft_id:$draft}) RETURN properties(a) AS a",
                         id=approval_id, d=self.dataset, s=scope.session_id, actor=scope.actor, draft=draft_id).single()
            require(row, "invalid_approval", "确认请求与当前会话的预览不匹配。", 403)
            approval = dict(row["a"])
            require(approval["revision"] == revision, "preview_stale", "确认的预览版本不一致，请刷新预览。", 409)
            require(approval["status"] == "consumed" or approval["expires_at"] > time.time(), "preview_expired", "预览已过期，请重新生成。", 409)
            tx.run("MATCH (a:KDApproval {id:$id}) WHERE a.status='pending' SET a.status='approved'", id=approval_id).consume()
            return approval
        return self.write(write)

    def validate_approval(self, tx, draft, approval_id, scope):
        require(scope is not None, "approval_required", "必须由服务端确认后提交此预览。", 403)
        scope.check(self, "confirm")
        row = tx.run("MATCH (a:KDApproval {id:$id,dataset:$d,session_id:$s,actor:$actor,draft_id:$draft}) RETURN properties(a) AS a",
                     id=approval_id, d=self.dataset, s=scope.session_id, actor=scope.actor, draft=draft["id"]).single()
        require(row and row["a"]["status"] in {"approved", "consumed"} and draft.get("owner_session") == scope.session_id and draft.get("owner_actor") == scope.actor,
                "approval_required", "此预览没有有效的服务端确认。", 403)
        require(row["a"]["revision"] == draft["revision"], "preview_stale", "确认版本与预览不一致。", 409)
        require(row['a']['status']=='consumed' or row['a']['expires_at']>time.time(), 'preview_expired', '操作已过期，请重新发起。', 409)

    def close_execution_draft(self, scope, draft_id):
        scope.check(self, "propose")
        def close(tx):
            tx.run("MATCH (p:KDDraft {id:$id,dataset:$d,owner_session:$s,owner_actor:$actor,status:'pending'}) SET p.status='superseded' "
                   "WITH p OPTIONAL MATCH (a:KDApproval {draft_id:p.id,dataset:$d}) WHERE a.status IN ['pending','approved'] SET a.status='revoked'",
                   id=draft_id, d=self.dataset, s=scope.session_id, actor=scope.actor).consume()
            tx.run("MATCH (e:KDActionEvent {draft_id:$id,dataset:$d,session_id:$s,actor:$actor,status:'prepared'}) SET e.status='superseded'",
                   id=draft_id, d=self.dataset, s=scope.session_id, actor=scope.actor).consume()
        self.write(close)

    def record_commit_context(self, tx, draft, payload, revision):
        if draft.get("runtime_version") != 2:
            return
        cid = digest([self.dataset, draft["owner_session"]])
        row = tx.run("MATCH (c:KDConversation {id:$id}) RETURN c.execution AS state", id=cid).single()
        if row and row["state"]:
            state = json.loads(row["state"])
            state.update(focus_refs=list(dict.fromkeys(o["id"] for o in [*payload["objects"], *payload.get("review_objects", [])])), pending=None,
                         last_commit={"draft_id": draft["id"], "revision": revision})
            # A confirmation run will persist its finished message history later.
            # Focus and commit identity are already atomic with business writes.
            tx.run("MATCH (c:KDConversation {id:$id}) SET c.execution=$state", id=cid, state=encoded(state)).consume()

    def mutation_history(self, entity_id):
        def read(tx):
            self.object(tx, entity_id)
            rows = tx.run("MATCH (m:KDMutation {dataset:$d}) WHERE m.change CONTAINS $id "
                          "OPTIONAL MATCH (p:KDDraft {id:m.id,dataset:$d}) "
                          "RETURN m.id AS id,m.change AS change,m.created_at AS created_at,p.payload AS payload ORDER BY m.created_at",
                          d=self.dataset, id=entity_id)
            result = []
            for row in rows:
                event = dict(row)
                change = json.loads(event["change"])
                payload = json.loads(event.pop("payload") or "{}")
                refs = change.get("entity_refs") or {o["ref"]: o["id"] for o in payload.get("objects", [])}
                change["entity_updates"] = [u for u in change.get("entity_updates", []) if refs.get(u["ref"]) == entity_id]
                change["retractions"] = [r for r in change.get("retractions", []) if r["previous"]["subject_id"] == entity_id]
                change["reviews"] = [r for r in change.get("reviews", []) if r["target_id"] == entity_id or
                                     r["target_type"] == "fact" and r["before"]["subject_id"] == entity_id]
                event["change"] = encoded(change)
                result.append(event)
            return result
        return self.read(read)
