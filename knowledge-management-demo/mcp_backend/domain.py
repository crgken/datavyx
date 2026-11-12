"""Pure canonical rules; never imports legacy code or model clients."""
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
import unicodedata
from .contracts import EntityCreate, NumberValue
from .errors import require


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def key(value):
    return unicodedata.normalize('NFC',value.strip()).casefold()


def identity_key(kind,namespace,identifier):
    return digest([key(kind),key(namespace),key(identifier)]) if identifier else ''


def number_fields(value):
    number=Decimal(value.decimal)
    require(number.is_finite() and abs(number)<=Decimal('1000000000'),
            'INVALID_ARGUMENT','A number exceeds the supported exact range.')
    scaled=number*1_000_000
    require(scaled==scaled.to_integral_value(),'INVALID_ARGUMENT','At most six decimal places are supported.')
    canonical='0' if number.is_zero() else format(number.normalize(),'f')
    return {'type':'number','value':canonical,'unit':value.unit},int(scaled)


def validate_record(data):
    """Validate verbatim evidence and deterministically represent number values."""
    sources={s.ref:s.text for s in data.sources}
    def quotes(values):
        for q in values:
            require(q.quote in sources[q.source_ref], 'INVALID_ARGUMENT',
                    'An evidence quote is not a verbatim substring of the received source.',source_ref=q.source_ref)
    for entity in data.entities:
        if isinstance(entity,EntityCreate):quotes(entity.quotes)
    for fact in data.facts:
        quotes(fact.quotes)
        if isinstance(fact.value,NumberValue):
            number_fields(fact.value)
            quotes(fact.value.unit_quotes)
            if fact.value.unit:
                require(bool(fact.value.unit_quotes),'INVALID_ARGUMENT','A numeric unit requires source evidence.')
                # Unit names are open vocabulary; no currency is inferred from
                # geography. Exact canonical labels must be present in evidence.
                require(any(key(fact.value.unit) == key(q.quote) for q in fact.value.unit_quotes),
                        'INVALID_ARGUMENT','The explicit unit label must appear in its quoted evidence.')
                require(all(any(q.source_ref==s.source_ref and q.quote in s.quote for s in fact.quotes)
                            for q in fact.value.unit_quotes),'INVALID_ARGUMENT','Unit evidence must support this fact.')


def literal_fields(value):
    if isinstance(value,NumberValue):return number_fields(value)
    return {'type':'text','value':value.text,'unit':''},None


def certainty(fact):
    if fact.get('certainty') in {'asserted','estimated','uncertain'}:return fact['certainty']
    if fact.get('semantic_certain') is False or fact.get('numeric_precise') is False:return 'uncertain'
    if fact.get('recording_policy_version',0)<2 and fact.get('review_status','confirmed' if fact.get('confirmed',True) else 'pending')!='confirmed':return 'uncertain'
    return 'asserted'


def available(obj):
    return not obj.get('archived',False) and obj.get('record_state','active')!='action_draft'


def fact_value(fact):
    if fact.get('object_id'):return {'type':'entity','object_id':fact['object_id']}
    return json.loads(fact.get('value_json') or '{}')


def value_key(fact):
    value=fact_value(fact)
    if value.get('type')=='number':
        try:value={**value,'value':format(Decimal(str(value['value'])).normalize(),'f')}
        except Exception:return digest(value)
    return digest(value)


def eligibility(objects,facts):
    """Single-valued conflicting assertions exclude the slot, not the entity."""
    slots=defaultdict(list)
    reasons={}
    for fact in facts:
        reasons[fact['id']]=[]
        if not fact.get('active',True):reasons[fact['id']].append('historical')
        if fact.get('record_state')=='action_draft':reasons[fact['id']].append('unexecuted_proposal')
        if not all(oid in objects and available(objects[oid]) for oid in (fact['subject_id'],fact.get('object_id')) if oid):
            reasons[fact['id']].append('unavailable_entity')
        if any(oid in objects and not objects[oid].get('confirmed',True) for oid in (fact['subject_id'],fact.get('object_id')) if oid):
            reasons[fact['id']].append('uncertain_identity')
        if certainty(fact)!='asserted':reasons[fact['id']].append('uncertain')
        if not any(r in reasons[fact['id']] for r in ('historical','unexecuted_proposal','unavailable_entity','uncertain_identity')):
            slots[(fact['subject_id'],fact['predicate'])].append(fact)
    for group in slots.values():
        if any(f.get('cardinality','one')=='one' for f in group) and len({value_key(f) for f in group})>1:
            for fact in group:reasons[fact['id']].append('conflict')
    return reasons
