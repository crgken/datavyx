"""Explicit v2 mutations; no approval or generic execution fields."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .contracts import (StrictModel, Identifier, RequestId, Source, Quote, Value,
                        NumberValue, Cursor, TargetSelection, OperationReceipt, PageResult)


class V2(StrictModel):
    schema_version: Literal['2.0'] = '2.0'
    workspace_id: Identifier | None = None


class Mutation(V2):
    request_id: RequestId
    sources: list[Source] = Field(min_length=1, max_length=8)
    instruction_quotes: list[Quote] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def received_evidence(self):
        sources={s.ref:s.text for s in self.sources}
        if len(sources)!=len(self.sources) or sum(len(t) for t in sources.values())>16000:
            raise ValueError('Source refs must be unique and total text at most 16000 characters.')
        quotes=list(self.instruction_quotes)
        for action in getattr(self,'changes',[]):
            quotes.extend(action.quotes)
            for replacement in getattr(action,'replacements',[]):
                quotes.extend(replacement.quotes)
                if isinstance(replacement.value,NumberValue):quotes.extend(replacement.value.unit_quotes)
        if any(q.source_ref not in sources or q.quote not in sources[q.source_ref] for q in quotes):
            raise ValueError('All evidence must be verbatim received source text.')
        return self


class Replacement(StrictModel):
    value: Value | None = None
    object_id: Identifier | None = None
    inherit_unit: bool = False
    quotes: list[Quote] = Field(min_length=1,max_length=8)

    @model_validator(mode='after')
    def exactly_one(self):
        if (self.value is None)==(self.object_id is None):raise ValueError('Exactly one value or object_id is required.')
        if self.inherit_unit and not (isinstance(self.value,NumberValue) and self.value.unit=='' and not self.value.unit_quotes):
            raise ValueError('Inherited units require a number with empty unit and no new unit quotes.')
        return self


class FactChange(StrictModel):
    action: Literal['replace_fact','replace_slot','replace_relation']
    entity_id: Identifier
    expected_version: int = Field(ge=1)
    predicate: str = Field(min_length=1,max_length=100)
    fact_ids: list[Identifier] = Field(min_length=1,max_length=100)
    replacements: list[Replacement] = Field(min_length=1,max_length=20)
    quotes: list[Quote] = Field(min_length=1,max_length=8)

    @model_validator(mode='after')
    def targets(self):
        if len(set(self.fact_ids))!=len(self.fact_ids):raise ValueError('Fact IDs must be unique.')
        if self.action!='replace_slot' and (len(self.fact_ids)!=1 or len(self.replacements)!=1):
            raise ValueError('Single fact/relation replacement needs one target and one replacement.')
        if self.action=='replace_relation' and self.replacements[0].object_id is None:
            raise ValueError('Relation replacement requires an existing object ID.')
        return self


class EntityFields(StrictModel):
    name: str | None = Field(default=None,max_length=200)
    identifier: str | None = Field(default=None,max_length=200)
    namespace: str | None = Field(default=None,max_length=200)
    aliases: list[Annotated[str,Field(min_length=1,max_length=200)]] | None = Field(default=None,max_length=20)

    @model_validator(mode='after')
    def nonempty(self):
        if not self.model_fields_set or any(getattr(self,k) is None for k in self.model_fields_set):
            raise ValueError('At least one non-null explicit field is required.')
        return self


class EntityChange(StrictModel):
    action: Literal['set_entity_fields']
    entity_id: Identifier
    expected_version: int = Field(ge=1)
    fields: EntityFields
    quotes: list[Quote] = Field(min_length=1,max_length=8)


Change=Annotated[FactChange | EntityChange,Field(discriminator='action')]


class UpdateInput(Mutation):
    changes: list[Change] = Field(min_length=1,max_length=50)


class SelectionMutation(Mutation):
    selection: TargetSelection
    expected_revision: int = Field(ge=0)
    expected_count: int = Field(ge=0,le=1000)
    selection_digest: str = Field(pattern='^[0-9a-f]{64}$')


class RestoreInput(Mutation):
    delete_receipt_id: Identifier
    expected_revision: int = Field(ge=0)


class HistoryInput(V2):
    entity_id: Identifier | None = None
    source_id: Identifier | None = None
    operation_id: Identifier | None = None
    limit: int = Field(default=25,ge=1,le=100)
    cursor: Cursor | None = None

    @model_validator(mode='after')
    def one_target(self):
        if sum(x is not None for x in (self.entity_id,self.source_id,self.operation_id))!=1:
            raise ValueError('Exactly one history target is required.')
        return self


class HistoryResult(PageResult):
    schema_version: Literal['2.0'] = '2.0'


MUTATION_INPUTS={'update_information':UpdateInput,'delete_information':SelectionMutation,
                 'restore_information':RestoreInput,'purge_information':SelectionMutation,
                 'get_history':HistoryInput}
MUTATION_OUTPUTS={name:HistoryResult if name=='get_history' else OperationReceipt for name in MUTATION_INPUTS}
