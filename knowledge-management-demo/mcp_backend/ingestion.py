"""Append-only recording: evidence, canonical statements and receipt in one transaction."""
import json
import time
from uuid import uuid4
from .contracts import EntityCreate
from .domain import (available, certainty, digest, eligibility, encoded, fact_value,
                     identity_key, literal_fields, validate_record, value_key)
from .errors import require


def request_key(scope, request_id):
    return digest([scope.actor, scope.client_id, scope.workspace_id, request_id])


class Ingestion:
    def __init__(self, repository):
        self.repo = repository

    def get_receipt(self, scope, data):
        def read(tx, revision):
            row = tx.run('MATCH (r:KDMCPRequest {dataset:$d,id:$id}) RETURN r.receipt AS receipt',
                         d=scope.dataset, id=request_key(scope, data.request_id)).single()
            return {'found': bool(row), 'receipt': json.loads(row['receipt']) if row else None}
        return self.repo.scoped(scope, read)

    def record_information(self, scope, data):
        validate_record(data)
        fingerprint = digest(data.model_dump(exclude={'workspace_id'}))
        rid = request_key(scope, data.request_id)
        def save(tx, revision):
            previous = tx.run('MATCH (r:KDMCPRequest {dataset:$d,id:$id}) '
                'RETURN r.fingerprint AS fingerprint,r.receipt AS receipt', d=scope.dataset, id=rid).single()
            if previous:
                require(previous['fingerprint'] == fingerprint, 'IDEMPOTENCY_CONFLICT',
                        'This request ID was already used for a different body.', 409)
                return {**json.loads(previous['receipt']), 'idempotent': True}
            objects, existing = self.repo.snapshot(tx, scope)
            now = time.time()
            metadata = {'dataset': scope.dataset, 'created_at': now, 'recorded_at': now,
                        'recorded_by': scope.actor, 'origin_channel': 'mcp', 'ingestion_schema_version': '1.0'}
            source_ids = {s.ref: uuid4().hex for s in data.sources}
            for source in data.sources:
                tx.run('CREATE (s:KDSource) SET s=$s', s={**metadata, 'id': source_ids[source.ref],
                    'message': source.text, 'external_ref': source.external_ref, 'valid': True,
                    'recording_mode': data.recording_mode}).consume()
            entity_ids = {}
            created = 0
            for entity in data.entities:
                if isinstance(entity, EntityCreate):
                    identity = identity_key(entity.kind, entity.namespace, entity.identifier)
                    # Check all physical records, including old unexecuted proposals.
                    duplicate = identity and tx.run('MATCH (o:KDObject {dataset:$d,identity_key:$identity}) '
                        'RETURN o.id AS id', d=scope.dataset, identity=identity).single()
                    require(not duplicate, 'IDENTITY_CONFLICT',
                            'This explicit identity already exists. Find and reference the existing record.', 409)
                    oid = uuid4().hex
                    obj = {**metadata, 'id': oid, 'kind': entity.kind, 'name': entity.name,
                        'identifier': entity.identifier, 'namespace': entity.namespace,
                        'identity_key': identity or 'new:' + oid, 'aliases': [], 'archived': False,
                        'record_state': 'active', 'recording_policy_version': 2}
                    # New assertions are direct records; never fabricate reviewed_by/approval.
                    tx.run('CREATE (o:KDObject) SET o=$o', o=obj).consume()
                    objects[oid] = obj
                    self._support(tx, scope, 'KDObject', oid, entity.quotes, source_ids)
                    created += 1
                else:
                    oid = entity.entity_id
                    obj = objects.get(oid)
                    require(obj and available(obj), 'INVALID_REFERENCE',
                            'A referenced entity is unavailable in this workspace.', 409)
                entity_ids[entity.ref] = oid
            facts = list(existing)
            original_ids = {f['id'] for f in existing}
            fact_ids = []
            for incoming in data.facts:
                subject = entity_ids[incoming.subject_ref]
                target = entity_ids[incoming.object_ref] if incoming.object_ref else ''
                value, scaled = ({}, None) if target else literal_fields(incoming.value)
                candidate = {**metadata, 'subject_id': subject, 'object_id': target,
                    'predicate': incoming.predicate, 'cardinality': incoming.cardinality,
                    'certainty': incoming.certainty, 'active': True, 'record_state': 'active',
                    'value_json': '' if target else encoded(value), 'number_scaled': scaled,
                    'value_type': 'object' if target else value['type'], 'unit': value.get('unit', ''),
                    'recording_policy_version': 2}
                self._compatible(objects, facts, candidate)
                candidate['value_key'] = value_key(candidate)
                same = next((f for f in facts if f['subject_id'] == subject and f['predicate'] == incoming.predicate
                    and f.get('active', True) and f.get('record_state') != 'action_draft'
                    and value_key(f) == candidate['value_key'] and certainty(f) == incoming.certainty
                    and f.get('cardinality', 'one') == incoming.cardinality), None)
                if same:
                    fid = same['id']
                else:
                    fid = uuid4().hex
                    candidate['id'] = fid
                    tx.run('CREATE (f:KDFact) SET f=$f WITH f '
                        'MATCH (o:KDObject {dataset:$d,id:$subject}) CREATE (o)-[:KD_HAS_FACT]->(f) '
                        'WITH f OPTIONAL MATCH (t:KDObject {dataset:$d,id:$target}) '
                        'FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | CREATE (f)-[:KD_TARGET]->(t))',
                        f=candidate, d=scope.dataset, subject=subject, target=target).consume()
                    facts.append(candidate)
                self._support(tx, scope, 'KDFact', fid, incoming.quotes, source_ids)
                if fid not in fact_ids:
                    fact_ids.append(fid)
            reasons = eligibility(objects, facts)
            # Counters describe statements touched by this receipt, not the whole workspace.
            receipt = {'status': 'recorded', 'schema_version': '1.0', 'request_id': data.request_id,
                'receipt_id': rid, 'revision': revision + 1, 'entity_ids': entity_ids, 'source_ids': source_ids,
                'fact_ids': fact_ids, 'objects_created': created, 'facts_created': len(set(fact_ids) - original_ids),
                'sources_recorded': len(source_ids), 'conflict_count': sum('conflict' in reasons[f] for f in fact_ids),
                'uncertain_count': sum(any(r in reasons[f] for r in ('uncertain', 'uncertain_identity')) for f in fact_ids),
                'idempotent': False}
            # Bound the resulting workspace as well as the pre-write snapshot.
            limit = self.repo.settings.max_records
            require(len(objects) <= limit and len(facts) <= limit, 'LIMIT_EXCEEDED',
                    'This recording would exceed the workspace resource limit.', 413)
            source_count = tx.run('MATCH (s:KDSource {dataset:$d}) RETURN count(s) AS n', d=scope.dataset).single()['n']
            require(source_count <= limit, 'LIMIT_EXCEEDED', 'This recording would exceed the source resource limit.', 413)
            tx.run('CREATE (r:KDMCPRequest) SET r=$r WITH r MATCH (d:KDData {id:$d}) SET d.revision=$revision',
                r={**metadata, 'id': rid, 'actor': scope.actor, 'client_id': scope.client_id,
                   'workspace_id': scope.workspace_id, 'request_id': data.request_id,
                   'fingerprint': fingerprint, 'receipt': encoded(receipt), 'event': 'knowledge_recorded'},
                d=scope.dataset, revision=revision+1).consume()
            return receipt
        return self.repo.scoped(scope, save, 'record')

    @staticmethod
    def _compatible(objects, facts, candidate):
        kind = objects[candidate['subject_id']]['kind']
        prior = [f for f in facts if f['predicate'] == candidate['predicate']
                 and objects.get(f['subject_id'], {}).get('kind') == kind]
        if not prior:
            return
        require({f.get('cardinality', 'one') for f in prior} == {candidate['cardinality']},
                'SCHEMA_CONFLICT', 'The cardinality is incompatible with the existing predicate.', 409)
        def shape(f):
            return ('entity', objects.get(f.get('object_id'), {}).get('kind', '')) if f.get('object_id') else (fact_value(f).get('type'), '')
        require(shape(candidate) in {shape(f) for f in prior}, 'SCHEMA_CONFLICT',
                'The value or target type is incompatible with the existing predicate.', 409)

    @staticmethod
    def _support(tx, scope, label, target, quotes, source_ids):
        assert label in {'KDObject', 'KDFact'}
        for quote in quotes:
            tx.run(f'MATCH (s:KDSource {{dataset:$d,id:$source}}),(n:{label} {{dataset:$d,id:$target}}) '
                'MERGE (s)-[e:KD_SUPPORTS]->(n) '
                'SET e.quotes=CASE WHEN $quote IN coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END) '
                'THEN coalesce(e.quotes,[e.quote]) ELSE coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END)+[$quote] END',
                d=scope.dataset, source=source_ids[quote.source_ref], target=target, quote=quote.quote).consume()
