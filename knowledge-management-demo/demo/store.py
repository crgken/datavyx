"""Neo4j confirmed-record extension. Semantic Entity/RELATES_TO are never queried.

Reuses the existing AtlasKnowledge transaction/revision/idempotency approach,
with standalone sources, open type discovery, neighbor paging and audit coverage.
"""
from __future__ import annotations

import json
import time
from copy import copy
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

from neo4j import GraphDatabase

from .model import Change, DemoError, Query, digest, encoded, key, require
from .execution_store import ExecutionStorage
from .review_store import ReviewStorage, fact_status
from .pending_store import PendingStorage
from .index_store import IndexStorage


def uid():
    return str(uuid4())


class Store(ExecutionStorage, ReviewStorage, PendingStorage, IndexStorage):
    def __init__(self, config):
        self.config, self.dataset = config, config.dataset
        self.driver = GraphDatabase.driver(config.uri, auth=(config.user, config.password), connection_timeout=5,
                                           notifications_min_severity="OFF")

    def close(self):
        self.driver.close()

    def for_dataset(self, dataset):
        """Return an immutable dataset view sharing this store's Neo4j driver."""
        if not isinstance(dataset, str) or not dataset or len(dataset) > 200:
            raise ValueError("Invalid dataset scope")
        scoped = copy(self)
        scoped.config = replace(self.config, dataset=dataset)
        scoped.dataset = dataset
        return scoped

    def workspace_datasets(self):
        rows = self.read(lambda tx: [r["dataset"] for r in tx.run(
            "MATCH (w:KDWorkspace {status:'active'}) RETURN DISTINCT w.dataset AS dataset ORDER BY dataset")])
        return sorted(set(rows) | {self.dataset})

    def initialize(self):
        self.driver.verify_connectivity()
        with self.driver.session(database=self.config.database) as session:
            for label in ("KDData", "KDObject", "KDFact", "KDDraft", "KDSource", "KDQuery", "KDMessage", "KDConversation"):
                session.run(f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT kd_identity IF NOT EXISTS FOR (o:KDObject) REQUIRE (o.dataset,o.identity_key) IS UNIQUE").consume()
            session.run("CREATE INDEX kd_fact_subject IF NOT EXISTS FOR (f:KDFact) ON (f.dataset,f.subject_id,f.predicate)").consume()
            session.run("CREATE INDEX kd_type IF NOT EXISTS FOR (o:KDObject) ON (o.dataset,o.kind)").consume()
        self.write(lambda tx: self.lock(tx))
        self.initialize_execution_schema()
        self.initialize_review_schema()
        self.initialize_pending_schema()

    def write(self, fn):
        with self.driver.session(database=self.config.database) as session:
            with session.begin_transaction(timeout=30) as tx:
                result = fn(tx)
                tx.commit()
                return result

    def read(self, fn):
        return self.write(fn)  # finite timeout, consumed records only

    def lock(self, tx, revision=None):
        row = tx.run("MERGE (d:KDData {id:$d}) ON CREATE SET d.revision=0 SET d.lock=true RETURN d.revision AS r", d=self.dataset).single()
        current = row["r"]
        require(revision is None or revision == current, "data_changed", "数据已变化，请从第一页重新浏览；旧分页结果不能宣称完整。", 409)
        return current

    def object(self, tx, oid):
        row = tx.run("MATCH (o:KDObject {dataset:$d,id:$id}) RETURN properties(o) AS o", d=self.dataset, id=oid).single()
        require(row, "object_not_found", "对象不存在。", 404)
        return dict(row["o"])

    def message(self, text, session_id="default", role="user"):
        require(isinstance(text, str) and 0 < len(text.strip()) <= 16000, "invalid_message", "消息不能为空，且最多 16000 字。")
        mid = uid()
        self.write(lambda tx: tx.run("CREATE (:KDMessage {id:$id,dataset:$d,session_id:$s,role:$role,text:$text,created_at:$now})",
                                    id=mid, d=self.dataset, s=session_id, role=role, text=text.strip(), now=time.time()).consume())
        return mid

    def messages(self, session_id="default"):
        return self.read(lambda tx: [dict(r) for r in tx.run(
            "MATCH (m:KDMessage {dataset:$d,session_id:$s}) RETURN m.id AS id,m.role AS role,m.text AS text,m.created_at AS created_at ORDER BY m.created_at DESC,m.id LIMIT 100",
            d=self.dataset, s=session_id)][::-1])

    def task(self, session_id="default"):
        def read(tx):
            row = tx.run("MATCH (c:KDConversation {id:$id,dataset:$d}) RETURN c.task AS task",
                         id=digest([self.dataset, session_id]), d=self.dataset).single()
            return json.loads(row["task"]) if row and row["task"] else None
        return self.read(read)

    def task_recovery_allowed(self, session_id="default"):
        # Legacy recovery is only available before this conversation has used
        # durable tasks. A cleared task is a tombstone, not lost context.
        return self.read(lambda tx: tx.run("MATCH (c:KDConversation {id:$id}) RETURN c.id AS id",
                                          id=digest([self.dataset, session_id])).single() is None)

    def save_task(self, task, session_id="default"):
        def write(tx):
            conversation_id = digest([self.dataset, session_id])
            next_draft = (task or {}).get("draft_id", "")
            tx.run("MATCH (c:KDConversation {id:$id}) MATCH (p:KDDraft {id:c.draft_id,status:'pending'}) "
                   "WHERE p.id <> $draft SET p.status='superseded'", id=conversation_id, draft=next_draft).consume()
            tx.run("MERGE (c:KDConversation {id:$id}) SET c.dataset=$d,c.session_id=$s,c.task=$task,c.draft_id=$draft,c.updated_at=$now",
                   id=conversation_id, d=self.dataset, s=session_id, task=encoded(task) if task else "",
                   draft=next_draft, now=time.time()).consume()
        self.write(write)

    def task_sources(self, message_ids, session_id="default"):
        return self.read(lambda tx: self._task_sources(tx, message_ids, session_id))

    def _task_sources(self, tx, message_ids, session_id):
        require(0 < len(message_ids) <= 24 and len(set(message_ids)) == len(message_ids),
                "invalid_task_sources", "任务来源数量无效，请开始新的任务。")
        rows = [dict(r) for r in tx.run(
            "MATCH (m:KDMessage {dataset:$d,session_id:$s,role:'user'}) WHERE m.id IN $ids RETURN m.id AS id,m.text AS text",
            d=self.dataset, s=session_id, ids=message_ids)]
        by_id = {row["id"]: row for row in rows}
        require(set(by_id) == set(message_ids), "invalid_task_sources", "任务引用了当前会话以外或不存在的用户消息。")
        require(sum(len(row["text"]) for row in rows) <= 48000, "task_too_large", "当前任务内容过长，请拆分为多个任务。")
        return [by_id[mid] for mid in message_ids]

    def prepare(self, change, message_id, source_message_ids=None):
        change = Change.model_validate(change).model_dump()
        require(not change["clarification_required"], "clarification_required", "请先回答澄清问题。")
        require(change["objects"] or change["reviews"], "empty_change", "本次没有可确认的具体对象或操作。")
        return self.write(lambda tx: self._prepare(tx, change, message_id, source_message_ids))

    def _prepare(self, tx, change, message_id, source_message_ids=None, expected_revision=None):
        revision = self.lock(tx, expected_revision)
        msg = tx.run("MATCH (m:KDMessage {dataset:$d,id:$id,role:'user'}) RETURN m.text AS text,m.session_id AS session_id", d=self.dataset, id=message_id).single()
        require(msg, "source_not_found", "原始消息不存在。")
        source_ids = source_message_ids if source_message_ids is not None else [message_id]
        require(message_id in source_ids, "invalid_task_sources", "任务必须包含本轮用户消息。")
        source_messages = self._task_sources(tx, source_ids, msg["session_id"])
        source_text = {row["id"]: row["text"] for row in source_messages}
        refs, objects = set(), []
        for raw in change["objects"]:
            require(raw["ref"] not in refs, "duplicate_ref", "对象引用重复。")
            refs.add(raw["ref"])
            require(raw["name"] or raw["identifier"] or raw["existing_id"], "missing_identity", "对象至少需要名称、编号或明确的已有对象引用。")
            identity = digest([key(raw["kind"]), key(raw["namespace"]), key(raw["identifier"])]) if raw["identifier"] else ""
            candidates = [dict(r["o"]) for r in tx.run(
                "MATCH (o:KDObject {dataset:$d}) WHERE o.kind=$kind AND "
                "(o.identity_key=$identity OR ($name<>'' AND (toLower(o.name)=$name OR $name IN coalesce(o.aliases,[]))) OR ($identifier<>'' AND toLower(o.identifier)=$identifier)) "
                "RETURN properties(o) AS o ORDER BY o.id LIMIT 21", d=self.dataset, kind=raw["kind"], identity=identity,
                name=key(raw["name"]), identifier=key(raw["identifier"]))]
            existing = next((o for o in candidates if identity and o["identity_key"] == identity), None)
            if raw["existing_id"]:
                existing = self.object(tx, raw["existing_id"])
                require(not existing.get("archived", False), "object_archived", "对象已归档，请先恢复后再补充或更正信息。", 409)
                require(existing["kind"] == raw["kind"], "type_conflict", "已有对象类型不同，不能自动合并。")
                # Explicit identity edits need a separate identity migration; never silently rewrite it.
                require(not identity or identity == existing["identity_key"], "identity_conflict", "编号或所属范围与已有对象不一致，请澄清是否为不同对象。")
            if raw["resolution"] == "new":
                require(existing is None, "duplicate_identity", "相同已确认标识已存在，请关联已有对象。")
            require(not existing or not existing.get("archived", False), "object_archived", "对象已归档，请先恢复。", 409)
            oid = existing["id"] if existing else uid()
            ambiguous_identity = not raw["identifier"] or (not raw["namespace"] and any(key(o.get("identifier", "")) == key(raw["identifier"]) for o in candidates))
            unresolved = bool(candidates and existing is None and raw["resolution"] != "new" and raw["certain"] and ambiguous_identity)
            objects.append({**raw, "id": oid, "identity_key": existing["identity_key"] if existing else identity or "new:" + oid,
                            "previous_confirmed":existing['confirmed'] if existing else False,
                            "new": existing is None, "resolved": not unresolved,
                            "candidates": [{k: o.get(k, "") for k in ("id", "name", "kind", "identifier", "namespace")} for o in candidates[:20]],
                            "candidates_truncated": len(candidates) > 20})
        resolved = {o["ref"]: o for o in objects}
        for update in change["entity_updates"]:
            obj = resolved[update["ref"]]
            require(not obj["new"], "invalid_entity_update", "只有已有对象可以执行字段更正。")
            current = self.object(tx, obj["id"])
            require(current.get(update["field"], "") == update["previous"], "entity_changed", "对象字段已变化，请重新生成预览。", 409)
            self.validate_quotes(update["sources"], source_text)
            require(update["field"] != "kind" or bool(update["value"]), "invalid_type", "实体类型不能为空。")
            obj[update["field"]] = update["value"]
        for obj in objects:
            if any(u["ref"] == obj["ref"] for u in change["entity_updates"]):
                obj["identity_key"] = digest([key(obj["kind"]), key(obj["namespace"]), key(obj["identifier"])]) if obj["identifier"] else "new:" + obj["id"]
                require(not tx.run("MATCH (o:KDObject {dataset:$d,identity_key:$identity}) WHERE o.id<>$id RETURN o.id AS id LIMIT 1",
                                   d=self.dataset, identity=obj["identity_key"], id=obj["id"]).single(), "duplicate_identity", "更正后的标识与另一个对象冲突，不能自动合并。")
        retractions = []
        for retract in change["retractions"]:
            self.validate_quotes(retract["sources"], source_text)
            row = tx.run("MATCH (f:KDFact {dataset:$d,id:$id,active:true}) RETURN properties(f) AS f", d=self.dataset, id=retract["fact_id"]).single()
            require(row, "fact_not_found", "要撤回的当前事实不存在或已失效。", 409)
            require(row["f"]["subject_id"] in {o["id"] for o in objects}, "invalid_retraction", "撤回事实必须属于本次明确引用的对象。")
            retractions.append({**retract, "previous": dict(row["f"])})
        seen, cardinalities = set(), {}
        preview_facts = []
        for fact in change["facts"]:
            require(fact["subject"] in resolved and (not fact["object"] or fact["object"] in resolved),
                    "unknown_reference", "事实引用了本次预览中不存在的对象。")
            quotes = fact["sources"] or [{"message_id": message_id, "quote": fact["evidence"]}]
            require(all(q["message_id"] in source_text and q["quote"] in source_text[q["message_id"]] for q in quotes)
                    and fact["evidence"] == quotes[0]["quote"] and len({(q["message_id"],q["quote"]) for q in quotes}) == len(quotes),
                    "invalid_evidence", "来源摘录与任务中的原始用户消息不一致，请重新解析。")
            fact["sources"] = quotes
            fields = self.fact_fields(fact, resolved)
            slot = (resolved[fact["subject"]]["id"], fact["predicate"])
            require(slot not in cardinalities or cardinalities[slot] == fact["cardinality"], "ambiguous_cardinality", "同一关系不能同时为单值与多值。")
            cardinalities[slot] = fact["cardinality"]
            pair = (resolved[fact["subject"]]["id"], fact["predicate"], fields["value_key"] if fact["cardinality"] == "many" else "single")
            require(pair not in seen, "conflicting_facts", "同一对象的同一属性出现多个值，请先澄清。")
            seen.add(pair)
            current = [dict(r["f"]) for r in tx.run(
                "MATCH (f:KDFact {dataset:$d,subject_id:$sid,predicate:$p,active:true,review_status:'confirmed'}) RETURN properties(f) AS f",
                d=self.dataset, sid=resolved[fact["subject"]]["id"], p=fact["predicate"])]
            multi = fact["cardinality"] == "many"
            require(len(current) <= 1 or multi, "integrity_error", "该关系已有多个当前值，请明确具体要更正哪一条关系。")
            require(not multi or fact["mode"] != "replace" or len(current) <= 1,
                    "ambiguous_replacement", "多值关系更正需要明确旧关系；请分别说明要替换的关联对象。")
            same_fact = next((f for f in current if f["value_key"] == fields["value_key"] and
                              (fields["value_type"] != "number" or not fact["certain"] or f.get("numeric_precise", False))), None)
            same = same_fact is not None
            require(not current or same or multi or not fact["certain"] or fact["mode"] == "replace",
                    "correction_required", "当前已有不同的属性值；请明确说明是否更正。")
            preview_facts.append({**fact, "id": same_fact["id"] if same else uid(),
                                  "previous": current, "supersedes": [f["id"] for f in current] if not same and (fact["certain"] or fact['mode']=='replace') and (not multi or fact["mode"] == "replace") else []})
        overrides = {}
        for u in change["entity_updates"]:
            overrides.setdefault(resolved[u["ref"]]["id"], {})[u["field"]] = u["value"]
        for obj in objects:
            if obj["id"] in overrides:
                overrides[obj["id"]]["identity_key"] = obj["identity_key"]
        reviews, impact, review_objects = self.prepare_review_operations(tx, change.get("reviews", []), source_text, overrides)
        asserted_slots = {(resolved[f["subject"]]["id"], f["predicate"]) for f in preview_facts}
        require(not any(o["target_type"] == "fact" and o["action"] in {"confirm", "restate"} and
                        (o["before"]["subject_id"], o["before"]["predicate"]) in asserted_slots for o in reviews),
                "conflicting_operations", "同一属性不能同时重新断言并确认或重新登记旧版本，请明确采用哪种变更。")
        require(not ({r["fact_id"] for r in retractions} & {o["target_id"] for o in reviews if o["target_type"] == "fact"}),
                "conflicting_operations", "同一事实不能同时通过两种操作修改。")
        payload = {"objects": objects, "facts": preview_facts, "message_id": message_id,
                   "source": "\n\n".join(row["text"] for row in source_messages), "source_messages": source_messages, "change": change,
                   "entity_updates": change["entity_updates"], "retractions": retractions,
                   "reviews": reviews, "review_impact": impact, "review_objects": review_objects, "origin": "agent"}
        draft_id, now = uid(), time.time()
        tx.run("CREATE (:KDDraft {id:$id,dataset:$d,revision:$r,payload:$payload,status:'pending',created_at:$now,expires_at:$expires})",
               id=draft_id, d=self.dataset, r=revision, payload=encoded(payload), now=now, expires=now + 1800).consume()
        return {"draft_id": draft_id, "revision": revision, "expires_at": now + 1800, **payload}

    @staticmethod
    def fact_fields(fact, objects):
        if fact["object"]:
            target = objects[fact["object"]]["id"]
            return {"object_id": target, "value_json": "", "value_key": digest({"object_id": target}),
                    "value_type": "object", "number_scaled": None, "unit": ""}
        from .model import Value
        return Value.model_validate(fact["value"]).fields()

    def draft(self, draft_id):
        require(isinstance(draft_id, str) and 0 < len(draft_id) <= 100, "invalid_draft", "预览标识无效。")
        def read(tx):
            row = tx.run("MATCH (d:KDDraft {dataset:$dataset,id:$id}) RETURN properties(d) AS draft", dataset=self.dataset, id=draft_id).single()
            require(row, "draft_not_found", "预览不存在。", 404)
            data = dict(row["draft"])
            return {**json.loads(data.pop("payload")), **data, "draft_id": data["id"]}
        return self.read(read)

    def resolve(self, draft_id, choices):
        draft = self.draft(draft_id)
        require(draft["status"] == "pending", "draft_closed", "该预览已经提交。")
        require(isinstance(choices, dict), "invalid_choices", "对象选择格式错误。")
        change = draft["change"]
        refs = {o["ref"] for o in change["objects"]}
        require(set(choices) <= refs, "invalid_choices", "对象选择引用不存在。")
        for obj in change["objects"]:
            choice = choices.get(obj["ref"])
            if choice == "new":
                obj["resolution"], obj["existing_id"] = "new", ""
            elif choice:
                selected = self.read(lambda tx: self.object(tx, choice))
                obj["identifier"], obj["namespace"] = selected["identifier"], selected["namespace"]
                obj["resolution"], obj["existing_id"] = "existing", choice
        updated = self.prepare(change, draft["message_id"], [row["id"] for row in draft.get("source_messages", [{"id": draft["message_id"]}])])
        task = self.task()
        if task and task.get("draft_id") == draft_id:
            self.save_task({**task, "draft_id": updated["draft_id"]})
        return updated

    def commit(self, draft_id, approval_id="", scope=None):
        require(isinstance(draft_id, str) and 0 < len(draft_id) <= 100, "invalid_draft", "预览标识无效。")
        return self.write(lambda tx: self._commit(tx, draft_id, approval_id, scope))

    def _commit(self, tx, draft_id, approval_id="", scope=None):
        revision = self.lock(tx)
        row = tx.run("MATCH (d:KDDraft {dataset:$d,id:$id}) RETURN properties(d) AS draft", d=self.dataset, id=draft_id).single()
        require(row, "draft_not_found", "预览不存在。", 404)
        draft = dict(row["draft"])
        if draft.get("runtime_version") == 2:
            self.validate_approval(tx, draft, approval_id, scope)
        if draft["status"] == "committed":
            return {"draft_id": draft_id, "idempotent": True, "projection_status": draft["projection_status"]}
        require(draft["status"] == "pending", "draft_closed", "该预览已被后续澄清或取消替代，请使用最新预览。", 409)
        require(draft["expires_at"] > time.time(), "preview_expired", "预览已过期，请重新解析。", 409)
        require(draft["revision"] == revision, "preview_stale", "数据已变化，请更新预览后再确认。", 409)
        payload = json.loads(draft["payload"])
        if payload.get("bulk"):
            bulk = payload["bulk"]
            require(bulk["complete"] and bulk["processed_rows"] == bulk["row_count"] and not bulk["missing"],
                    "table_incomplete", "导入覆盖不完整，不能保存。")
        self.verify_review_snapshots(tx, payload)
        require(all(o["resolved"] for o in payload["objects"]), "identity_unresolved", "请先选择关联已有对象或创建新对象，并更新预览。")
        objects = {o["ref"]: o for o in payload["objects"]}
        now, source_id = time.time(), payload["message_id"]
        source_messages = payload.get("source_messages", [{"id": source_id, "text": payload["source"]}])
        for source in source_messages:
            tx.run("MERGE (s:KDSource {id:$id}) ON CREATE SET s.dataset=$d,s.message=$message,s.created_at=$now",
                   id=source["id"], d=self.dataset, message=source["text"], now=now).consume()
        for obj in objects.values():
            tx.run("MERGE (o:KDObject {id:$id}) ON CREATE SET o.dataset=$d,o.name=$name,o.kind=$kind,o.identifier=$identifier,o.namespace=$namespace,o.identity_key=$identity,o.created_at=$now,o.confirmed=$certain,o.reason=$reason,o.aliases=[],o.archived=false "
                   "SET o.confirmed=o.confirmed OR $certain "
                   "SET o.review_status=CASE WHEN o.confirmed THEN 'confirmed' ELSE 'pending' END "
                   "SET o.reason=CASE WHEN o.confirmed THEN '' ELSE coalesce(o.reason,$reason) END "
                   "SET o.aliases=CASE WHEN $alias IN o.aliases THEN o.aliases ELSE o.aliases+[$alias] END "
                   "WITH o MATCH (s:KDSource {dataset:$d}) WHERE s.id IN $sources MERGE (s)-[:KD_SUPPORTS]->(o)",
                   id=obj["id"], d=self.dataset, name=obj["name"], kind=obj["kind"], identifier=obj["identifier"], namespace=obj["namespace"],
                   identity=obj["identity_key"], now=now, certain=obj["certain"], reason=obj["reason"], alias=key(obj["name"]), sources=[s["id"] for s in source_messages]).consume()
        for fact in payload["facts"]:
            subject = objects[fact["subject"]]
            certain = fact["certain"] and subject["certain"] and (not fact["object"] or objects[fact["object"]]["certain"])
            fields = self.fact_fields(fact, objects)
            if fact["certain"] and fact["supersedes"]:
                tx.run("MATCH (f:KDFact {dataset:$d}) WHERE f.id IN $ids SET f.active=false,f.ended_at=$now,f.replaced_by=$new",
                       d=self.dataset, ids=fact["supersedes"], now=now, new=fact["id"]).consume()
            # Uncertain assertions never attach to or overwrite confirmed facts.
            fid = fact["id"] if fact["certain"] else digest([source_id, fact["subject"], fact["predicate"], fields["value_key"], "pending"])
            for quote in fact.get("sources") or [{"message_id": source_id, "quote": fact["evidence"]}]:
                tx.run("MERGE (f:KDFact {id:$id}) ON CREATE SET f.dataset=$d,f.subject_id=$sid,f.predicate=$p,f.object_id=$object_id,f.value_json=$value_json,f.value_key=$value_key,f.value_type=$value_type,f.number_scaled=$number_scaled,f.unit=$unit,f.confirmed=$certain,f.active=true,f.reason=$reason,f.created_at=$now,f.cardinality=$cardinality,f.review_status=$review,f.numeric_precise=$precise "
                       "WITH f MATCH (o:KDObject {id:$sid}), (s:KDSource {id:$source}) MERGE (o)-[:KD_HAS_FACT]->(f) "
                       "MERGE (s)-[e:KD_SUPPORTS]->(f) SET e.quotes=CASE WHEN $evidence IN coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END) THEN coalesce(e.quotes,[e.quote]) ELSE coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END)+[$evidence] END,e.quote=coalesce(e.quote,$evidence) "
                       "WITH f OPTIONAL MATCH (t:KDObject {id:$object_id}) FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | MERGE (f)-[:KD_TARGET]->(t))",
                       id=fid, d=self.dataset, sid=subject["id"], p=fact["predicate"], **fields, certain=certain,
                       reason=fact["reason"] or ("关联对象身份待确认" if not certain else ""), now=now, source=quote["message_id"], evidence=quote["quote"], cardinality=fact["cardinality"],
                       review="confirmed" if fact["certain"] else "pending", precise=fact["certain"] if fields["value_type"] == "number" else True).consume()
        for update in payload.get("entity_updates", []):
            obj = objects[update["ref"]]
            values = {update["field"]: update["value"], "identity_key": obj["identity_key"]}
            tx.run("MATCH (o:KDObject {dataset:$d,id:$id}) SET o += $values", d=self.dataset, id=obj["id"], values=values).consume()
        for retract in payload.get("retractions", []):
            tx.run("MATCH (f:KDFact {dataset:$d,id:$id,active:true}) SET f.active=false,f.ended_at=$now,f.retracted_by=$draft",
                   d=self.dataset, id=retract["fact_id"], now=now, draft=draft_id).consume()
        self.apply_reviews(tx, payload, draft_id, scope.actor if scope else "structured-import")
        tx.run("CREATE (:KDMutation {id:$id,dataset:$d,change:$change,created_at:$now})",
               id=draft_id, d=self.dataset, change=encoded({"entities": list(dict.fromkeys(o["id"] for o in [*payload["objects"], *payload.get("review_objects", [])])),
                   "entity_refs": {o["ref"]: o["id"] for o in payload["objects"]},
                   "entity_updates": payload.get("entity_updates", []), "retractions": payload.get("retractions", []),
                   "reviews": payload.get("reviews", []), "actor": scope.actor if scope else "structured-import",
                   "action_event_id": payload.get("action_event_id", "")}), now=now).consume()
        if approval_id:
            tx.run("MATCH (a:KDApproval {id:$id,dataset:$d}) SET a.status='consumed'", id=approval_id, d=self.dataset).consume()
        tx.run("MATCH (d:KDData {id:$d}), (p:KDDraft {id:$id}) SET d.revision=d.revision+1,p.status='committed',p.committed_at=$now,p.projection_status='pending',p.attempts=0",
               d=self.dataset, id=draft_id, now=now).consume()
        tx.run("MATCH (c:KDConversation {dataset:$d,draft_id:$id}) SET c.task='',c.draft_id=''",
               d=self.dataset, id=draft_id).consume()
        self.record_commit_context(tx, draft, payload, revision + 1)
        if payload.get("bulk"):
            job_row = tx.run("MATCH (j:KDImport {id:$id,dataset:$d,session_id:$s,actor:$actor}) RETURN j.payload AS payload",
                             id=payload["bulk"]["import_id"], d=self.dataset, s=scope.session_id, actor=scope.actor).single()
            require(job_row, "import_not_found", "导入任务不存在。", 409)
            job = json.loads(job_row["payload"])
            job.update(status="committed", entity_refs={o["ref"]:o["id"] for o in objects.values()})
            tx.run("MATCH (j:KDImport {id:$id}) SET j.payload=$payload", id=job["id"], payload=encoded(job)).consume()
        changed_ids={o['id'] for o in [*payload['objects'],*payload.get('review_objects',[])]}
        dependencies={o['id'] for o in payload['objects'] if o['new'] or not o.get('previous_confirmed',False)}
        dependencies.update(objects[u['ref']]['id'] for u in payload.get('entity_updates',[]))
        dependencies.update(op['target_id'] for op in payload.get('reviews',[]) if op['target_type']=='entity')
        self.enqueue_indexes(tx,changed_ids,dependencies)
        return {"draft_id": draft_id, "revision": revision + 1, "idempotent": False, "projection_status": "pending"}

    @staticmethod
    def where(query, confirmed=True):
        parts, params = ["o.dataset=$d", "NOT coalesce(o.archived,false)"], {}
        if confirmed:
            parts.append("o.confirmed=true")
        if query.kind:
            parts.append("o.kind=$kind")
            params["kind"] = query.kind
        if query.object_ids:
            parts.append("o.id IN $ids")
            params["ids"] = query.object_ids
        if confirmed:
            for index, item in enumerate(query.filters):
                params[f"p{index}"] = item.predicate
                condition = ""
                if item.op != "exists":
                    params[f"v{index}"] = item.object_id if item.direction == "in" else digest({"object_id": item.object_id}) if item.object_id else item.value.fields()["value_key"]
                    value_field = "subject_id" if item.direction == "in" else "value_key"
                    condition = f" AND f.{value_field} {'=' if item.op == 'eq' else '<>'} $v{index}"
                subject_field = "object_id" if item.direction == "in" else "subject_id"
                parts.append(f"EXISTS {{ MATCH (f:KDFact {{dataset:$d,{subject_field}:o.id,predicate:$p{index},active:true,confirmed:true}}) WHERE true{condition} }}")
        return " AND ".join(parts), params

    def query(self, raw):
        q = Query.model_validate(raw)
        return self.write(lambda tx: self._query(tx, q))

    def _query(self, tx, q):
        revision = self.lock(tx, q.revision)
        where, params = self.where(q)
        params["d"] = self.dataset
        base = "MATCH (o:KDObject) WHERE " + where
        population, population_params = self.where(q, confirmed=False)
        population_ids = [dict(r) for r in tx.run("MATCH (o:KDObject) WHERE " + population + " RETURN o.id AS id,o.confirmed AS confirmed ORDER BY o.id", **population_params, d=self.dataset)]
        matches = [r["id"] for r in tx.run(base + " RETURN o.id AS id ORDER BY o.id", **params)]
        matched_set = set(matches)
        exclusions = [{"id": o["id"], "reason": "身份或类型待确认" if not o["confirmed"] else "条件不匹配或筛选事实未确认"} for o in population_ids if o["id"] not in matched_set]
        data = {"operation": q.operation, "revision": revision, "query": q.model_dump(), "scope": "数据库中已确认、当前有效的规范记录，不含归档对象；不保证现实业务全部录入",
                "matched_count": len(matches), "population_count": len(population_ids), "offset": q.offset, "limit": q.limit}
        participants = matches
        if q.operation == "COUNT":
            # The count itself is executed by Neo4j, never inferred from a semantic result.
            data["count"] = tx.run(base + " RETURN count(DISTINCT o.id) AS n", **params).single()["n"]
            data["rows"] = self._object_page(tx, matches, q.offset, q.limit)
            data["total"] = data["count"]
        elif q.operation == "LIST":
            data["rows"] = self._object_page(tx, matches, q.offset, q.limit)
            data["total"] = len(matches)
        else:
            subject_field = "object_id" if q.direction == "in" else "subject_id"
            facts_base = base + f" OPTIONAL MATCH (f:KDFact {{dataset:$d,{subject_field}:o.id,predicate:$predicate,active:true,confirmed:true}})"
            params["predicate"] = q.predicate
            records = [dict(r) for r in tx.run(facts_base + " RETURN o.id AS id,collect(properties(f)) AS facts ORDER BY id", **params)]
            if q.operation == "SUM":
                require(all(len(r["facts"]) <= 1 for r in records), "integrity_error", "数值属性存在多个当前值，拒绝统计。")
            covered, missing = [], []
            for row in records:
                if len(row["facts"]) == 1:
                    covered.append((row["id"], row["facts"][0]))
                else:
                    missing.append({"id": row["id"], "reason": "分组关系有多个值，归属未确定" if len(row["facts"]) > 1 else "属性缺失或待确认"})
            data["missing_count"] = len(missing)
            if q.operation == "GROUP":
                grouping = "f.subject_id AS key,'' AS value_json,f.subject_id AS object_id" if q.direction == "in" else "f.value_key AS key,f.value_json AS value_json,f.object_id AS object_id"
                groups = [dict(r) for r in tx.run(facts_base + " WITH o,collect(f) AS fs WHERE size(fs)=1 WITH o,fs[0] AS f RETURN " + grouping + ",count(DISTINCT o.id) AS count ORDER BY key", **params)]
                for group in groups:
                    group["value"] = json.loads(group.pop("value_json")) if group["value_json"] else {"object_id": group["object_id"], "value": (self.object(tx, group["object_id"])["name"] or self.object(tx, group["object_id"])["identifier"])}
                if missing:
                    groups.append({"key": "unresolved", "value": {"value": "未确定"}, "count": len(missing)})
                data.update(groups=groups[q.offset:q.offset + q.limit], total=len(groups), total_groups=len(groups), unknown_count=len(missing), unknown_reasons=self._reasons(missing))
                participants = matches
            else:
                invalid = [{"id": oid, "reason": "非精确数值" if f["value_type"] != "number" else "币种或单位不一致：" + f["unit"]} for oid, f in covered if f["value_type"] != "number" or f["unit"] != q.unit]
                exclusions += missing + invalid
                invalid_ids = {r["id"] for r in missing + invalid}
                participants = [oid for oid in matches if oid not in invalid_ids]
                result = tx.run(base + " MATCH (f:KDFact {dataset:$d,subject_id:o.id,predicate:$predicate,active:true,confirmed:true,value_type:'number',unit:$unit}) "
                    "RETURN sum(f.number_scaled) AS total", **params, unit=q.unit).single()["total"] or 0
                require(abs(result) < 2**53, "sum_overflow", "合计超过当前精确数值范围，拒绝返回近似结果。")
                data.update(sum=format(Decimal(result) / Decimal(1_000_000), "f"), unit=q.unit, total=len(participants))
        page_count = len(data.get("groups", data.get("rows", [])))
        data.update(participating_count=len(participants), excluded_count=len(exclusions), exclusion_summary=self._reasons(exclusions),
                    returned_count=page_count, has_more=q.operation in {"COUNT", "LIST", "GROUP"} and q.offset + page_count < data["total"])
        data["complete"] = not data["has_more"]
        audit_id = uid()
        tx.run("CREATE (:KDQuery {id:$id,dataset:$d,revision:$revision,query:$query_json,participants:$participants,exclusions:$exclusions,result:$result,created_at:$now})",
               id=audit_id, d=self.dataset, revision=revision, query_json=encoded(q.model_dump()), participants=participants,
               exclusions=encoded(exclusions), result=encoded(data), now=time.time()).consume()
        return {**data, "query_id": audit_id, "exclusion_examples": exclusions[:20], "exclusion_examples_truncated": len(exclusions) > 20}

    @staticmethod
    def _reasons(rows):
        result = {}
        for row in rows:
            result[row["reason"]] = result.get(row["reason"], 0) + 1
        return result

    def _object_page(self, tx, ids, offset, limit):
        return [dict(r["o"]) for r in tx.run("MATCH (o:KDObject {dataset:$d}) WHERE o.id IN $ids RETURN properties(o) AS o ORDER BY o.id SKIP $offset LIMIT $limit",
                                          d=self.dataset, ids=ids, offset=offset, limit=limit)]

    def types(self, offset=0, limit=25, revision=None, include_archived=False):
        self.page_bounds(offset, limit)
        def read(tx):
            version = self.lock(tx, revision)
            condition = " WHERE $archived OR NOT coalesce(o.archived,false) "
            total = tx.run("MATCH (o:KDObject {dataset:$d})" + condition + "RETURN count(DISTINCT o.kind) AS n", d=self.dataset, archived=include_archived).single()["n"]
            rows = [dict(r) for r in tx.run("MATCH (o:KDObject {dataset:$d})" + condition + "RETURN o.kind AS kind,count(o) AS total,count(CASE WHEN o.confirmed THEN 1 END) AS confirmed ORDER BY kind SKIP $offset LIMIT $limit",
                d=self.dataset, archived=include_archived, offset=offset, limit=limit)]
            return self.page_result(rows, total, offset, limit, version)
        return self.write(read)

    @staticmethod
    def page_bounds(offset, limit):
        require(type(offset) is int and 0 <= offset <= 10**8 and type(limit) is int and 1 <= limit <= 100,
                "invalid_page", "分页参数错误；每页支持 1–100 条。")

    @staticmethod
    def page_result(rows, total, offset, limit, revision):
        more = offset + len(rows) < total
        return {"rows": rows, "total": total, "offset": offset, "limit": limit, "revision": revision,
                "returned_count": len(rows), "has_more": more, "complete": not more,
                "scope": "当前数据库记录范围；后续页必须保持相同数据版本"}

    def objects(self, kind="", offset=0, limit=25, revision=None, include_archived=False, status='all'):
        self.page_bounds(offset, limit)
        require(status in {'all','confirmed','pending'},'invalid_status','状态筛选无效。')
        def read(tx):
            version = self.lock(tx, revision)
            condition = (" WHERE true" if include_archived else " WHERE NOT coalesce(o.archived,false)") + (" AND o.kind=$kind" if kind else "")
            pending_ids=list({p['subject_id'] for p in self.pending_proposals(tx)})
            pending="(o.confirmed=false OR o.id IN $pending_ids OR EXISTS { MATCH (f:KDFact {dataset:$d,subject_id:o.id,active:true,review_status:'pending'}) })"
            if status!='all':
                condition+=' AND '+(pending if status=='pending' else 'NOT '+pending)
            total = tx.run("MATCH (o:KDObject {dataset:$d})" + condition + " RETURN count(o) AS n", d=self.dataset, kind=kind,pending_ids=pending_ids).single()["n"]
            rows = [dict(r["o"]) for r in tx.run("MATCH (o:KDObject {dataset:$d})" + condition + " RETURN properties(o) AS o ORDER BY o.id SKIP $offset LIMIT $limit", d=self.dataset, kind=kind,pending_ids=pending_ids, offset=offset, limit=limit)]
            return {**self.page_result(rows, total, offset, limit, version), "kind": kind}
        return self.write(read)

    def detail(self, oid, offset=0, limit=25, revision=None, history=False):
        self.page_bounds(offset, limit)
        def read(tx):
            version = self.lock(tx, revision)
            obj = self.entity_view(self.object(tx, oid))
            condition = "" if history else " AND f.active=true"
            total = tx.run("MATCH (f:KDFact {dataset:$d,subject_id:$id}) WHERE true" + condition + " RETURN count(f) AS n", d=self.dataset, id=oid).single()["n"]
            rows = [dict(r["f"]) for r in tx.run("MATCH (f:KDFact {dataset:$d,subject_id:$id}) WHERE true" + condition + " RETURN properties(f) AS f ORDER BY f.predicate,f.created_at,f.id SKIP $offset LIMIT $limit", d=self.dataset, id=oid, offset=offset, limit=limit)]
            rows = [self.fact_view(tx, fact) for fact in rows]
            for fact in rows:
                fact["source_count"] = tx.run("MATCH (:KDSource)-[e:KD_SUPPORTS]->(:KDFact {id:$id}) RETURN count(e) AS n", id=fact["id"]).single()["n"]
            dependencies = list({f["object_id"]:self.entity_view(self.object(tx, f["object_id"])) for f in rows if f.get("object_id")}.values())
            proposals=self.pending_proposals(tx,oid)
            return {**self.page_result(rows, total, offset, limit, version), "object": obj, "history": history, "dependencies": dependencies,'pending_proposals':proposals}
        return self.write(read)

    def sources(self, fact_id, offset=0, limit=25):
        self.page_bounds(offset, limit)
        def read(tx):
            total = tx.run("MATCH (:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(:KDFact {dataset:$d,id:$id}) RETURN count(e) AS n", d=self.dataset, id=fact_id).single()["n"]
            rows = [dict(r) for r in tx.run("MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(:KDFact {dataset:$d,id:$id}) RETURN s.id AS source_id,s.message AS message,e.quote AS quote,coalesce(e.quotes,[e.quote]) AS quotes,s.created_at AS created_at ORDER BY s.created_at,s.id SKIP $offset LIMIT $limit", d=self.dataset, id=fact_id, offset=offset, limit=limit)]
            return self.page_result(rows, total, offset, limit, None)
        return self.read(read)

    def neighbors(self, oid, offset=0, limit=25, revision=None, depth=0, max_depth=4):
        self.page_bounds(offset, limit)
        require(type(depth) is int and type(max_depth) is int and 0 <= depth < max_depth <= 8,
                "max_depth", "已达到展开深度限制；可以从该对象重新开始浏览，最大深度为 8。")
        def read(tx):
            version = self.lock(tx, revision)
            self.object(tx, oid)
            base = "MATCH (f:KDFact {dataset:$d,active:true}), (s:KDObject {dataset:$d,id:f.subject_id}), (t:KDObject {dataset:$d,id:f.object_id}) WHERE f.object_id<>'' AND (f.subject_id=$id OR f.object_id=$id) AND NOT coalesce(s.archived,false) AND NOT coalesce(t.archived,false) "
            total = tx.run(base + "RETURN count(f) AS n", d=self.dataset, id=oid).single()["n"]
            rows = [dict(r) for r in tx.run(base + " MATCH (o:KDObject {dataset:$d}) WHERE o.id=CASE WHEN f.subject_id=$id THEN f.object_id ELSE f.subject_id END "
                "RETURN f.id AS fact_id,f.predicate AS predicate,f.review_status AS review_status,CASE WHEN f.subject_id=$id THEN 'out' ELSE 'in' END AS direction,properties(o) AS object ORDER BY object.id,fact_id SKIP $offset LIMIT $limit", d=self.dataset, id=oid, offset=offset, limit=limit)]
            return {**self.page_result(rows, total, offset, limit, version), "depth": depth, "max_depth": max_depth}
        return self.write(read)

    def graph(self, revision=None):
        """A complete, versioned canonical graph, including isolated objects."""
        def read(tx):
            version = self.lock(tx, revision)
            objects = {r["o"]["id"]:self.entity_view(dict(r["o"])) for r in tx.run(
                "MATCH (o:KDObject {dataset:$d}) WHERE NOT coalesce(o.archived,false) RETURN properties(o) AS o ORDER BY o.id", d=self.dataset)}
            edges = []
            for row in tx.run(
                "MATCH (f:KDFact {dataset:$d,active:true}) WHERE f.object_id<>'' AND f.subject_id IN $ids AND f.object_id IN $ids "
                "OPTIONAL MATCH (:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(f) "
                "RETURN properties(f) AS f,count(e) AS sources ORDER BY f.id", d=self.dataset, ids=list(objects)):
                fact = dict(row["f"])
                status, reasons = fact_status(fact, objects)
                edges.append({"id":fact["id"], "source":fact["subject_id"], "target":fact["object_id"],
                    "predicate":fact["predicate"], "review_status":fact.get("review_status", "pending"),
                    "effective_status":status, "blocked_reasons":reasons, "source_count":row["sources"]})
            pending_ids={r['id'] for r in tx.run("MATCH (f:KDFact {dataset:$d,active:true,review_status:'pending'}) RETURN DISTINCT f.subject_id AS id",d=self.dataset)}
            pending_ids.update(p['subject_id'] for p in self.pending_proposals(tx))
            for oid,obj in objects.items():
                obj['has_pending']=not obj['confirmed'] or oid in pending_ids
            return {"nodes":list(objects.values()), "edges":edges, "node_count":len(objects), "edge_count":len(edges),
                    "revision":version, "complete":True, "scope":"当前数据范围的全部未归档对象与当前关系；虚线关系待确认或暂不生效"}
        return self.write(read)

    def audit(self, query_id, offset=0, limit=25):
        self.page_bounds(offset, limit)
        def read(tx):
            row = tx.run("MATCH (q:KDQuery {dataset:$d,id:$id}) RETURN properties(q) AS q", d=self.dataset, id=query_id).single()
            require(row, "query_not_found", "查询记录不存在。", 404)
            q = dict(row["q"])
            members = [{"id": oid, "reason": "参与计算"} for oid in q["participants"]] + json.loads(q["exclusions"])
            page = members[offset:offset + limit]
            for member in page:
                member["object"] = self.object(tx, member["id"])
            return {**self.page_result(page, len(members), offset, limit, q["revision"]), "query": json.loads(q["query"]), "result": json.loads(q["result"])}
        return self.read(read)

    def context(self):
        """Bounded planner context is explicitly separate from full deterministic queries."""
        def read(tx):
            revision = self.lock(tx)
            kinds = [r["value"] for r in tx.run("MATCH (o:KDObject {dataset:$d}) WHERE NOT coalesce(o.archived,false) RETURN DISTINCT o.kind AS value ORDER BY value LIMIT 101", d=self.dataset)]
            predicates = [r["value"] for r in tx.run("MATCH (f:KDFact {dataset:$d}) RETURN DISTINCT f.predicate AS value ORDER BY value LIMIT 101", d=self.dataset)]
            objects = [dict(r["o"]) for r in tx.run("MATCH (o:KDObject {dataset:$d}) WHERE NOT coalesce(o.archived,false) RETURN properties(o) AS o ORDER BY o.created_at DESC,o.id LIMIT 101", d=self.dataset)]
            signatures = [dict(r) for r in tx.run("MATCH (s:KDObject {dataset:$d})-[:KD_HAS_FACT]->(f:KDFact {active:true,confirmed:true}) OPTIONAL MATCH (f)-[:KD_TARGET]->(t:KDObject) RETURN DISTINCT s.kind AS subject_kind,f.predicate AS predicate,coalesce(t.kind,f.value_type) AS target_kind LIMIT 101", d=self.dataset)]
            return {"kinds": kinds[:100], "predicates": predicates[:100], "objects": objects[:100], "revision": revision,
                    "relation_signatures":signatures[:100], "context_truncated": len(kinds)>100 or len(predicates)>100 or len(objects)>100 or len(signatures)>100}
        return self.write(read)

    def pending_projections(self):
        return self.read(lambda tx: [dict(r["p"]) for r in tx.run("MATCH (p:KDDraft {dataset:$d,status:'committed',projection_status:'pending'}) WHERE coalesce(p.retry_at,0)<$now RETURN properties(p) AS p ORDER BY p.committed_at,p.id LIMIT 5", d=self.dataset, now=time.time())])

    def projection_result(self, draft_id, episode_id=None, snapshot_hash=""):
        def write(tx):
            if episode_id:
                tx.run("MATCH (p:KDDraft {dataset:$d,id:$id}) SET p.projection_status='ready',p.episode_id=$episode,p.snapshot_hash=$snapshot REMOVE p.retry_at,p.error_code", d=self.dataset, id=draft_id, episode=episode_id, snapshot=snapshot_hash).consume()
            else:
                tx.run("MATCH (p:KDDraft {dataset:$d,id:$id}) SET p.attempts=p.attempts+1,p.retry_at=$now+60,p.error_code='projection_unavailable'", d=self.dataset, id=draft_id, now=time.time()).consume()
        self.write(write)

    def health(self):
        return self.index_health()
