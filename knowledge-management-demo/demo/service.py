"""Server-owned state and approvals, model-owned semantic decisions."""
import asyncio
import logging
import re
from uuid import uuid4

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_core import to_jsonable_python
from .agent_runtime import AgentRuntime
from .change_service import ChangeService
from .contracts import ExecutionScope, RunDependencies, ReviewRequest, SelectionRef
from .graphiti_adapter import GraphitiAdapter
from .model import DemoError, require
from .bulk_import import BulkImport, parse_table


def public_text(text, identifiers):
    """Hide application preview/query IDs; keep business names and identifiers."""
    for identifier in identifiers:
        text = re.sub(r"[（(][^()（）\n]*" + re.escape(identifier) + r"[^()（）\n]*[)）]", "", text)
        text = text.replace(identifier, "当前记录")
    return text


def review_receipt(draft):
    labels = {"confirm":"已确认", "archive":"已归档", "restore":"已恢复", "retract":"已撤回", "restate":"已重新登记", "reclassify":"已更正"}
    parts = []
    for op in draft["reviews"]:
        before = op["before"]
        if op["target_type"] == "entity":
            name = before["name"] or before["identifier"]
            detail = f"类型：{before['kind']} → {op['after']['kind']}" if op["action"] == "reclassify" else f"当前类型：{op['after']['kind']}"
            parts.append(f"{labels[op['action']]}对象“{name}”（{detail}）。")
        else:
            subject = next(o for o in draft["review_objects"] if o["id"] == before["subject_id"])
            parts.append(f"{labels[op['action']]}“{subject['name'] or subject['identifier']} · {before['predicate']}”这条信息。")
    blocked = [r for r in draft.get("review_impact", []) if r["after"] == "blocked"]
    if blocked:
        reasons = list(dict.fromkeys(reason for r in blocked for reason in r["reasons"]))
        parts.append(f"其中 {len(blocked)} 条信息暂不生效：" + "；".join(reasons[:4]) + ("；其余原因可在对象详情查看。" if len(reasons)>4 else "。"))
    return "\n".join(parts[:8]) + (f"\n另有 {len(parts)-8} 项说明，可在操作历史中核对。" if len(parts)>8 else "")


class Service:
    def __init__(self, store, pipeline, runtime=None, scope=None):
        self.store, self.pipeline = store, pipeline
        self.scope = scope or ExecutionScope(store.dataset)
        self._runtime = runtime
        self.changes = ChangeService(store)
        self.memory = GraphitiAdapter(store, pipeline, self.scope)
        self.chat_lock = asyncio.Lock()
        self.bulk = BulkImport(self)

    @property
    def runtime(self):
        if self._runtime is None:
            self._runtime = AgentRuntime(self.store, self.pipeline)
        return self._runtime

    async def prepare_review(self, body):
        request = ReviewRequest.model_validate(body)
        self.scope.check(self.store, "propose")
        async with self.chat_lock:
            state = self.store.conversation(self.scope)
            require(not (state.get("pending") or {}).get("import_id"), "import_pending", "请先完成或取消当前表格导入任务。", 409)
            previous = (state.get("pending") or {}).get("draft_id", "")
            require(not previous or previous == request.replace_draft_id, "pending_preview", "当前还有待确认预览，请明确取消或替换后再操作。", 409)
            draft = self.store.prepare_button_review(request, self.scope)
            self.close_previous(state, draft["draft_id"])
            approval = self.store.bind_approval(self.scope, draft["draft_id"], str(uuid4()))
            self.store.save_conversation(self.scope, {**state, "pending": {"status": "preview", "draft_id": draft["draft_id"], "source_message_ids": []}, "approval": approval, "clarification": None}, state["version"])
            return {**draft, "approval_id": approval["id"]}

    async def cancel_preview(self, draft_id):
        async with self.chat_lock:
            self.scope.check(self.store, "propose")
            state = self.store.conversation(self.scope)
            require((state.get("pending") or {}).get("draft_id") == draft_id, "preview_changed", "预览已变化，请刷新。", 409)
            self.close_previous(state)
            self.store.save_conversation(self.scope, {**state, "pending": None, "approval": None, "clarification": None}, state["version"])
            return {"clear_draft": True}

    def review_impact(self, draft_id, offset=0, limit=25):
        self.store.page_bounds(offset, limit)
        draft = self.public_draft(draft_id)
        rows = draft.get("review_impact", [])
        return self.store.page_result(rows[offset:offset+limit], len(rows), offset, limit, draft["revision"])

    def dependencies(self, state, current):
        self.scope.check(self.store, "read")
        users = [m["id"] for m in self.store.messages(self.scope.session_id) if m["role"] == "user"]
        pending, clarification = state.get("pending") or {}, state.get("clarification") or {}
        ids = list(dict.fromkeys([*pending.get("source_message_ids", []), *clarification.get("source_message_ids", []), *users[-20:], current["id"]]))
        require(len(ids) <= 24, "task_too_large", "当前任务涉及的原始消息过多，请确认或取消当前任务后再继续。")
        state = {**state, "data_revision": self.store.context()["revision"]}
        if pending.get("draft_id"):
            state["current_draft"] = self.store.draft(pending["draft_id"])
        deps = RunDependencies(self.scope, self.store, self.memory, self.changes, current, state, ids)
        for oid in state.get("focus_refs", [])[:8]:
            deps.entities_seen[oid] = self.store.detail(oid, limit=100, history=True)
        if state.get("selection", {}).get("target_type") == "fact":
            fact = state["selection_record"]
            snapshot = deps.entities_seen[fact["subject_id"]]
            snapshot["rows"] = list({r["id"]:r for r in [*snapshot["rows"], fact]}.values())
        return deps

    def close_previous(self, state, next_draft=""):
        previous = (state.get("pending") or {}).get("draft_id")
        if previous and previous != next_draft:
            self.store.close_execution_draft(self.scope, previous)

    def public_draft(self, draft_id):
        self.scope.check(self.store, "read")
        draft = self.store.draft(draft_id)
        require(draft.get("runtime_version") == 2 and draft.get("owner_session") == self.scope.session_id and draft.get("owner_actor") == self.scope.actor,
                "legacy_preview", "预览来自旧执行流程或其他会话，请重新生成。", 409)
        state = self.store.conversation(self.scope)
        approval = state.get("approval") or {}
        return {**draft, "approval_id": approval.get("id", "") if approval.get("draft_id") == draft_id else ""}

    async def chat(self, text, request_id=None, context_ref=None):
        require(isinstance(text,str) and 0<len(text.strip())<=16000,'invalid_message','消息不能为空，且最多 16000 字。')
        async with self.chat_lock:
            self.scope.check(self.store, "read")
            state = self.store.conversation(self.scope)
            receipt=self.store.pending_receipt(self.scope,request_id,text)
            if receipt:
                return {**receipt,'saved_pending':True,'idempotent':True,'message':
                    ('This information is already saved as unconfirmed. No duplicate was created.' if self.scope.language == 'en' else '这条信息已保存为待确认，没有重复写入。'),'clear_draft':True}
            state.pop("selection", None)
            state.pop("selection_record", None)
            if context_ref is not None:
                ref = SelectionRef.model_validate(context_ref)
                def selected(tx):
                    self.store.lock(tx, ref.revision)
                    return self.store.object(tx, ref.target_id) if ref.target_type == "entity" else self.store.fact_view(tx, self.store.fact(tx, ref.target_id))
                record = self.store.write(selected)
                state["selection"] = ref.model_dump()
                state["selection_record"] = record
                state["focus_refs"] = [record["id"] if ref.target_type == "entity" else record["subject_id"]]
            if request_id and request_id == state.get("request_id") and state.get("last_response"):
                return state["last_response"]
            importing = (state.get("pending") or {}).get("import_id")
            require(not importing, "import_pending", "当前表格任务尚未结束，请等待完成，或在导入进度中重试/取消后再提交。", 409)
            table = parse_table(text) if isinstance(text, str) else None
            if table is not None:
                return await self.bulk.start(text, table, state, request_id)
            if not state["history"]:
                legacy = self.store.messages(self.scope.session_id)
                state["history"] = to_jsonable_python([
                    ModelRequest(parts=[UserPromptPart(content=m["text"])]) if m["role"] == "user"
                    else ModelResponse(parts=[TextPart(content=m["text"])]) for m in legacy])
            mid = self.store.message(text, session_id=self.scope.session_id)
            deps = self.dependencies(state, {"id": mid, "text": text.strip()})
            try:
                output, history = await self.runtime.run(text.strip(), deps)
            except DemoError:
                pending = state.get("pending") or {"status": "interrupted", "draft_id": "", "source_message_ids": []}
                recovery = {**pending, "source_message_ids": list(dict.fromkeys([*pending.get("source_message_ids", []), mid]))}
                self.store.save_conversation(self.scope, {**state, "pending": recovery}, state["version"])
                raise
            updated = {**state, "history": history, "request_id": request_id or str(uuid4()),
                       "last_tool_log": deps.tool_log, "clarification": None}
            if output.action == "clarify":
                questions = [q.question for q in output.questions]
                info = {"questions": [q.model_dump() for q in output.questions], "source_message_ids": output.source_message_ids}
                updated["clarification"] = info
                if output.task_relation != "detour":
                    self.close_previous(state)
                    updated.update(pending={**info, "status": "waiting", "draft_id": ""}, approval=None)
                response = {"clarification_required": True, "questions": questions, "clear_draft": output.task_relation != "detour"}
                answer = "；".join(questions)
            elif output.action == "preview":
                draft = deps.drafts[output.draft_id]
                self.close_previous(state, draft["draft_id"])
                if all(o['resolved'] for o in draft['objects']):
                    saved=self.store.save_pending(draft['draft_id'],self.scope,request_id or '')
                    updated.update(pending=None,approval=None,focus_refs=saved.get('entity_refs',state.get('focus_refs',[])))
                    answer="已自动保存为待确认，可查询并在星状图中查看。核对确认后才正式生效；待确认内容不参与精准统计。" if saved.get('item_count') else "已补充来源，现有记录及其确认状态保持不变，没有重复创建信息。"
                    if self.scope.language == 'en':
                        answer = ('Saved as unconfirmed and available for browsing and search. It is excluded from precise statistics until confirmed and effective.' if saved.get('item_count') else 'The source was added. Existing records and confirmation states were preserved; no duplicate was created.')
                    response={**saved,'saved_pending':True,'message':answer,'clear_draft':True}
                else:
                    updated.update(pending={'status':'preview','draft_id':draft['draft_id'],'source_message_ids':[s['id'] for s in draft['source_messages']]},approval=None)
                    answer="请先选择对象身份；已收集的信息仍保留，选择后自动保存为待确认。"
                    if self.scope.language == 'en':
                        answer = 'Choose the object identities to continue. Collected information is preserved and will be saved as unconfirmed after resolution.'
                    response={'draft':draft,'message':answer}
            elif output.action == "cancel":
                self.scope.check(self.store, "propose")
                self.close_previous(state)
                updated.update(pending=None, approval=None, clarification=None)
                answer = output.message or "已取消当前未完成变更。"
                response = {"message": answer, "clear_draft": True}
            elif output.action == "query":
                answer = output.message
                response = {"message": answer, "result": deps.query_results[output.query_id]}
            elif output.action == "browse":
                answer = output.message or "已打开对象记录。"
                response = {"message": answer}
                if output.entity_id:
                    response["detail"] = self.store.detail(output.entity_id)
                    updated["focus_refs"] = [output.entity_id]
                else:
                    response["types"] = self.store.types()
            else:
                answer = output.message
                response = {"message": answer}
            internal = [*deps.drafts, *deps.query_results]
            previous = (state.get("pending") or {}).get("draft_id")
            if previous:
                internal.append(previous)
            answer = public_text(answer, internal)
            if "message" in response:
                response["message"] = answer
            if any(t.get('tool')=='retrieve_context' and t.get('status')=='partial_error' for t in deps.tool_log):
                response['warning']='部分语义索引暂时不可用，本次回答已通过规范数据工具继续核验；不能将索引失败解释为没有记录。'
            require(bool(answer), "empty_answer", "模型未生成可展示的回答，请重试。", 502)
            self.store.message(answer, session_id=self.scope.session_id, role="assistant")
            updated["last_response"] = response
            self.store.save_conversation(self.scope, updated, state["version"])
            return response

    async def confirm(self, draft_id, approval_id, revision):
        async with self.chat_lock:
            self.scope.check(self.store, "confirm")
            draft = self.store.draft(draft_id)
            require(draft.get("runtime_version") == 2, "legacy_preview", "此预览来自旧执行流程，请重新描述以生成新版预览。", 409)
            approval = self.store.approve(self.scope, draft_id, approval_id, revision)
            state = self.store.conversation(self.scope)
            if draft["status"] == "committed":
                self.store.commit(draft_id, approval_id, self.scope)
                return {"draft_id": draft_id, "idempotent": True, "message": "这条变更已保存，本次确认没有重复写入。"}
            require(state.get("approval") and state["approval"]["id"] == approval_id,
                    "invalid_approval", "只能确认当前会话仍在等待确认的预览。", 403)
            if draft.get("origin") in {"button", "bulk"}:
                result = self.store.commit(draft_id, approval_id, self.scope)
                if draft.get("bulk"):
                    info = draft["bulk"]
                    answer = f"整表 {info['row_count']} 行已统一保存：{len(draft['objects'])} 个对象、{info['relation_count']} 条关系、{info['value_count']} 条属性；已有对象已复用。"
                else:
                    answer = review_receipt(draft)
                latest = self.store.conversation(self.scope)
                # Application event, not a fabricated user utterance/source.
                context = draft["source"] if draft.get("bulk") else "应用操作事件：" + str(draft["action_event"])
                history = [*state["history"], to_jsonable_python(ModelRequest(parts=[UserPromptPart(content=context)])), to_jsonable_python(ModelResponse(parts=[TextPart(content=answer)]))]
                self.store.message(answer, session_id=self.scope.session_id, role="assistant")
                result = {**result, "message": answer}
                self.store.save_conversation(self.scope, {**latest, "history": history, "pending": None, "approval": None, "clarification": None, "last_response": result}, state["version"])
                return result
            current = self.store.task_sources([draft["message_id"]], self.scope.session_id)[0]
            deps = self.dependencies(state, {"id": current["id"], "text": current["text"]})
            deps.approval_id, deps.approved_draft_id = approval_id, draft_id
            warning = None
            try:
                output, history = await self.runtime.confirm(deps, {**approval, "draft_id": draft_id})
                answer = public_text(output.message, [draft_id])
            except Exception as exc:
                if deps.commit_result is None:
                    if isinstance(exc, DemoError):
                        raise
                    raise DemoError("confirmation_execution_failed", "确认执行失败，请重试；可再次提交同一确认以核对结果。", 502) from exc
                logging.getLogger("demo").error("confirmation_receipt_failed type=%s", type(exc).__name__)
                answer, history = "变更已保存。", state["history"]
                warning = "业务变更已保存，但模型未能生成说明。可查看对象和来源核对。"
            latest = self.store.conversation(self.scope)
            updated = {**latest, "history": history, "approval": None, "pending": None,
                       "clarification": None, "last_tool_log": deps.tool_log}
            result = {**deps.commit_result, "message": answer, "warning": warning}
            self.store.message(answer, session_id=self.scope.session_id, role="assistant")
            updated["last_response"] = result
            self.store.save_conversation(self.scope, updated, state["version"])
            return result

    async def resolve(self, draft_id, choices):
        async with self.chat_lock:
            state = self.store.conversation(self.scope)
            draft = self.store.draft(draft_id)
            deps = self.dependencies(state, {"id": draft["message_id"], "text": draft["source"]})
            updated_draft = self.changes.resolve_draft(draft_id, choices, deps)
            if all(o['resolved'] for o in updated_draft['objects']):
                result=self.store.save_pending(updated_draft['draft_id'],self.scope)
                self.store.save_conversation(self.scope,{**state,'pending':None,'approval':None,'clarification':None,'focus_refs':result.get('entity_refs',[])},state['version'])
                return {**result,'saved_pending':True,'message':'对象身份已明确，信息已保存为待确认。'}
            approval = self.store.bind_approval(self.scope, updated_draft["draft_id"], str(uuid4()))
            self.store.save_conversation(self.scope, {**state, "pending": {"status": "preview", "draft_id": updated_draft["draft_id"],
                "source_message_ids": [s["id"] for s in updated_draft["source_messages"]]}, "approval": approval}, state["version"])
            return {**updated_draft, "approval_id": approval["id"]}
