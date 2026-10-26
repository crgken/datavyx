"""Explicit, audited lifecycle operations and derived fact eligibility."""
import json
import time
from copy import deepcopy
from uuid import uuid4

from .model import digest, encoded, key, require


def fact_status(fact, entities):
    reasons = []
    if not fact.get("active", True):
        return ("retracted" if fact.get("retracted_by") else "superseded"), []
    if fact.get("review_status", "confirmed" if fact.get("confirmed") else "pending") != "confirmed":
        reasons.append("内容待确认")
    for oid in dict.fromkeys([fact["subject_id"], *([fact["object_id"]] if fact.get("object_id") else [])]):
        obj = entities[oid]
        label = obj.get("name") or obj.get("identifier") or "关联对象"
        if obj.get("archived", False):
            reasons.append(label + "已归档")
        elif not obj["confirmed"]:
            reasons.append(label + "身份或类型待确认")
    if fact["value_type"] == "number" and not fact.get("numeric_precise", fact.get("confirmed", False)):
        reasons.append("数值仍不精确，需要补充确定值")
    return ("effective" if not reasons else "pending" if reasons == ["内容待确认"] else "blocked"), reasons


class ReviewStorage:
    def validate_review_source(self,tx,target_type,target_id):
        label='KDObject' if target_type=='entity' else 'KDFact'
        rows=[dict(r) for r in tx.run(f'MATCH (s:KDSource {{dataset:$d}})-[e:KD_SUPPORTS]->(n:{label} {{dataset:$d,id:$id}}) RETURN s.message AS text,coalesce(s.valid,true) AS valid,coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END) AS quotes',d=self.dataset,id=target_id)]
        require(rows and all(r['valid'] and all(q in r['text'] for q in r['quotes']) for r in rows),'source_invalid','原记录来源缺失或失效，不能确认；可撤回后补充有效信息。',409)

    def initialize_review_schema(self):
        with self.driver.session(database=self.config.database) as session:
            session.run("CREATE CONSTRAINT kd_action_event_id IF NOT EXISTS FOR (n:KDActionEvent) REQUIRE n.id IS UNIQUE").consume()
        def migrate(tx):
            row = tx.run("MATCH (d:KDData {id:$d}) RETURN d.review_schema_version AS v", d=self.dataset).single()
            if row["v"] == 3:
                return
            count = tx.run("MATCH (o:KDObject {dataset:$d}) RETURN count(o) AS n", d=self.dataset).single()["n"]
            tx.run("MATCH (o:KDObject {dataset:$d}) SET o.legacy_confirmed=o.confirmed,o.archived=coalesce(o.archived,false),"
                   "o.review_status=CASE WHEN o.confirmed THEN 'confirmed' ELSE 'pending' END", d=self.dataset).consume()
            tx.run("MATCH (f:KDFact {dataset:$d}) SET f.legacy_confirmed=f.confirmed,"
                   "f.review_status=CASE WHEN f.confirmed THEN 'confirmed' ELSE 'pending' END,"
                   "f.numeric_precise=CASE WHEN f.value_type='number' THEN f.confirmed ELSE true END", d=self.dataset).consume()
            self.refresh_effective_facts(tx)
            tx.run("MATCH (d:KDData {id:$d}) SET d.review_schema_version=3,d.schema_version=3,d.revision=d.revision+$bump",
                   d=self.dataset, bump=1 if count else 0).consume()
            if count:
                tx.run("MATCH (p:KDDraft {dataset:$d,status:'pending'}) SET p.status='superseded'", d=self.dataset).consume()
                tx.run("MATCH (a:KDApproval {dataset:$d}) WHERE a.status IN ['pending','approved'] SET a.status='revoked'", d=self.dataset).consume()
                tx.run("MATCH (e:KDActionEvent {dataset:$d,status:'prepared'}) SET e.status='superseded'", d=self.dataset).consume()
                for row in tx.run("MATCH (c:KDConversation {dataset:$d}) WHERE c.task IS NOT NULL AND c.task<>'' RETURN c.id AS id,c.task AS task", d=self.dataset):
                    task = json.loads(row["task"])
                    if task.get("draft_id"):
                        task.update(status="interrupted", draft_id="")
                        tx.run("MATCH (c:KDConversation {id:$id}) SET c.task=$task,c.draft_id=''", id=row["id"], task=encoded(task)).consume()
                for row in tx.run("MATCH (c:KDConversation {dataset:$d}) WHERE c.execution IS NOT NULL RETURN c.id AS id,c.execution AS execution", d=self.dataset):
                    state = json.loads(row["execution"])
                    pending = state.get("pending") or {}
                    if pending.get("draft_id"):
                        sources = pending.get("source_message_ids", [])
                        state.update(pending={"status":"interrupted", "draft_id":"", "source_message_ids":sources} if sources else None,
                                     approval=None, version=state["version"]+1)
                        tx.run("MATCH (c:KDConversation {id:$id}) SET c.execution=$state", id=row["id"], state=encoded(state)).consume()
                tx.run("MATCH (p:KDDraft {dataset:$d,status:'committed',projection_status:'ready'}) SET p.projection_status='pending',p.attempts=0 REMOVE p.retry_at", d=self.dataset).consume()
        self.write(migrate)

    def fact(self, tx, fid):
        row = tx.run("MATCH (f:KDFact {dataset:$d,id:$id}) RETURN properties(f) AS f", d=self.dataset, id=fid).single()
        require(row, "fact_not_found", "信息不存在或不属于当前数据范围。", 404)
        return dict(row["f"])

    @staticmethod
    def entity_view(obj):
        return {**obj, "review_status": "confirmed" if obj["confirmed"] else "pending", "archived": obj.get("archived", False)}

    def fact_view(self, tx, fact):
        entities = {oid: self.object(tx, oid) for oid in {fact["subject_id"], fact.get("object_id")} if oid}
        status, reasons = fact_status(fact, entities)
        value = json.loads(fact["value_json"]) if fact["value_json"] else {
            "object_id": fact["object_id"], "value": entities[fact["object_id"]]["name"] or entities[fact["object_id"]]["identifier"]}
        return {**fact, "value": value, "review_status": fact.get("review_status", "confirmed" if fact["confirmed"] else "pending"),
                "effective_status": status, "blocked_reasons": reasons,
                "numeric_precise": fact.get("numeric_precise", fact["confirmed"]),
                "reason": "；".join(reasons) if reasons else ""}

    def refresh_effective_facts(self, tx):
        # 'confirmed' remains the indexed, derived eligibility flag used by all
        # existing deterministic queries, never the fact's own review decision.
        tx.run("MATCH (f:KDFact {dataset:$d}) MATCH (s:KDObject {id:f.subject_id,dataset:$d}) "
               "OPTIONAL MATCH (t:KDObject {id:f.object_id,dataset:$d}) "
               "SET f.confirmed=f.active AND f.review_status='confirmed' AND s.confirmed AND NOT coalesce(s.archived,false) "
               "AND (f.object_id='' OR (t IS NOT NULL AND t.confirmed AND NOT coalesce(t.archived,false))) "
               "AND (f.value_type<>'number' OR coalesce(f.numeric_precise,false))", d=self.dataset).consume()

    def prepare_review_operations(self, tx, operations, source_text=None, overrides=None):
        entities, facts, seen, groups = {}, {}, set(), {}
        for op in operations:
            slot = (op["target_type"], op["target_id"])
            require((*slot, op["action"]) not in seen, "duplicate_operation", "同一目标的操作不能重复。")
            seen.add((*slot, op["action"]))
            groups.setdefault(slot, set()).add(op["action"])
            require(not ({"archive", "restore"} <= groups[slot] or {"confirm", "retract"} <= groups[slot]
                         or {"archive", "confirm"} <= groups[slot]), "conflicting_operations", "同一目标的操作互相矛盾。")
            if source_text is not None:
                self.validate_quotes(op["sources"], source_text)
            if op["target_type"] == "entity":
                entities[op["target_id"]] = self.object(tx, op["target_id"])
            else:
                f = self.fact(tx, op["target_id"])
                facts[f["id"]] = f
                for oid in [f["subject_id"], f.get("object_id")]:
                    if oid:
                        entities[oid] = self.object(tx, oid)
            if op['action']=='confirm':
                self.validate_review_source(tx,op['target_type'],op['target_id'])
        # Include every directly affected fact, not just a relevance sample.
        for row in tx.run("MATCH (f:KDFact {dataset:$d}) WHERE f.subject_id IN $ids OR f.object_id IN $ids RETURN properties(f) AS f",
                          d=self.dataset, ids=list(entities)):
            f = dict(row["f"])
            facts[f["id"]] = f
        for f in facts.values():
            for oid in [f["subject_id"], f.get("object_id")]:
                if oid and oid not in entities:
                    entities[oid] = self.object(tx, oid)
        virtual_entities, virtual_facts = deepcopy(entities), deepcopy(facts)
        for oid, values in (overrides or {}).items():
            if oid in virtual_entities:
                virtual_entities[oid].update(values)
        prepared = []
        priority = {"reclassify": 0, "restore": 1, "confirm": 2, "archive": 3, "retract": 4, "restate": 5}
        for op in sorted(operations, key=lambda item: (priority[item["action"]], item["target_type"])):
            before = (entities if op["target_type"] == "entity" else facts)[op["target_id"]]
            after = (virtual_entities if op["target_type"] == "entity" else virtual_facts)[op["target_id"]]
            action = op["action"]
            if op["target_type"] == "entity":
                if action == "confirm":
                    require(not after.get("archived", False), "object_archived", "对象已归档，请先恢复对象。", 409)
                    require(not after["confirmed"], "already_confirmed", "该对象已经确认，无需重复确认。", 409)
                    after.update(confirmed=True, review_status="confirmed", reason="")
                elif action in {"archive", "restore"}:
                    require(after.get("archived", False) != (action == "archive"), "invalid_transition", "对象状态已变化，请刷新。", 409)
                    after["archived"] = action == "archive"
                else:
                    require(not after.get("archived", False), "object_archived", "归档对象需要先恢复，才能更正类型。", 409)
                    require(after["kind"] != op["new_kind"], "unchanged_type", "类型相同，无需更正。")
                    require("kind" not in (overrides or {}).get(op["target_id"], {}), "conflicting_updates", "类型更正不能重复提出。")
                    after["kind"] = op["new_kind"]
                    after["identity_key"] = digest([key(after["kind"]), key(after["namespace"]), key(after["identifier"])]) if after["identifier"] else "new:" + after["id"]
                    require(not tx.run("MATCH (o:KDObject {dataset:$d,identity_key:$k}) WHERE o.id<>$id RETURN o.id AS id",
                                       d=self.dataset, k=after["identity_key"], id=after["id"]).single(), "duplicate_identity", "更正后的标识与已有对象冲突。")
            elif action == "retract":
                require(after["active"], "fact_closed", "此信息已经失效。", 409)
                after.update(active=False, retracted_by="this_change")
            elif action == "confirm":
                require(after["active"], "fact_closed", "不能确认已经撤回或被更正的信息。", 409)
                require(after.get("review_status") != "confirmed", "already_confirmed", "内容已经确认；请查看依赖阻塞原因。", 409)
                if 'pending_supersedes' in after:
                    olds=after['pending_supersedes']
                    actual={f['id'] for f in virtual_facts.values() if f['id']!=after['id'] and f['subject_id']==after['subject_id'] and f['predicate']==after['predicate'] and f['active'] and f.get('review_status')=='confirmed'}
                    require(after.get('cardinality','one')=='many' or actual==set(olds),'slot_changed','单值当前版本已变化，请重新核对更正范围。',409)
                    for fid in olds:
                        if fid in virtual_facts:
                            virtual_facts[fid].update(active=False,replaced_by=after['id'])
                after["review_status"] = "confirmed"
            else:
                require(not after["active"] and after.get("retracted_by") and not after.get("replaced_by"),
                        "not_retracted", "只能重新登记已撤回信息；已被更正的信息需要明确更正当前值。", 409)
                require(not tx.run("MATCH (f:KDFact {dataset:$d,active:true,restated_from:$id}) RETURN f.id AS id LIMIT 1",
                                   d=self.dataset, id=before["id"]).single(), "already_restated", "此信息已有重新登记的当前版本，请操作该版本。", 409)
                new_id = str(uuid4())
                after = {**after, "id": new_id, "active": True, "restated_from": before["id"]}
                after.pop("retracted_by", None)
                after.pop("ended_at", None)
                virtual_facts[new_id] = after
            prepared.append({**op, "before": deepcopy(before), "after": deepcopy(after), "snapshot_hash": digest(before)})
        # A confirmed single-value assertion cannot silently create a second
        # current value, even if it is temporarily blocked by an endpoint.
        slots = {}
        for f in virtual_facts.values():
            if f["active"] and f.get("review_status") == "confirmed":
                slot = (f["subject_id"], f["predicate"])
                slots.setdefault(slot, []).append(f)
        touched = {(op["after"]["subject_id"], op["after"]["predicate"]) for op in prepared
                   if op["target_type"] == "fact" and op["action"] in {"confirm", "restate"}}
        for slot, fs in slots.items():
            if slot not in touched:
                continue
            require(len(fs) <= 1 or all(f.get("cardinality", "one") == "many" for f in fs),
                    "fact_conflict", "确认或重新登记会产生多个单值记录，请先明确撤回或更正冲突项。", 409)
        impact = []
        for fid, after in virtual_facts.items():
            original = facts.get(fid)
            prior = fact_status(original, entities) if original else ("absent", [])
            following = fact_status(after, virtual_entities)
            if prior != following:
                subject = virtual_entities[after["subject_id"]]
                target = virtual_entities.get(after.get("object_id"))
                impact.append({"fact_id": fid, "subject_id": subject["id"], "subject": subject["name"] or subject["identifier"],
                    "predicate": after["predicate"], "value": (target["name"] or target["identifier"]) if target else json.loads(after["value_json"])["value"],
                    "before": prior[0], "after": following[0], "reasons": following[1]})
        return prepared, impact, list(entities.values())

    def verify_review_snapshots(self, tx, payload):
        for op in payload.get("reviews", []):
            current = self.object(tx, op["target_id"]) if op["target_type"] == "entity" else self.fact(tx, op["target_id"])
            require(digest(current) == op["snapshot_hash"], "review_target_changed", "操作目标已变化，请重新生成预览。", 409)

    def apply_reviews(self, tx, payload, draft_id, actor):
        now = time.time()
        for op in payload.get("reviews", []):
            params = {"d": self.dataset, "id": op["target_id"], "now": now, "actor": actor, "draft": draft_id, "reason": op["reason"]}
            if op["target_type"] == "entity":
                values = {k: op["after"][k] for k in ("confirmed", "review_status", "archived", "kind", "identity_key", "reason") if k in op["after"]}
                if op["action"] in {"archive", "restore"}:
                    values.update(lifecycle_reason=op["reason"], lifecycle_at=now)
                tx.run("MATCH (o:KDObject {dataset:$d,id:$id}) SET o += $values,o.reviewed_by=$actor,o.updated_at=$now",
                       **params, values=values).consume()
            elif op["action"] == "confirm":
                tx.run('MATCH (f:KDFact {dataset:$d}) WHERE f.id IN $ids SET f.active=false,f.replaced_by=$new,f.ended_at=$now',d=self.dataset,ids=op['after'].get('pending_supersedes',[]),new=op['target_id'],now=now).consume()
                tx.run("MATCH (f:KDFact {dataset:$d,id:$id}) SET f.review_status='confirmed',f.reviewed_at=$now,f.reviewed_by=$actor", **params).consume()
            elif op["action"] == "retract":
                tx.run("MATCH (f:KDFact {dataset:$d,id:$id}) SET f.active=false,f.ended_at=$now,f.retracted_by=$draft,f.retraction_reason=$reason", **params).consume()
            else:
                values = {**op["after"], "created_at": now, "confirmed": False}
                tx.run("CREATE (n:KDFact) SET n=$values WITH n MATCH (o:KDObject {dataset:$d,id:n.subject_id}) MERGE (o)-[:KD_HAS_FACT]->(n) "
                       "WITH n OPTIONAL MATCH (t:KDObject {dataset:$d,id:n.object_id}) FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | MERGE (n)-[:KD_TARGET]->(t))",
                       d=self.dataset, values=values).consume()
                tx.run("MATCH (old:KDFact {id:$id,dataset:$d})<-[e:KD_SUPPORTS]-(s:KDSource), (n:KDFact {id:$new,dataset:$d}) "
                       "MERGE (s)-[copy:KD_SUPPORTS]->(n) SET copy.quote=e.quote,copy.quotes=e.quotes", **params, new=values["id"]).consume()
        if payload.get("action_event_id"):
            tx.run("MATCH (e:KDActionEvent {id:$id,dataset:$d}) SET e.status='applied',e.applied_at=$now,e.draft_id=$draft",
                   id=payload["action_event_id"], d=self.dataset, now=now, draft=draft_id).consume()
        self.refresh_effective_facts(tx)

    def prepare_button_review(self, request, scope):
        scope.check(self, "propose")
        require(all(not op.sources for op in request.operations), "invalid_action_evidence", "页面操作不能伪造聊天来源。")
        def write(tx):
            revision = self.lock(tx, request.revision)
            reviews, impact, entities = self.prepare_review_operations(tx, [o.model_dump() for o in request.operations])
            event_id, draft_id, now = str(uuid4()), str(uuid4()), time.time()
            event = {"id": event_id, "actor": scope.actor, "session_id": scope.session_id,
                     "operations": [o.model_dump(exclude={"sources"}) for o in request.operations], "revision": revision, "created_at": now}
            payload = {"objects": [], "facts": [], "entity_updates": [], "retractions": [], "reviews": reviews,
                       "review_objects": entities, "review_impact": impact, "action_event_id": event_id, "action_event": event,
                       "message_id": "", "source": "用户通过页面发起操作；原记录内容和来源保留。", "source_messages": [],
                       "origin": "button", "change": {"schema_version": 3}}
            tx.run("CREATE (:KDActionEvent {id:$id,dataset:$d,session_id:$s,actor:$actor,status:'prepared',event:$event,created_at:$now,draft_id:$draft})",
                   id=event_id, d=self.dataset, s=scope.session_id, actor=scope.actor, event=encoded(event), now=now, draft=draft_id).consume()
            tx.run("CREATE (:KDDraft {id:$id,dataset:$d,revision:$revision,payload:$payload,status:'pending',runtime_version:2,owner_session:$s,owner_actor:$actor,created_at:$now,expires_at:$expires})",
                   id=draft_id, d=self.dataset, revision=revision, payload=encoded(payload), s=scope.session_id, actor=scope.actor, now=now, expires=now + 1800).consume()
            return {**payload, "draft_id": draft_id, "revision": revision, "expires_at": now + 1800, "runtime_version": 2, "status": "pending"}
        return self.write(write)

    def projection_snapshot(self, payload):
        """Current canonical data and a fingerprint; old episode text fails closed."""
        def read(tx):
            self.lock(tx)
            ids = list(dict.fromkeys(o["id"] for o in [*payload.get("objects", []), *payload.get("review_objects", [])]))
            objects = {oid: self.object(tx, oid) for oid in ids}
            facts = [dict(r["f"]) for r in tx.run("MATCH (f:KDFact {dataset:$d,active:true,confirmed:true}) WHERE f.subject_id IN $ids OR f.object_id IN $ids RETURN properties(f) AS f ORDER BY f.id", d=self.dataset, ids=ids)]
            for f in facts:
                for oid in [f["subject_id"], f.get("object_id")]:
                    if oid and oid not in objects:
                        objects[oid] = self.object(tx, oid)
            objects = {oid: o for oid, o in objects.items() if o["confirmed"] and not o.get("archived", False)}
            label = lambda o: o["name"] or o["identifier"]
            body = {"objects": [{"canonical_id": o["id"], "display_label": label(o), "name": o["name"], "identifier": o["identifier"], "kind": o["kind"]} for o in sorted(objects.values(), key=lambda o:o["id"])],
                    "facts": [{"canonical_id": f["id"], "subject": label(objects[f["subject_id"]]), "predicate": f["predicate"], "value": label(objects[f["object_id"]]) if f.get("object_id") else json.loads(f["value_json"])} for f in facts]}
            return {"body": body, "hash": digest(body)}
        return self.read(read)
