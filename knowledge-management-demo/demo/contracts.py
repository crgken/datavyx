"""Semantic proposals and application-owned execution contracts.

Business vocabulary remains data. Nothing here matches a particular utterance,
person, entity type, or field name from a business domain.
"""
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .model import FactInput, ReviewOperation, SourceQuote, StrictModel, require


@dataclass(frozen=True)
class ExecutionScope:
    dataset: str
    session_id: str = "default"
    actor: str = "local-user"
    permissions: frozenset[str] = frozenset({"read", "propose", "record_pending", "confirm"})
    language: str = "zh"

    def check(self, store, permission):
        require(self.dataset == store.dataset, "scope_denied", "请求不属于当前数据范围。", 403)
        require(permission in self.permissions, "permission_denied", "当前操作没有相应权限。", 403)


class FieldAssignment(StrictModel):
    field: Literal["name", "identifier", "namespace"]
    value: str = Field(min_length=1, max_length=200)
    sources: list[SourceQuote] = Field(min_length=1, max_length=8)


class EntityProposal(StrictModel):
    ref: str = Field(min_length=1, max_length=100)
    action: Literal["create", "reference", "update", "reclassify"]
    entity_id: str = Field(default="", max_length=100)
    kind: str = Field(default="", max_length=100, description="Open entity type, distinct from a business role. For references reuse the actual current type.")
    assignments: list[FieldAssignment] = Field(default_factory=list, max_length=3)
    classification_error: bool = Field(default=False, description="True only when the current entity type is actually wrong, not merely when adding a role or relation.")
    reason: str = Field(default="", max_length=1000)
    sources: list[SourceQuote] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def validate_action(self):
        require((self.action == "create") != bool(self.entity_id), "invalid_entity_action", "创建对象不带已有ID；引用或修改对象必须带已有ID。")
        require(self.action != "create" or bool(self.kind), "missing_type", "新对象需要明确的类型。")
        require(self.action != "reclassify" or (self.classification_error and bool(self.kind) and bool(self.reason) and bool(self.sources)),
                "missing_type_correction", "类型更正必须明确指出实际分类错误、目标类型和来源。")
        fields = [a.field for a in self.assignments]
        require(len(fields) == len(set(fields)), "duplicate_assignment", "名称、编号、范围分别提供，不能重复赋值。")
        return self


class RetractionProposal(StrictModel):
    fact_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)
    sources: list[SourceQuote] = Field(min_length=1, max_length=8)


class ChangeProposal(StrictModel):
    task_relation: Literal["continue_draft", "new_change"]
    entities: list[EntityProposal] = Field(default_factory=list, max_length=40)
    facts: list[FactInput] = Field(default_factory=list, max_length=100)
    retractions: list[RetractionProposal] = Field(default_factory=list, max_length=40)
    reviews: list[ReviewOperation] = Field(default_factory=list, max_length=40)
    source_message_ids: list[str] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def nonempty(self):
        require(self.entities or self.facts or self.retractions or self.reviews, "empty_change", "提案至少包含一项实际操作。")
        return self


class SelectionRef(StrictModel):
    target_type: Literal["entity", "fact"]
    target_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=0, strict=True)


class ReviewRequest(StrictModel):
    operations: list[ReviewOperation] = Field(min_length=1, max_length=40)
    revision: int = Field(ge=0, strict=True)
    replace_draft_id: str = Field(default="", max_length=100)


class SemanticQuestion(StrictModel):
    target_ref: str = Field(default="", max_length=100)
    facet: Literal["identity", "field", "unit", "scope", "conflict", "goal"]
    field: str = Field(default="", max_length=100)
    question: str = Field(min_length=1, max_length=1500)
    blocking_reason: str = Field(min_length=1, max_length=1000, description="Why the missing information prevents the user's current operation. Optional enrichment is not a reason to block.")


class QuestionReview(StrictModel):
    keep_indices: list[int] = Field(default_factory=list, max_length=5)
    reason: str = Field(min_length=1, max_length=1500)


class TurnOutput(StrictModel):
    action: Literal["answer", "clarify", "preview", "query", "browse", "cancel"]
    task_relation: Literal["continue_draft", "new_change", "detour", "none"] = "none"
    message: str = Field(default="", max_length=6000)
    questions: list[SemanticQuestion] = Field(default_factory=list, max_length=5)
    source_message_ids: list[str] = Field(default_factory=list, max_length=24)
    draft_id: str = Field(default="", max_length=100)
    query_id: str = Field(default="", max_length=100)
    entity_id: str = Field(default="", max_length=100)

    @model_validator(mode="after")
    def check_output(self):
        require(self.action != "clarify" or bool(self.questions), "missing_questions", "澄清必须提供实际需要用户回答的问题。")
        require(self.action != "query" or bool(self.query_id), "missing_query", "查询回答必须引用本次执行的查询。")
        require(self.action != "preview" or bool(self.draft_id), "missing_preview", "预览必须引用本次生成的草稿。")
        return self


class EntityLookup(StrictModel):
    text: str = Field(default="", max_length=200)
    identifier: str = Field(default="", max_length=200)
    namespace: str = Field(default="", max_length=200)
    kind: str = Field(default="", max_length=100)
    limit: int = Field(default=15, ge=1, le=50)


class MemoryPort(Protocol):
    async def retrieve_context(self, query: str, limit: int = 8) -> dict: ...


@dataclass
class RunDependencies:
    scope: ExecutionScope
    store: object
    memory: MemoryPort
    changes: object
    current_message: dict
    state: dict
    allowed_source_ids: list[str]
    query_results: dict = field(default_factory=dict)
    drafts: dict = field(default_factory=dict)
    entities_seen: dict = field(default_factory=dict)
    tool_log: list = field(default_factory=list)
    approval_id: str = ""
    approved_draft_id: str = ""
    commit_result: dict | None = None
