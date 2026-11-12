"""Scoped candidate search and one-hop records with exact received evidence."""
from .domain import available, certainty, eligibility, fact_value, key
from .errors import require
from .paging import Pager


def public_object(obj):
    return {field: obj.get(field, '') for field in ('id', 'kind', 'name', 'identifier', 'namespace')} | {
        'identity_status': 'asserted' if obj.get('confirmed', True) else 'uncertain',
        'archived': obj.get('archived', False)}


def public_fact(fact, reasons, sources):
    return {'id': fact['id'], 'subject_id': fact['subject_id'], 'predicate': fact['predicate'],
            'value': fact_value(fact), 'cardinality': fact.get('cardinality', 'one'),
            'certainty': certainty(fact), 'eligible': not reasons, 'exclusion_reasons': reasons,
            'source_ids': sources, 'active': fact.get('active', True)}


class Records:
    def __init__(self, repository):
        self.repo = repository
        self.pager = Pager(repository.settings.cursor_secret)

    def _sources(self, tx, scope):
        limit = self.repo.settings.max_records + 1
        size = tx.run("MATCH (s:KDSource {dataset:$d}) WHERE coalesce(s.valid,true) "
            "AND coalesce(s.record_state,'active')<>'action_draft' "
            "RETURN count(s) AS n,coalesce(sum(size(coalesce(s.message,''))),0) AS chars", d=scope.dataset).single()
        require(size['n'] < limit and size['chars'] <= 16*1024*1024, 'LIMIT_EXCEEDED',
                'The source directory exceeds this query resource limit.', 413)
        rows = [dict(row['s']) for row in tx.run(
            "MATCH (s:KDSource {dataset:$d}) WHERE coalesce(s.valid,true) "
            "AND coalesce(s.record_state,'active')<>'action_draft' "
            'RETURN properties(s) AS s ORDER BY s.id LIMIT $n', d=scope.dataset, n=limit)]
        require(len(rows) < limit, 'LIMIT_EXCEEDED', 'The source directory exceeds this query resource limit.', 413)
        return rows

    def find_records(self, scope, data):
        def read(tx, revision):
            rows = []
            if data.record_type in {'entity', 'all'}:
                objects, _ = self.repo.snapshot(tx, scope)
                for obj in objects.values():
                    if not available(obj) or data.kind and obj.get('kind') != data.kind:
                        continue
                    if data.identifier and key(obj.get('identifier', '')) != key(data.identifier):
                        continue
                    if data.namespace and key(obj.get('namespace', '')) != key(data.namespace):
                        continue
                    searchable = [obj.get(f, '') for f in ('name', 'identifier', 'namespace')]
                    searchable.extend(obj.get('aliases', []))
                    if data.text and not any(key(data.text) in key(s) for s in searchable):
                        continue
                    rows.append({'record_type': 'entity', **public_object(obj)})
            if data.record_type in {'source', 'all'} and not (data.kind or data.identifier or data.namespace):
                for source in self._sources(tx, scope):
                    if not data.text or key(data.text) in key(source.get('message', '')):
                        # Candidate search does not dump unrelated full texts.
                        rows.append({'record_type': 'source', 'id': source['id'],
                            'character_count': len(source.get('message', '')), 'created_at': source.get('created_at')})
            rows.sort(key=lambda row: (row['record_type'], row['id']))
            return {**self.pager.page(rows, scope, revision, data, 'find', data.cursor),
                    'search_mode': 'literal_substring' if data.text else 'field_match'}
        return self.repo.scoped(scope, read)

    def get_record(self, scope, data):
        def read(tx, revision):
            objects, facts = self.repo.snapshot(tx, scope)
            obj = objects.get(data.entity_id)
            require(obj and (data.include_history or available(obj)), 'NOT_FOUND', 'The record is not available.', 404)
            reasons = eligibility(objects, facts)
            selected = [f for f in facts if (f['subject_id'] == obj['id'] or f.get('object_id') == obj['id'])
                        and (data.include_history or (f.get('active', True)
                             and 'unavailable_entity' not in reasons[f['id']]))]
            support = {}
            for row in tx.run('MATCH (s:KDSource {dataset:$d})-[:KD_SUPPORTS]->(f:KDFact {dataset:$d}) '
                "WHERE f.id IN $ids AND coalesce(s.valid,true) AND coalesce(s.record_state,'active')<>'action_draft' "
                'RETURN f.id AS id,collect(DISTINCT s.id) AS sources', d=scope.dataset, ids=[f['id'] for f in selected]):
                support[row['id']] = sorted(row['sources'])
            attributes, relations = [], []
            for fact in selected:
                row = public_fact(fact, reasons[fact['id']], support.get(fact['id'], []))
                if fact.get('object_id'):
                    outgoing = fact['subject_id'] == obj['id']
                    target = objects.get(fact['object_id'] if outgoing else fact['subject_id'])
                    row.update(direction='out' if outgoing else 'in', related_object=public_object(target) if target else None)
                    relations.append(row)
                elif fact['subject_id'] == obj['id']:
                    attributes.append(row)
            return {'schema_version': '1.0', 'object': public_object(obj),
                    'facts': self.pager.page(attributes, scope, revision, data, 'record-facts', data.facts_cursor),
                    'relations': self.pager.page(relations, scope, revision, data, 'record-relations', data.relations_cursor)}
        return self.repo.scoped(scope, read)

    def get_sources(self, scope, data):
        def read(tx, revision):
            sources = {s['id']: s for s in self._sources(tx, scope)}
            if data.source_id:
                require(data.source_id in sources, 'NOT_FOUND', 'The source is not available.', 404)
                selected = [(sources[data.source_id], [])]
            else:
                found = tx.run("MATCH (f:KDFact {dataset:$d,id:$id}) WHERE coalesce(f.record_state,'active')<>'action_draft' "
                               'RETURN f.id AS id', d=scope.dataset, id=data.fact_id).single()
                require(found, 'NOT_FOUND', 'The fact is not available.', 404)
                selected = [(sources[row['id']], row['quotes']) for row in tx.run(
                    'MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(:KDFact {dataset:$d,id:$id}) '
                    'RETURN s.id AS id,coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END) AS quotes '
                    'ORDER BY s.id', d=scope.dataset, id=data.fact_id) if row['id'] in sources]
            rows = [{'source_id': s['id'], 'text': s.get('message', ''), 'quotes': quotes,
                'external_ref': s.get('external_ref', ''), 'received_at': s.get('recorded_at', s.get('created_at')),
                'recorded_by': s.get('recorded_by', ''), 'origin_channel': s.get('origin_channel', 'legacy'),
                'authenticity': 'received_text_not_verified_chat_history'} for s, quotes in selected]
            return self.pager.page(rows, scope, revision, data, 'sources', data.cursor)
        return self.repo.scoped(scope, read)
