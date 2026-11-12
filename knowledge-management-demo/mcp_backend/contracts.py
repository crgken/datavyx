"""Version 1 tool contracts. Business vocabulary is data, never executable code."""
from __future__ import annotations
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
Ref = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]
RequestId = Annotated[str, Field(min_length=8, max_length=100)]
Cursor = Annotated[str, Field(min_length=1, max_length=4096)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_default=True)


class Versioned(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    workspace_id: Identifier | None = None


class Quote(StrictModel):
    source_ref: Ref
    quote: str = Field(min_length=1, max_length=16000)


class Source(StrictModel):
    ref: Ref
    text: str = Field(min_length=1, max_length=16000)
    external_ref: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def not_blank(self):
        if not self.text.strip():
            raise ValueError("Source text must not be blank.")
        return self


class TextValue(StrictModel):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=16000)


class NumberValue(StrictModel):
    type: Literal["number"]
    decimal: str = Field(max_length=36, pattern=r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$")
    unit: str = Field(max_length=40, description='Explicit unit; use an ISO currency code for a known currency. No inferred currency or conversion.')
    unit_quotes: list[Quote] = Field(default_factory=list, max_length=8,
        description='Quote literal unit tokens only, e.g. 人民币 for CNY. Unique CLDR Chinese/English currency names are supported; bare 元/$/¥ do not establish an ISO currency. Include the shared denomination statement in each affected fact.quotes as well as its transaction quote.')


Value = Annotated[TextValue | NumberValue, Field(discriminator="type")]


class EntityCreate(StrictModel):
    ref: Ref
    action: Literal["create"]
    kind: str = Field(min_length=1, max_length=100)
    name: str = Field(default="", max_length=200)
    identifier: str = Field(default="", max_length=200)
    namespace: str = Field(default="", max_length=200)
    quotes: list[Quote] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def identity(self):
        if not self.kind.strip() or not (self.name.strip() or self.identifier.strip()):
            raise ValueError("A new entity needs a kind and name or identifier.")
        return self


class EntityReference(StrictModel):
    ref: Ref
    action: Literal["reference"]
    entity_id: Identifier


Entity = Annotated[EntityCreate | EntityReference, Field(discriminator="action")]


class Fact(StrictModel):
    subject_ref: Ref
    predicate: str = Field(min_length=1, max_length=100)
    value: Value | None = None
    object_ref: Ref | None = None
    cardinality: Literal["one", "many"] = "one"
    certainty: Literal["asserted", "estimated", "uncertain"] = "asserted"
    quotes: list[Quote] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def exactly_one_value(self):
        if (self.value is None) == (self.object_ref is None) or not self.predicate.strip():
            raise ValueError("A fact requires a predicate and exactly one value or object_ref.")
        return self


class RecordInput(Versioned):
    request_id: RequestId
    recording_mode: Literal["structured", "source_only"] = "structured"
    sources: list[Source] = Field(min_length=1, max_length=8)
    entities: list[Entity] = Field(default_factory=list, max_length=40)
    facts: list[Fact] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def references(self):
        source_refs = {s.ref for s in self.sources}
        refs = {e.ref for e in self.entities}
        if len(source_refs) != len(self.sources) or len(refs) != len(self.entities):
            raise ValueError("Source and entity refs must each be unique.")
        if sum(len(s.text) for s in self.sources) > 16000:
            raise ValueError("Combined source text exceeds 16000 characters.")
        if self.recording_mode == "source_only":
            if self.entities or self.facts:
                raise ValueError("source_only cannot contain entities or facts.")
        elif not (self.entities or self.facts):
            raise ValueError("A structured record needs entities or facts.")
        quotes = [q for e in self.entities if isinstance(e, EntityCreate) for q in e.quotes]
        for fact in self.facts:
            if fact.subject_ref not in refs or fact.object_ref and fact.object_ref not in refs:
                raise ValueError("Fact refs must identify an entity in this request.")
            quotes.extend(fact.quotes)
            if isinstance(fact.value, NumberValue):
                quotes.extend(fact.value.unit_quotes)
        if any(q.source_ref not in source_refs for q in quotes):
            raise ValueError("Quotes must identify a source in this request.")
        return self


class PageInput(Versioned):
    limit: int = Field(default=25, ge=1, le=100)
    cursor: Cursor | None = None


class ContextInput(PageInput):
    directory: Literal["types", "predicates", "identity_rules"] = "types"
    kind: str = Field(default="", max_length=100)
    predicate: str = Field(default="", max_length=100)


class FindInput(PageInput):
    text: str = Field(default="", max_length=200)
    kind: str = Field(default="", max_length=100)
    identifier: str = Field(default="", max_length=200)
    namespace: str = Field(default="", max_length=200)
    record_type: Literal["entity", "source", "all"] = "entity"
    selection: 'TargetSelection | None' = None


class GetInput(Versioned):
    entity_id: Identifier
    limit: int = Field(default=25, ge=1, le=100)
    facts_cursor: Cursor | None = None
    relations_cursor: Cursor | None = None
    include_history: bool = False


class SourcesInput(PageInput):
    source_id: Identifier | None = None
    fact_id: Identifier | None = None
    include_history: bool = False

    @model_validator(mode="after")
    def one_target(self):
        if (self.source_id is None) == (self.fact_id is None):
            raise ValueError("Specify exactly one source_id or fact_id.")
        return self


class Filter(StrictModel):
    predicate: str = Field(min_length=1, max_length=100)
    operator: Literal["eq", "in", "exists", "gt", "gte", "lt", "lte"]
    value: Value | None = None
    values: list[Value] = Field(default_factory=list, max_length=100)
    object_id: Identifier | None = None
    direction: Literal["out", "in"] = "out"

    @model_validator(mode="after")
    def operand(self):
        supplied = sum((self.value is not None, bool(self.values), self.object_id is not None))
        if self.operator == "exists":
            valid = supplied == 0
        elif self.operator == "in":
            valid = bool(self.values) and supplied == 1
        else:
            valid = supplied == 1 and not self.values
        if self.operator in {"gt", "gte", "lt", "lte"}:
            valid = valid and isinstance(self.value, NumberValue)
        if not valid or not self.predicate.strip():
            raise ValueError("Filter operator and operands are incompatible.")
        return self


class QueryInput(PageInput):
    operation: Literal["LIST", "COUNT", "GROUP", "SUM"]
    kind: str = Field(default="", max_length=100)
    object_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    filters: list[Filter] = Field(default_factory=list, max_length=8)
    predicate: str = Field(default="", max_length=100)
    unit: str | None = Field(default=None, max_length=40)
    direction: Literal["out", "in"] = "out"

    @model_validator(mode="after")
    def aggregation(self):
        if self.operation in {"GROUP", "SUM"} and not self.predicate.strip():
            raise ValueError("Aggregation requires predicate.")
        if self.operation == "SUM" and (self.unit is None or self.direction != "out"):
            raise ValueError("SUM requires an explicit unit and outgoing attribute.")
        return self


class TargetSelection(StrictModel):
    resource: Literal['entity', 'fact', 'source', 'workspace']
    ids: list[Identifier] = Field(default_factory=list, max_length=1000)
    kind: str = Field(default='', max_length=100)
    namespace: str = Field(default='', max_length=200)
    identifier: str = Field(default='', max_length=200)
    subject_id: Identifier | None = None
    predicate: str = Field(default='', max_length=100)
    filters: list[Filter] = Field(default_factory=list, max_length=8)
    state: Literal['present', 'deleted'] = 'present'

    @model_validator(mode='after')
    def compatible(self):
        if len(set(self.ids)) != len(self.ids):raise ValueError('Target IDs must be unique.')
        if self.resource != 'entity' and (self.kind or self.namespace or self.identifier or self.filters):
            raise ValueError('Entity filters require entity selection.')
        if self.resource != 'fact' and (self.subject_id or self.predicate):
            raise ValueError('Fact filters require fact selection.')
        if self.resource == 'workspace' and (self.ids or self.state != 'present'):
            raise ValueError('Workspace selection covers all present canonical resources.')
        return self


class ReceiptInput(Versioned):
    request_id: RequestId
    include_history: bool = False


class RecordReceipt(StrictModel):
    status: Literal["recorded"] = "recorded"
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    request_id: RequestId
    receipt_id: Identifier
    revision: int = Field(ge=0)
    entity_ids: dict[str, str]
    source_ids: dict[str, str]
    fact_ids: list[str]
    objects_created: int = Field(ge=0)
    facts_created: int = Field(ge=0)
    sources_recorded: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    uncertain_count: int = Field(ge=0)
    idempotent: bool = False
    current_state: Literal['current','changed','deleted'] = 'current'


class PageResult(StrictModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    revision: int = Field(ge=0)
    rows: list[dict]
    total: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    has_more: bool
    next_cursor: Cursor | None
    complete: bool


class ContextResult(PageResult):
    workspace_id: Identifier
    profile: dict
    permissions: list[str]
    catalog_revision: int = Field(ge=0)
    directory: Literal['types', 'predicates', 'identity_rules']


class FindResult(PageResult):
    search_mode: Literal['field_match', 'literal_substring']
    selection_digest: str | None = None
    selection_count: int | None = None
    selection_revision: int | None = None
    resource_counts: dict[str, int] | None = None


class RecordResult(StrictModel):
    schema_version: Literal['1.0'] = SCHEMA_VERSION
    object: dict
    facts: PageResult
    relations: PageResult


class QueryResult(PageResult):
    operation: Literal['LIST', 'COUNT', 'GROUP', 'SUM']
    count_unit: Literal['entity'] = 'entity'
    population_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    participating_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    exclusions: list[dict]
    aggregate_complete: bool = True
    count: int | None = None
    sum: str | None = None
    unit: str | None = None


class ReceiptResult(StrictModel):
    found: bool
    receipt: RecordReceipt | OperationReceipt | PurgedReceipt | None = None


class OperationReceipt(StrictModel):
    schema_version: Literal['2.0'] = '2.0'
    request_id: RequestId
    receipt_id: Identifier
    operation: Literal['update', 'delete', 'restore', 'purge']
    status: Literal['applied', 'noop']
    revision_before: int = Field(ge=0)
    revision: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    applied_count: int = Field(ge=0)
    objects_changed: int = Field(ge=0)
    facts_changed: int = Field(ge=0)
    sources_changed: int = Field(ge=0)
    sources_recorded: int = Field(default=0,ge=0)
    relations_hidden: int = Field(ge=0)
    recoverable: bool
    selection_complete: Literal[True] = True
    idempotent: bool = False
    current_state: Literal['current','changed','restored'] = 'current'


class PurgedReceipt(StrictModel):
    schema_version: Literal['2.0'] = '2.0'
    status: Literal['purged'] = 'purged'
    receipt_id: Identifier


FindInput.model_rebuild()
ReceiptResult.model_rebuild()


TOOL_INPUTS = {
    "get_context": ContextInput, "find_records": FindInput,
    "get_record": GetInput, "query_records": QueryInput,
    "get_sources": SourcesInput, "record_information": RecordInput,
    "get_receipt": ReceiptInput,
}

TOOL_OUTPUTS = {
    'get_context': ContextResult, 'find_records': FindResult,
    'get_record': RecordResult, 'query_records': QueryResult,
    'get_sources': PageResult, 'record_information': RecordReceipt,
    'get_receipt': ReceiptResult,
}
