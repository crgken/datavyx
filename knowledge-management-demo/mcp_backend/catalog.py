"""Observed graph vocabulary and deterministic constraints, without graph content."""
from collections import defaultdict
from .domain import available, digest, fact_value
from .paging import Pager
from .lifecycle import present,current_fact


def vocabulary(objects, facts):
    types = defaultdict(lambda: {'total': 0, 'active': 0})
    identities = set()
    predicates = defaultdict(lambda: {'cardinalities': set(), 'units': set(), 'total': 0})
    for obj in objects.values():
        if not present(obj):continue
        kind = obj.get('kind', '')
        types[kind]['total'] += 1
        types[kind]['active'] += int(available(obj))
        if obj.get('identifier'):
            identities.add((kind, obj.get('namespace', '')))
    for fact in facts:
        if not current_fact(fact,objects):continue
        subject = objects.get(fact['subject_id'])
        if not subject:
            continue
        target = objects.get(fact.get('object_id'))
        value = fact_value(fact)
        signature = (subject.get('kind', ''), fact['predicate'],
                     'entity' if fact.get('object_id') else value.get('type', 'unknown'),
                     target.get('kind', '') if target else '')
        entry = predicates[signature]
        entry['cardinalities'].add(fact.get('cardinality', 'one'))
        if value.get('type') == 'number':
            entry['units'].add(value.get('unit', ''))
        entry['total'] += 1
    return {
        'types': [{'kind': kind, **counts} for kind, counts in sorted(types.items())],
        'identity_rules': [{'kind': kind, 'namespace': namespace,
            'identity_fields': ['kind', 'namespace', 'identifier'], 'normalization': 'NFC-strip-casefold-v1',
            'name_is_unique': False} for kind, namespace in sorted(identities)],
        'predicates': [{'subject_kind': kind, 'predicate': predicate, 'value_type': typ,
            'object_kind': target, 'direction': 'out', 'cardinalities': sorted(entry['cardinalities']),
            'units': sorted(entry['units']), 'observed_fact_count': entry['total']}
            for (kind, predicate, typ, target), entry in sorted(predicates.items())],
    }


class Catalog:
    def __init__(self, repository):
        self.repo = repository
        self.pager = Pager(repository.settings.cursor_secret)

    def get_context(self, scope, data):
        def read(tx, revision):
            permissions = self.repo.authorize(tx, scope)
            objects, facts = self.repo.snapshot(tx, scope)
            catalog = vocabulary(objects, facts)
            # Vocabulary version is independent of repeated source-only additions.
            shape = {k: [{field: val for field, val in row.items()
                          if field not in {'total', 'active', 'observed_fact_count'}} for row in rows]
                     for k, rows in catalog.items()}
            version = int(digest(shape)[:13], 16)
            rows = [row for row in catalog[data.directory]
                    if (not data.kind or row.get('kind', row.get('subject_kind')) == data.kind)
                    and (not data.predicate or row.get('predicate') == data.predicate)]
            return {**self.pager.page(rows, scope, revision, data, 'context', data.cursor),
                    'workspace_id': scope.workspace_id, 'catalog_revision': version, 'directory': data.directory,
                    'permissions': sorted(permissions), 'profile': {
                        'actor_id': scope.actor, 'identity_normalization': 'NFC-strip-casefold-v1',
                        'name_is_unique': False, 'new_vocabulary': 'allowed_with_source',
                        'numeric_scale': 6, 'max_number_absolute': '1000000000',
                        'recording_available': 'record' in permissions and (self.repo.settings.mode == 'test'
                            or self.repo.settings.recording_enabled or scope.actor in self.repo.settings.recording_actors),
                        'unit_rule': 'explicit_source_quote_cldr_currency_v1', 'scope': 'authorized_workspace',
                        'supported_operations': ['record', 'query']}}
        return self.repo.scoped(scope, read)
