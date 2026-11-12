"""Bounded fixed query operations; no generated expressions, floats or model calls."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import json
from .domain import available, eligibility, encoded, fact_value, literal_fields, value_key
from .errors import require
from .paging import Pager
from .records import public_object


def _matches(fact, condition):
    if condition.operator == 'exists':
        return True
    if condition.object_id is not None:
        return fact.get('object_id' if condition.direction == 'out' else 'subject_id') == condition.object_id
    if condition.direction == 'in':
        return False
    actual = fact_value(fact)
    requested = condition.values if condition.operator == 'in' else [condition.value]
    for value in requested:
        expected, _ = literal_fields(value)
        if actual.get('type') != expected['type'] or actual.get('unit', '') != expected.get('unit', ''):
            continue
        if actual['type'] == 'number':
            try:
                left, right = Decimal(str(actual['value'])), Decimal(expected['value'])
                if not left.is_finite():
                    continue
            except (InvalidOperation, KeyError):
                continue
        else:
            left, right = actual.get('value'), expected['value']
        if condition.operator in {'eq', 'in'} and left == right:
            return True
        if condition.operator == 'gt' and left > right: return True
        if condition.operator == 'gte' and left >= right: return True
        if condition.operator == 'lt' and left < right: return True
        if condition.operator == 'lte' and left <= right: return True
    return False


class Queries:
    def __init__(self, repository):
        self.repo = repository
        self.pager = Pager(repository.settings.cursor_secret)

    def query_records(self, scope, data):
        def read(tx, revision):
            objects, facts = self.repo.snapshot(tx, scope)
            reasons = eligibility(objects, facts)
            index = defaultdict(list)
            for fact in facts:
                if any(r in reasons[fact['id']] for r in ('historical', 'unavailable_entity', 'unexecuted_proposal')):
                    continue
                index[(fact['subject_id'], fact['predicate'], 'out')].append(fact)
                if fact.get('object_id'):
                    index[(fact['object_id'], fact['predicate'], 'in')].append(fact)
            population = [o for o in objects.values() if available(o)
                          and (not data.kind or o.get('kind') == data.kind)
                          and (not data.object_ids or o['id'] in data.object_ids)]
            matched = []
            exclusions = {}
            for obj in population:
                excluded = set()
                if not obj.get('confirmed', True): excluded.add('uncertain_identity')
                for condition in data.filters:
                    slot = index[(obj['id'], condition.predicate, condition.direction)]
                    eligible = [f for f in slot if not reasons[f['id']]]
                    if any(_matches(f, condition) for f in eligible):
                        continue
                    if not slot: excluded.add('missing_filter_value')
                    elif not eligible:
                        excluded.update(r for f in slot for r in reasons[f['id']])
                    else: excluded.add('filter_not_matched')
                if excluded: exclusions[obj['id']] = excluded
                else: matched.append(obj)
            participating = []
            chosen = []
            grouping = defaultdict(set)
            for obj in matched:
                if data.operation in {'LIST', 'COUNT'}:
                    participating.append(obj)
                    continue
                slot = index[(obj['id'], data.predicate, data.direction)]
                eligible = [f for f in slot if not reasons[f['id']]]
                selected = {}
                extra = set()
                for fact in eligible:
                    value = fact_value(fact) if data.direction == 'out' else {'type': 'entity', 'object_id': fact['subject_id']}
                    if value.get('type') == 'number':
                        try:
                            number = Decimal(str(value['value']))
                            if not number.is_finite(): raise InvalidOperation()
                            value = {**value, 'value': '0' if number.is_zero() else format(number.normalize(), 'f')}
                        except (InvalidOperation, KeyError, ValueError):
                            extra.add('numeric_unavailable'); continue
                    if data.operation == 'SUM':
                        if value.get('type') != 'number': extra.add('non_numeric'); continue
                        if value.get('unit', '') != data.unit: extra.add('unit_mismatch'); continue
                        try:
                            number = Decimal(str(value['value']))
                            scaled = number * 1_000_000
                            valid = (number.is_finite() and abs(number) <= 1_000_000_000
                                     and scaled == scaled.to_integral_value()
                                     and type(fact.get('number_scaled')) is int and fact['number_scaled'] == int(scaled))
                        except (InvalidOperation, ValueError, KeyError): valid = False
                        if not valid: extra.add('numeric_unavailable'); continue
                    selected.setdefault(encoded(value), (fact, value))
                if not selected:
                    exclusions[obj['id']] = (extra or {r for f in slot for r in reasons[f['id']]} or {'missing_value'})
                    continue
                # A many-valued numeric slot with unknown members has an unknown
                # total. Never silently sum only its known same-unit subset.
                qualified_values = {value_key(f) for f, _ in selected.values()}
                blocked = {r for f in slot if value_key(f) not in qualified_values for r in reasons[f['id']]}
                if data.operation in {'SUM', 'GROUP'} and (extra or blocked):
                    exclusions[obj['id']] = extra | blocked
                    continue
                participating.append(obj)
                for fact, value in selected.values():
                    chosen.append(fact)
                    grouping[encoded(value)].add(obj['id'])
            ids = [o['id'] for o in participating]
            count = tx.run('MATCH (o:KDObject {dataset:$d}) WHERE o.id IN $ids '
                           'RETURN count(DISTINCT o.id) AS n', d=scope.dataset, ids=ids).single()['n']
            total = None
            if data.operation == 'SUM':
                # Neo4j sums signed int64. Validate the worst-case magnitude
                # first so even intermediate additions cannot overflow.
                require(sum(abs(f['number_scaled']) for f in chosen) <= 2**63-1,
                        'NUMERIC_OVERFLOW', 'This sum exceeds the exact supported range.', 422)
                scaled = tx.run('MATCH (f:KDFact {dataset:$d}) WHERE f.id IN $ids '
                    'RETURN coalesce(sum(f.number_scaled),0) AS total',
                    d=scope.dataset, ids=[f['id'] for f in chosen]).single()['total']
                # No participating values means unknown/empty, not a known 0.
                total = format(Decimal(scaled) / 1_000_000, 'f') if chosen else None
            if data.operation == 'GROUP':
                groups = [{'key': k, 'ids': sorted(v)} for k, v in sorted(grouping.items())]
                rows = [{'value': json.loads(r['key']), 'count': r['n']}
                        for r in tx.run('UNWIND $groups AS g UNWIND g.ids AS id '
                            'MATCH (o:KDObject {dataset:$d,id:id}) '
                            'RETURN g.key AS key,count(DISTINCT o.id) AS n ORDER BY key',
                            d=scope.dataset, groups=groups)]
            else:
                rows = [public_object(o) for o in sorted(participating, key=lambda o:o['id'])]
            counts = Counter(reason for values in exclusions.values() for reason in values)
            return {**self.pager.page(rows, scope, revision, data, 'query', data.cursor),
                    'operation': data.operation, 'count_unit': 'entity', 'population_count': len(population),
                    'matched_count': len(matched), 'participating_count': count, 'excluded_count': len(exclusions),
                    'exclusions': [{'reason': reason, 'count': n} for reason, n in sorted(counts.items())],
                    'aggregate_complete': True, 'count': count if data.operation == 'COUNT' else None,
                    'sum': total, 'unit': data.unit if data.operation == 'SUM' else None}
        return self.repo.scoped(scope, read)
