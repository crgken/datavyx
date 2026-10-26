"""Versioned, source-bound projections into two physically isolated indexes."""
import json
import time

from .model import digest, encoded, require


class IndexStorage:
    def _index_snapshot(self, tx, oid, lane):
        row=tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) RETURN properties(o) AS o',d=self.dataset,id=oid).single()
        body={'dataset':self.dataset,'lane':lane,'root_id':oid,'objects':[],'facts':[],'proposals':[],'sources':[]}
        if not row or row['o'].get('archived',False):
            return {'body':body,'hash':digest(body)}
        root=dict(row['o'])
        objects={oid:root}
        # Pending facts with confirmed endpoints stay in the pending index.
        for r in tx.run('MATCH (f:KDFact {dataset:$d,subject_id:$id,active:true}) RETURN properties(f) AS f ORDER BY f.id',d=self.dataset,id=oid):
            fact=dict(r['f'])
            if (lane=='approved') != (fact.get('review_status')=='confirmed'):
                continue
            if fact.get('object_id'):
                target=self.object(tx,fact['object_id'])
                if target.get('archived',False):
                    continue
                objects[target['id']]=target
            if lane=='approved' and (not root['confirmed'] or (fact.get('object_id') and not objects[fact['object_id']]['confirmed'])):
                continue
            body['facts'].append({k:fact.get(k) for k in ('id','subject_id','predicate','object_id','value_json','review_status','numeric_precise','semantic_certain','pending_supersedes')})
        if lane=='pending':
            body['proposals']=self.pending_proposals(tx,oid)
        include_root=(lane=='approved' and root['confirmed']) or (lane=='pending' and not root['confirmed'])
        if not include_root and not body['facts'] and not body['proposals']:
            return {'body':body,'hash':digest(body)}
        for obj in sorted(objects.values(),key=lambda o:o['id']):
            body['objects'].append({k:obj.get(k) for k in ('id','name','identifier','namespace','kind','review_status','confirmed','semantic_certain')})
        targets=[oid,*[f['id'] for f in body['facts']]]
        body['sources']=[dict(r) for r in tx.run('MATCH (s:KDSource {dataset:$d})-[e:KD_SUPPORTS]->(n) WHERE n.id IN $ids RETURN DISTINCT s.id AS id,coalesce(e.quotes,CASE WHEN e.quote IS NULL THEN [] ELSE [e.quote] END) AS quotes ORDER BY id,quotes',d=self.dataset,ids=targets)]
        body['proposals'].sort(key=lambda i:i['id'])
        return {'body':body,'hash':digest(body)}

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
        for oid in ids:
            for lane in ('approved','pending'):
                snapshot=self._index_snapshot(tx,oid,lane)
                hid=digest([self.dataset,oid,lane])
                jid=digest([hid,snapshot['hash']])
                tx.run("MERGE (j:KDIndexJob {id:$id}) ON CREATE SET j.dataset=$d,j.object_id=$oid,j.lane=$lane,j.snapshot=$snapshot,j.snapshot_hash=$hash,j.status='pending',j.attempts=0,j.created_at=$now "
                       'WITH j MERGE (h:KDIndexHead {id:$head}) SET h.dataset=$d,h.object_id=$oid,h.lane=$lane,h.job_id=j.id',
                       id=jid,d=self.dataset,oid=oid,lane=lane,snapshot=encoded(snapshot['body']),hash=snapshot['hash'],now=time.time(),head=hid).consume()

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
