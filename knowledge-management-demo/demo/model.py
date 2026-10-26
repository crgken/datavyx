"""Closed canonical schemas shared by the Agent application and deterministic store.

Adapted from atlasai.knowledge.model's validation and fixed precision approach.
Business types and predicates are data, never Python branches.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DemoError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def require(condition, code, message, status=400):
    if not condition:
        raise DemoError(code, message, status)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def key(value):
    return unicodedata.normalize("NFC", value.strip()).casefold()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Value(StrictModel):
    type: Literal["text", "number", "boolean"]
    value: str = Field(min_length=1, max_length=2000, description="Numbers are decimal strings; booleans use true/false.")
    unit: str = Field(default="", max_length=40, description="Explicit canonical currency/unit, e.g. CNY or USD; never mix currencies.")

    @model_validator(mode="after")
    def validate_value(self):
        if self.type == "number":
            try:
                number = Decimal(self.value)
                require(number.is_finite() and number.copy_abs() <= Decimal("1000000000") and
                        (number.is_zero() or number.copy_abs() >= Decimal("0.000001")),
                        "invalid_number", "数值最多六位小数，单值绝对值不超过十亿。")
                with localcontext() as context:
                    context.prec = max(32, len(number.as_tuple().digits) + 12)
                    scaled = number * 1_000_000
                    require(scaled == scaled.to_integral_value(), "invalid_number", "数值最多六位小数。")
                    self.value = "0" if number.is_zero() else format(number.normalize(), "f")
            except InvalidOperation as exc:
                raise DemoError("invalid_number", "无法识别精确数值。") from exc
        elif self.type == "boolean":
            require(self.value in {"true", "false"} and not self.unit, "invalid_boolean", "布尔值只支持 true/false，不能带单位。")
        else:
            require(not self.unit, "invalid_unit", "文本不使用数值单位。")
        return self

    def fields(self):
        data = self.model_dump()
        return {"value_json": encoded(data), "value_key": digest(data), "value_type": self.type,
                "number_scaled": int(Decimal(self.value) * 1_000_000) if self.type == "number" else None,
                "unit": self.unit, "object_id": ""}


class ObjectInput(StrictModel):
    ref: str = Field(min_length=1, max_length=100)
    name: str = Field(default="", max_length=200, description="Only an explicitly supplied name; never copy an identifier into this field.")
    kind: str = Field(min_length=1, max_length=100, description="Open business type, in user's language; reuse existing vocabulary exactly.")
    identifier: str = Field(default="", max_length=200, description="Explicit business identifier; a bare descriptive name is not proof of identity.")
    namespace: str = Field(default="", max_length=200, description="Identifier scope, e.g. owning company. Do not invent scope.")
    existing_id: str = Field(default="", max_length=100, description="Proposed existing canonical ID from supplied context only.")
    resolution: Literal["", "new", "existing"] = ""
    certain: bool = True
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def uncertainty_reason(self):
        require(self.certain or bool(self.reason), "uncertainty_reason_required", "待确认对象必须说明身份或类型不明确的原因。")
        return self


class SourceQuote(StrictModel):
    message_id: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=16000)


class FactInput(StrictModel):
    subject: str = Field(min_length=1, max_length=100)
    predicate: str = Field(min_length=1, max_length=100)
    value: Value | None = None
    object: str = Field(default="", max_length=100)
    certain: bool = True
    reason: str = Field(default="", max_length=500)
    evidence: str = Field(min_length=1, max_length=2000, description="An exact substring from the user's source message; no fabricated quotes.")
    sources: list[SourceQuote] = Field(default_factory=list, max_length=8, description="Original user messages supporting this fact, including separate amount/currency clarifications.")
    unit_source: SourceQuote | None = Field(default=None, description="For a numeric value with a unit, quote ONLY the literal unit label from the user, without the amount or surrounding sentence. Currency names normalize via CLDR; other units preserve their source label. Never infer a currency from language or magnitude.")
    mode: Literal["assert", "replace"] = "assert"
    cardinality: Literal["one", "many"] = "one"

    @model_validator(mode="after")
    def check_target(self):
        require(bool(self.object) != (self.value is not None), "invalid_fact", "事实必须有且只有一个属性值或关联对象。")
        require(self.certain or bool(self.reason), "uncertainty_reason_required", "不确定的事实必须说明原因。")
        require(self.cardinality == "one" or bool(self.object), "invalid_cardinality", "多值关系必须关联对象；数值属性使用单一当前值。")
        return self


class EntityUpdate(StrictModel):
    ref: str = Field(min_length=1, max_length=100)
    field: Literal["name", "identifier", "namespace", "kind"]
    previous: str = Field(max_length=200)
    value: str = Field(max_length=200)
    reason: str = Field(min_length=1, max_length=1000)
    sources: list[SourceQuote] = Field(min_length=1, max_length=8)


class FactRetraction(StrictModel):
    fact_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)
    sources: list[SourceQuote] = Field(min_length=1, max_length=8)


class ReviewOperation(StrictModel):
    target_type: Literal["entity", "fact"]
    target_id: str = Field(min_length=1, max_length=100)
    action: Literal["confirm", "archive", "restore", "retract", "restate", "reclassify"] = Field(description="Operate on existing content. Fact confirm only verifies the same predicate, direction and value; a more specific/new relationship belongs in facts, never only in reason.")
    reason: str = Field(min_length=1, max_length=1000)
    sources: list[SourceQuote] = Field(default_factory=list, max_length=8)
    new_kind: str = Field(default="", max_length=100)
    classification_error: bool = False

    @model_validator(mode="after")
    def valid_operation(self):
        allowed = {"entity": {"confirm", "archive", "restore", "reclassify"}, "fact": {"confirm", "retract", "restate"}}
        require(self.action in allowed[self.target_type], "invalid_review_action", "操作与目标类型不匹配。")
        require(self.action != "reclassify" or (self.new_kind and self.classification_error),
                "missing_type_correction", "更正类型必须明确实际分类错误及目标类型。")
        require(self.action == "reclassify" or not self.new_kind and not self.classification_error,
                "unexpected_type_change", "确认、归档或关系操作不能隐式更正类型。")
        return self


class Change(StrictModel):
    schema_version: Literal[1, 2, 3] = 1
    objects: list[ObjectInput] = Field(default_factory=list, max_length=40)
    facts: list[FactInput] = Field(default_factory=list, max_length=100)
    entity_updates: list[EntityUpdate] = Field(default_factory=list, max_length=40)
    retractions: list[FactRetraction] = Field(default_factory=list, max_length=40)
    reviews: list[ReviewOperation] = Field(default_factory=list, max_length=40)
    clarification_required: bool = False
    questions: list[str] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def validate_references(self):
        refs = {obj.ref for obj in self.objects}
        require(len(refs) == len(self.objects), "duplicate_ref", "对象 ref 必须唯一。")
        require(all(fact.subject in refs and (not fact.object or fact.object in refs) for fact in self.facts),
                "unknown_reference", "facts.subject/object 只能引用 objects[].ref；已有对象也必须列在 objects 中，不能直接把 UUID 或对象名称当作 ref。")
        require(all(update.ref in refs for update in self.entity_updates), "unknown_reference", "对象修改必须引用本次预览中的对象。")
        slots = [(u.ref, u.field) for u in self.entity_updates]
        require(len(slots) == len(set(slots)), "conflicting_updates", "一个对象字段只能有一个本次修改。")
        return self


class Filter(StrictModel):
    predicate: str = Field(min_length=1, max_length=100)
    op: Literal["eq", "ne", "exists"] = "eq"
    value: Value | None = None
    object_id: str = Field(default="", max_length=100)
    direction: Literal["out", "in"] = "out"

    @model_validator(mode="after")
    def check_target(self):
        if self.op != "exists":
            require(bool(self.object_id) != (self.value is not None), "invalid_filter", "筛选条件必须指定一个值或对象。")
        require(self.direction == "out" or self.op == "exists" or bool(self.object_id), "invalid_filter", "入向关系筛选必须指定关联对象。")
        return self


class Query(StrictModel):
    operation: Literal["COUNT", "LIST", "GROUP", "SUM"]
    kind: str = Field(default="", max_length=100)
    object_ids: list[str] = Field(default_factory=list, max_length=100)
    filters: list[Filter] = Field(default_factory=list, max_length=8)
    predicate: str = Field(default="", max_length=100)
    unit: str | None = Field(default=None, max_length=40)
    direction: Literal["out", "in"] = "out"
    offset: int = Field(default=0, ge=0, le=10**8)
    limit: int = Field(default=25, ge=1, le=100)
    revision: int | None = Field(default=None, ge=0, description="Subsequent pages must reuse the returned data revision.")

    @model_validator(mode="after")
    def check_operation(self):
        if self.operation in {"GROUP", "SUM"}:
            require(bool(self.predicate), "predicate_required", "请明确分组或求和的属性。")
        if self.operation == "SUM":
            require(self.unit is not None, "unit_required", "求和必须指定币种或单位；无量纲使用空字符串。")
            require(self.direction == "out", "invalid_direction", "SUM 只汇总对象自身的数值属性。")
        return self


class QueryAnswer(StrictModel):
    answer: str = Field(min_length=1, max_length=6000, description="A natural-language answer grounded only in the supplied query result.")


class RecordQuery(StrictModel):
    kind: str = Field(default='', max_length=100)
    text: str = Field(default='', max_length=200)
    status: Literal['all','confirmed','pending'] = 'all'
    object_ids: list[str] = Field(default_factory=list,max_length=100)
    filters: list[Filter] = Field(default_factory=list,max_length=8)
    offset: int = Field(default=0,ge=0,le=10**8)
    limit: int = Field(default=25,ge=1,le=100)
    revision: int | None = Field(default=None,ge=0)
