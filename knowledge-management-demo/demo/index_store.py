"""Versioned, source-bound projections into two physically isolated indexes."""
import json
import time

from .model import digest, encoded, require


class IndexStorage:
    def _index_snapshot(self, tx, oid, lane, prefetched=None):
        if prefetched is None:
            row=tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) RETURN properties(o) AS o',d=self.dataset,id=oid).single()
            root=dict(row['o']) if row else None
        else:
            root=prefetched['objects'].get(oid)
        body={'dataset':self.dataset,'lane':lane,'root_id':oid,'objects':[],'facts':[],'proposals':[],'sources':[]}
        if not root or root.get('archived',False):
            return {'body':body,'hash':digest(body)}
        objects={oid:root}
        if prefetched is None:
            facts=[dict(r['f']) for r in tx.run('MATCH (f:KDFact {dataset:$d,subject_id:$id,active:true}) RETURN properties(f) AS f ORDER BY f.id',d=self.dataset,id=oid)]
        else:
            facts=prefetched['facts'].get(oid,[])
        # Pending facts with confirmed endpoints stay in the pending index.
        for fact in facts:
            if (lane=='approved') != (fact.get('review_status')=='confirmed'):
                continue
            if fact.get('object_id'):
                target=self.object(tx,fact['object_id']) if prefetched is None else prefetched['objects'].get(fact['object_id'])
                require(target,'object_not_found','对象不存在。',404)
                if target.get('archived',False):
                    continue
                objects[target['id']]=target
            if lane=='approved' and (not root['confirmed'] or (fact.get('object_id') and not objects[fact['object_id']]['confirmed'])):
                continue
            body['facts'].append({k:fact.get(k) for k in ('id','subject_id','predicate','object_id','value_json','review_status','numeric_precise','semantic_certain','pending_supersedes')})
        if lane=='pending':
            body['proposals']=self.pending_proposals(tx,oid) if prefetched is None else prefetched['proposals'].get(oid,[])[:]
        include_root=(lane=='approved' and root['confirmed']) or (lane=='pending' and not root['confirmed'])
        if not include_root and not body['facts'] and not body['proposals']:
            return {'body':body,'hash':digest(body)}
        for obj in sorted(objects.values(),key=lambda o:o['id']):
            body['objects'].append({k:obj.get(k) for k in ('id','name','identifier','namespace','kind','review_status','confirmed','semantic_certain')})
        targets=[oid,*[f['id'] for f in body['facts']]]
        if prefetched is None:
            body['sources']=[dict(r) for r in tx.run('MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(n) WHERE n.id IN $ids RETURN DISTINCT s.id AS id,coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END) AS quotes ORDER BY id,quotes',d=self.dataset,ids=targets)]
        else:
            sources={source for target in targets for source in prefetched['sources'].get(target,[])}
            body['sources']=[{'id':source,'quotes':list(quotes)} for source,quotes in sorted(sources)]
        body['proposals'].sort(key=lambda i:i['id'])
        return {'body':body,'hash':digest(body)}

    def _prefetch_index_snapshots(self, tx, ids):
        facts={}
        targets=set(ids)
        source_targets=set(ids)
        for row in tx.run('MATCH (f:KDFact {dataset:$d,active:true}) WHERE f.subject_id IN $ids RETURN properties(f) AS f ORDER BY f.id',d=self.dataset,ids=ids):
            fact=dict(row['f'])
            facts.setdefault(fact['subject_id'],[]).append(fact)
            source_targets.add(fact['id'])
            if fact.get('object_id'):
                targets.add(fact['object_id'])
        objects={row['o']['id']:dict(row['o']) for row in tx.run('MATCH (o:KDObject {dataset:$d}) WHERE o.id IN $ids RETURN properties(o) AS o',d=self.dataset,ids=sorted(targets))}
        sources={}
        for row in tx.run('MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(n) WHERE n.id IN $ids RETURN DISTINCT n.id AS target,s.id AS id,coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END) AS quotes',d=self.dataset,ids=sorted(source_targets)):
            sources.setdefault(row['target'],[]).append((row['id'],tuple(row['quotes'])))
        proposals={}
        for proposal in self.pending_proposals(tx):
            proposals.setdefault(proposal['subject_id'],[]).append(proposal)
        return {'objects':objects,'facts':facts,'sources':sources,'proposals':proposals}

    def enqueue_indexes(self, tx, object_ids=None, dependency_ids=None):
        # Canonical mutation + durable outbox share this transaction. Recomputing
        # heads includes incoming relation endpoints and explicit withdrawals.
        if object_ids is None:
            ids=[r['id'] for r in tx.run('MATCH (o:KDObject {dataset:$d}) RETURN o.id AS id ORDER BY o.id',d=self.dataset)]
        else:
            ids=set(object_ids)
            if dependency_ids:
                ids.update(r['id'] for r in tx.run('MATCH (f:KDFact {dataset:$d,active:true}) WHERE f.object_id IN $ids RETURN DISTINCT f.subject_id AS id',d=self.dataset,ids=list(dependency_ids)))
            ids=sorted(ids)
        if not ids:
            return
        prefetched=self._prefetch_index_snapshots(tx,ids)
        jobs=[]
        now=time.time()
        for oid in ids:
            for lane in ('approved','pending'):
                snapshot=self._index_snapshot(tx,oid,lane,prefetched)
                hid=digest([self.dataset,oid,lane])
                jid=digest([hid,snapshot['hash']])
                jobs.append({'id':jid,'oid':oid,'lane':lane,'snapshot':encoded(snapshot['body']),'hash':snapshot['hash'],'head':hid})
        tx.run("UNWIND $jobs AS job MERGE (j:KDIndexJob {id:job.id}) ON CREATE SET j.dataset=$d,j.object_id=job.oid,j.lane=job.lane,j.snapshot=job.snapshot,j.snapshot_hash=job.hash,j.status='pending',j.attempts=0,j.created_at=$now "
               'WITH j,job MERGE (h:KDIndexHead {id:job.head}) SET h.dataset=$d,h.object_id=job.oid,h.lane=job.lane,h.job_id=j.id',
               jobs=jobs,d=self.dataset,now=now).consume()

    def index_jobs(self):
        return self.read(lambda tx:[dict(r['j']) for r in tx.run("MATCH (h:KDIndexHead {dataset:$d}), (j:KDIndexJob {id:h.job_id,status:'pending'}) WHERE coalesce(j.retry_at,0)<$now RETURN properties(j) AS j ORDER BY j.lane DESC,coalesce(j.retry_at,0),j.created_at DESC,j.id LIMIT 8",d=self.dataset,now=time.time())])

    def index_result(self, jid, episode=None):
        def write(tx):
            if episode is not None:
                tx.run("MATCH (j:KDIndexJob {dataset:$d,id:$id}) SET j.status='ready',j.episode_id=$episode REMOVE j.error_code,j.retry_at",d=self.dataset,id=jid,episode=episode).consume()
            else:
                tx.run("MATCH (j:KDIndexJob {dataset:$d,id:$id}) SET j.status='pending',j.attempts=j.attempts+1,j.error_code='projection_unavailable',j.retry_at=$retry",d=self.dataset,id=jid,retry=time.time()+60).consume()
        self.write(write)

    def index_bindings(self,lane):
        def read(tx):
            self.lock(tx)
            allowed={}
            for r in tx.run("MATCH (h:KDIndexHead {dataset:$d,lane:$lane}), (j:KDIndexJob {id:h.job_id,status:'ready'}) WHERE j.episode_id<>'' RETURN properties(j) AS j",d=self.dataset,lane=lane):
                job=dict(r['j'])
                actual=self._index_snapshot(tx,job['object_id'],lane)
                if actual['hash']==job['snapshot_hash']:
                    allowed[job['episode_id']]={'job_id':job['id'],'hash':job['snapshot_hash'],'body':actual['body']}
            return allowed
        return self.read(read)

    def known_index_episodes(self,lane):
        def read(tx):
            episodes={r['id'] for r in tx.run("MATCH (j:KDIndexJob {dataset:$d,lane:$lane}) WHERE coalesce(j.episode_id,'')<>'' RETURN j.episode_id AS id",d=self.dataset,lane=lane)}
            if lane=='approved':
                episodes.update(r['id'] for r in tx.run("MATCH (p:KDDraft {dataset:$d}) WHERE p.episode_id IS NOT NULL AND p.episode_id<>'no_confirmed_facts' RETURN p.episode_id AS id",d=self.dataset))
            return episodes
        return self.read(read)

    def retry_indexes(self,scope):
        scope.check(self,'record_pending')
        self.write(lambda tx:tx.run("MATCH (h:KDIndexHead {dataset:$d}), (j:KDIndexJob {id:h.job_id,status:'pending'}) REMOVE j.retry_at,j.error_code",d=self.dataset).consume())
        return {'message':'已安排重试语义索引；规范记录不受影响。'}

    def index_health(self):
        def read(tx):
            return dict(tx.run("MATCH (h:KDIndexHead {dataset:$d}), (j:KDIndexJob {id:h.job_id}) RETURN count(CASE WHEN j.status='pending' THEN 1 END) AS projection_pending,count(CASE WHEN j.error_code IS NOT NULL THEN 1 END) AS projection_failed,count(CASE WHEN j.lane='pending' AND j.status='pending' THEN 1 END) AS pending_index_waiting,count(CASE WHEN j.lane='approved' AND j.status='pending' THEN 1 END) AS approved_index_waiting",d=self.dataset).single())
        return self.read(read)
