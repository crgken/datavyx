"""Deterministic legacy audit and additive fixed-point backfill, with no model use."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import json
from ..domain import digest, eligibility, encoded, fact_value
from ..errors import KnowledgeError, require


def _inspect(tx, repo, dataset):
    require(isinstance(dataset, str) and 0 < len(dataset) <= 200, 'INVALID_ARGUMENT', 'An exact dataset is required.')
    nodes = {}
    for label in ('KDObject', 'KDFact', 'KDSource', 'KDWorkspaceMember'):
        values = [dict(r['n']) for r in tx.run(f'MATCH (n:{label} {{dataset:$d}}) '
            'RETURN properties(n) AS n ORDER BY n.id LIMIT $n', d=dataset, n=repo.settings.max_records+1)]
        require(len(values) <= repo.settings.max_records, 'LIMIT_EXCEEDED', 'The compatibility audit exceeds its resource limit.', 413)
        nodes[label] = values
    supports = [dict(r) for r in tx.run('MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(n) '
        'RETURN s.id AS source,n.id AS target,n.dataset AS target_dataset,properties(e) AS properties '
        'ORDER BY source,target', d=dataset)]
    supports.sort(key=encoded)
    objects = {o['id']: o for o in nodes['KDObject']}
    sources = {s['id']: s for s in nodes['KDSource']}
    facts = nodes['KDFact']
    blockers = Counter()
    warnings = Counter()
    fixes = []
    evidence = defaultdict(list)
    for support in supports:
        if support['target_dataset'] != dataset:
            blockers['cross_dataset_support'] += 1
        source = sources[support['source']]
        quotes = support['properties'].get('quotes') or ([support['properties']['quote']] if support['properties'].get('quote') else [])
        if source.get('valid', True) and any(isinstance(q, str) and q and q in source.get('message', '') for q in quotes):
            evidence[support['target']].append(support['source'])
    for obj in objects.values():
        if not obj.get('kind') or not obj.get('id'): blockers['invalid_object_shape'] += 1
        if obj.get('confirmed') is False and obj.get('recording_policy_version', 0) < 2 and obj.get('record_state') != 'action_draft':
            blockers['unclassified_legacy_identity'] += 1
    cardinalities = defaultdict(set)
    valid_facts = []
    for fact in facts:
        subject = objects.get(fact.get('subject_id'))
        if not subject or fact.get('object_id') and fact['object_id'] not in objects:
            blockers['orphan_fact'] += 1
            continue
        if not fact.get('predicate') or not fact.get('id') or fact.get('cardinality', 'one') not in {'one', 'many'}:
            blockers['invalid_fact_shape'] += 1
            continue
        if (fact.get('recording_policy_version', 0) < 2 and fact.get('confirmed') is False
                and fact.get('record_state') != 'action_draft'):
            blockers['unclassified_legacy_statement'] += 1
        if subject and fact.get('record_state') != 'action_draft':
            cardinalities[(subject.get('kind'), fact.get('predicate'))].add(fact.get('cardinality', 'one'))
        try:
            value = fact_value(fact)
            if not fact.get('object_id'):
                require(value.get('type') in {'text', 'number', 'boolean'} and 'value' in value,
                        'INVALID_LEGACY_VALUE', 'An unsupported historical value was found.')
            if value.get('type') == 'number':
                number = Decimal(str(value['value']))
                scaled = number * 1_000_000
                if not number.is_finite() or abs(number) > 1_000_000_000 or scaled != scaled.to_integral_value():
                    warnings['legacy_imprecise_number'] += 1
                elif fact.get('number_scaled') is None:
                    fixes.append({'id': fact['id'], 'scaled': int(scaled)})
                elif type(fact['number_scaled']) is not int or fact['number_scaled'] != int(scaled):
                    blockers['numeric_storage_mismatch'] += 1
            valid_facts.append(fact)
        except (ValueError, TypeError, KeyError, InvalidOperation):
            blockers['invalid_literal'] += 1
        except KnowledgeError:
            blockers['unsupported_literal'] += 1
    blockers['incompatible_predicate_cardinality'] = sum(len(v) > 1 for v in cardinalities.values())
    reasons = eligibility(objects, valid_facts)
    for fact in valid_facts:
        if not reasons[fact['id']] and not evidence[fact['id']]:
            blockers['eligible_fact_without_verbatim_evidence'] += 1
    for member in nodes['KDWorkspaceMember']:
        row = tx.run('MATCH (w:KDWorkspace {id:$id}) RETURN w.dataset AS d', id=member.get('workspace_id')).single()
        if not row or row['d'] != dataset: blockers['workspace_binding_mismatch'] += 1
    preserved = {**nodes, 'KDFact': [{k: v for k, v in f.items() if k != 'number_scaled'} for f in facts],
                 'supports': supports}
    report = {'migration_version': 1, 'counts': {label: len(values) for label, values in nodes.items()},
        'states': {'archived_objects': sum(bool(o.get('archived')) for o in objects.values()),
                   'unexecuted_objects': sum(o.get('record_state') == 'action_draft' for o in objects.values()),
                   'unexecuted_facts': sum(f.get('record_state') == 'action_draft' for f in facts),
                   'inactive_facts': sum(not f.get('active', True) for f in facts)},
        'blockers': {k: v for k, v in sorted(blockers.items()) if v},
        'warnings': dict(sorted(warnings.items())), 'number_backfill_count': len(fixes),
        'fingerprint': digest({'nodes': nodes, 'supports': supports}), 'preserved_fingerprint': digest(preserved),
        'source_fingerprint': digest(nodes['KDSource']),
        'eligible_fact_count': sum(not value for value in reasons.values()),
        'conflict_fact_count': sum('conflict' in value for value in reasons.values())}
    return report, fixes


def audit(repository, dataset):
    def read(tx):
        before = repository.revision(tx, dataset)
        report, _ = _inspect(tx, repository, dataset)
        require(repository.revision(tx, dataset) == before, 'STALE_SNAPSHOT', 'Data changed during the audit. Retry on a stable copy.', 409)
        return {**report, 'revision': before, 'ready': not report['blockers']}
    return repository.transaction(read)


def migrate(repository, dataset, expected_fingerprint):
    def apply(tx):
        revision = repository.revision(tx, dataset, write=True)
        before, fixes = _inspect(tx, repository, dataset)
        require(before['fingerprint'] == expected_fingerprint, 'STALE_SNAPSHOT', 'The audited data changed. Audit again.', 409)
        require(not before['blockers'], 'MIGRATION_BLOCKED', 'Resolve the reported compatibility blockers before migration.', 409)
        tx.run('UNWIND $fixes AS fix MATCH (f:KDFact {dataset:$d,id:fix.id}) '
               'WHERE f.number_scaled IS NULL SET f.number_scaled=fix.scaled', fixes=fixes, d=dataset).consume()
        after, _ = _inspect(tx, repository, dataset)
        require(before['preserved_fingerprint'] == after['preserved_fingerprint'], 'MIGRATION_INTEGRITY_ERROR',
                'Canonical content changed unexpectedly. The migration was rolled back.', 500)
        tx.run('MERGE (m:KDMCPMigration {id:$id}) ON CREATE SET m.dataset=$d,m.version=1,'
               'm.source_fingerprint=$source,m.preserved_fingerprint=$preserved '
               'WITH m MATCH (d:KDData {id:$d}) SET d.revision=$revision',
               id=digest([dataset, 'legacy-v1']), d=dataset, source=before['source_fingerprint'],
               preserved=before['preserved_fingerprint'], revision=revision+bool(fixes)).consume()
        return {'status': 'migrated', 'migration_version': 1, 'number_backfilled': len(fixes),
                'revision': revision+bool(fixes), 'preserved_fingerprint': after['preserved_fingerprint'],
                'source_fingerprint': after['source_fingerprint']}
    return repository.transaction(apply)
