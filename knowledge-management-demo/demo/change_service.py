"""Translate semantic proposals; enforce scope, structure, sources and versions.

The model chooses meanings and targets. This service never infers them by
matching a user's wording, a name, a role, or a business-domain predicate.
"""
from .model import Change, ReviewOperation, require
from .unit_labels import unit_label_matches


class ChangeService:
    def __init__(self, store):
        self.store = store

    def prepare_change(self, proposal, deps):
        deps.scope.check(self.store, "propose")
        ids = proposal.source_message_ids
        task_sources = {deps.current_message["id"]}
        if proposal.task_relation == "continue_draft":
            pending = deps.state.get("pending")
            require(pending, "no_active_task", "当前没有未完成草稿；请按新变更处理，不恢复已完成任务。")
            task_sources.update(pending.get("source_message_ids", []))
        require(deps.current_message["id"] in ids and set(ids) <= set(deps.allowed_source_ids),
                "invalid_task_sources", "变更必须包含本轮消息，且只能使用当前执行允许的用户来源。")
        sources = self.store.task_sources(ids, deps.scope.session_id)
        require(set(ids) <= task_sources, "closed_task_sources", "新变更只能引用本轮用户原文；continue_draft 才能引用当前未完成任务的来源。已结束任务的原文仅供上下文理解，不能重新执行。")
        texts = {s["id"]: s["text"] for s in sources}
        objects, updates, normalized_reviews = [], [], []
        for item in proposal.entities:
            for assignment in item.assignments:
                self.store.validate_quotes([q.model_dump() for q in assignment.sources], texts)
            if item.sources:
                self.store.validate_quotes([q.model_dump() for q in item.sources], texts)
            if item.action == "create":
                values = {a.field: a.value for a in item.assignments}
                require(values.get("name") or values.get("identifier"), "missing_identity", "新对象缺少可供确认的名称或编号。")
                objects.append({"ref": item.ref, "name": values.get("name", ""), "identifier": values.get("identifier", ""),
                                "namespace": values.get("namespace", ""), "kind": item.kind, "resolution": "new"})
                continue
            require(item.entity_id in deps.entities_seen, "entity_not_inspected", "请先通过 get_entity 核验已有对象及当前事实，再引用或更正它。")
            snapshot = deps.entities_seen[item.entity_id]
            obj = snapshot["object"]
            objects.append({"ref": item.ref, "existing_id": obj["id"], "name": obj.get("name", ""),
                            "kind": obj["kind"], "identifier": obj.get("identifier", ""), "namespace": obj.get("namespace", ""),
                            "certain": obj.get("semantic_certain",obj["confirmed"]), "reason": obj.get("reason", "") or ("身份尚待核实" if not obj.get("semantic_certain",obj["confirmed"]) else ""), "resolution": "existing"})
            require(item.action in {"update", "reclassify"} or not item.assignments,
                    "invalid_entity_update", "引用已有对象不能静默更改名称或编号；请明确提供 update 操作。")
            for assignment in item.assignments:
                if obj.get(assignment.field, "") != assignment.value:
                    updates.append({"ref": item.ref, "field": assignment.field, "previous": obj.get(assignment.field, ""),
                                    "value": assignment.value, "reason": item.reason or "用户补充或更正对象字段",
                                    "sources": [q.model_dump() for q in assignment.sources]})
            if item.action == "reclassify":
                require(item.kind != obj["kind"], "unchanged_type", "目标类型与当前类型相同，不需要类型更正；如仅补充关系请使用 reference。")
                normalized_reviews.append(ReviewOperation(target_type="entity", target_id=obj["id"], action="reclassify",
                    new_kind=item.kind, classification_error=item.classification_error, reason=item.reason, sources=item.sources))
            else:
                require(not item.kind or item.kind == obj["kind"], "type_change_requires_operation", "类型变更必须使用显式 reclassify 操作，不能通过补充关系隐式改类型。")
        for fact in proposal.facts:
            self.store.validate_quotes([q.model_dump() for q in fact.sources], texts)
            require(fact.evidence == fact.sources[0].quote, "invalid_evidence", "事实 evidence 必须等于第一条来源摘录。")
            if fact.value and fact.value.type == "number" and fact.value.unit:
                require(fact.unit_source is not None, "unit_evidence_required", "带单位的精确数值必须提供用户明确该单位的 unit_source 原文。只有数值、语言习惯或领域常识时不得推断单位，可澄清或保留待确认文本。")
                self.store.validate_quotes([fact.unit_source.model_dump()], texts)
                require(unit_label_matches(fact.unit_source.quote, fact.value.unit), "unit_label_mismatch",
                        "unit_source.quote 必须只摘录明确的单位标签，并与 value.unit 一致。币种名按 CLDR 标准规范化；其他单位保留原文标签。数字、量级或整句话不能作为某币种标签，没有标签则澄清或保留待确认文本。")
                require(any(s.message_id == fact.unit_source.message_id and fact.unit_source.quote in s.quote for s in fact.sources),
                        "invalid_unit_source", "单位来源必须包含在此事实的支持来源中。")
        for retract in proposal.retractions:
            self.store.validate_quotes([q.model_dump() for q in retract.sources], texts)
        reviews = [*proposal.reviews, *normalized_reviews]
        for op in reviews:
            self.store.validate_quotes([q.model_dump() for q in op.sources], texts)
            inspected = op.target_id in deps.entities_seen if op.target_type == "entity" else any(
                any(f["id"] == op.target_id for f in snapshot["rows"]) for snapshot in deps.entities_seen.values())
            require(inspected, "review_target_not_inspected", "请先核验该对象或信息的当前内容及状态，再提出操作。")
        change = Change(schema_version=3, objects=objects, facts=proposal.facts, entity_updates=updates,
                        retractions=[r.model_dump() for r in proposal.retractions], reviews=reviews)
        require(change.facts or change.entity_updates or change.retractions or change.reviews or any(o["existing_id"] == "" for o in change.model_dump()["objects"]),
                "empty_change", "当前提案没有实际新增或修改。")
        draft = self.store.prepare_execution_change(change.model_dump(), deps.current_message["id"], ids,
                                                     deps.scope, deps.state["data_revision"])
        deps.drafts[draft["draft_id"]] = draft
        return {"status": "ready", "draft_id": draft["draft_id"], "revision": draft["revision"],
                "objects": draft["objects"], "facts": draft["facts"], "entity_updates": draft["entity_updates"], "retractions": draft["retractions"],
                "reviews": draft["reviews"], "review_impact": draft["review_impact"]}

    def resolve_draft(self, draft_id, choices, deps):
        deps.scope.check(self.store, "propose")
        draft = self.store.draft(draft_id)
        require(draft.get("runtime_version") == 2 and draft["status"] == "pending", "draft_closed", "只能更新尚未确认的新流程预览。", 409)
        require(isinstance(choices, dict) and all(isinstance(k, str) and isinstance(v, str) and 0 < len(v) <= 100 for k, v in choices.items()), "invalid_choices", "身份选择格式无效。")
        require(not draft.get("entity_updates") and not draft.get("retractions") and not draft.get("reviews"), "resolve_requires_proposal", "含更正或状态操作的预览请通过对话重新生成。", 409)
        require(draft.get("owner_session") == deps.scope.session_id and draft.get("owner_actor") == deps.scope.actor,
                "scope_denied", "预览不属于当前执行者。", 403)
        # Choices select explicit identities; producing the new proposal remains
        # the model's job. Existing schema/evidence/version checks still apply.
        require(set(choices) <= {o["ref"] for o in draft["objects"]}, "invalid_choices", "身份选择引用不存在。")
        change = draft["change"]
        for obj in change["objects"]:
            choice = choices.get(obj["ref"])
            if choice == "new":
                obj.update(resolution="new", existing_id="")
            elif choice:
                selected = self.store.detail(choice)["object"]
                obj.update(existing_id=choice, resolution="existing", kind=selected["kind"],
                           name=selected.get("name", ""), identifier=selected.get("identifier", ""), namespace=selected.get("namespace", ""))
        updated = self.store.prepare_execution_change(change, draft["message_id"], [s["id"] for s in draft["source_messages"]],
                                                     deps.scope, deps.state["data_revision"], bulk=draft.get("bulk"))
        deps.drafts[updated["draft_id"]] = updated
        self.store.close_execution_draft(deps.scope, draft_id)
        return updated
