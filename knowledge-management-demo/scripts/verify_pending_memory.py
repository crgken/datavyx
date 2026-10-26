"""Real Graphiti 0.30.1 and real model, two dedicated test Neo4j instances."""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4
from demo.config import Config,load_model_env
from demo.contracts import ExecutionScope
from demo.graphiti_adapter import GraphitiAdapter
from demo.graphiti_extension import Pipeline
from demo.model import Change
from demo.store import Store

async def main():
    load_model_env(sys.argv[1])
    config=Config(uri='bolt://127.0.0.1:17694',password='atlas-demo-local',pending_uri='bolt://127.0.0.1:17696',dataset='pending-memory-'+uuid4().hex)
    store=Store(config);store.initialize();scope=ExecutionScope(config.dataset);pipeline=Pipeline(config);memory=GraphitiAdapter(store,pipeline,scope);checks=[]
    def check(n,v):assert v,n;checks.append(n);print('PASS',n,flush=True)
    try:
        suffix=uuid4().hex[:8]
        sample,lab='海棠样本'+suffix,'晨光实验室'+suffix
        text=f'登记样本{sample}，它由{lab}保管。{lab}是机构。'
        mid=store.message(text)
        change=Change.model_validate({'objects':[{'ref':'a','name':sample,'kind':'样本','resolution':'new'},{'ref':'b','name':lab,'kind':'机构','resolution':'new'}],
            'facts':[{'subject':'a','predicate':'保管机构','object':'b','evidence':text,'sources':[{'message_id':mid,'quote':text}]}]})
        draft=store.prepare_execution_change(change.model_dump(),mid,[mid],scope,store.context()['revision'])
        saved=store.save_pending(draft['draft_id'],scope);oid=draft['objects'][0]['id']
        def job(lane):
            return store.read(lambda tx:dict(tx.run('MATCH (h:KDIndexHead {dataset:$d,object_id:$oid,lane:$lane}), (j:KDIndexJob {id:h.job_id}) RETURN properties(j) AS j',d=store.dataset,oid=oid,lane=lane).single()['j']))
        pending=job('pending')
        check('pending uses physically separate driver',pipeline.for_lane('pending').config.uri!=pipeline.config.uri)
        await pipeline.for_lane('pending').project_index(pending,store)
        check('pending Graphiti episode created',bool(store.index_bindings('pending')))
        check('pending excluded from approved bindings',not store.index_bindings('approved'))
        result=await memory.retrieve_context(sample+'由哪个实验室保管？')
        check('real pending memory search resolves canonical record',any(oid in i['canonical_refs'] and i['lane']=='pending' for i in result['items']))
        check('pending retrieval exposes current pending status',all(not r['object']['confirmed'] for i in result['items'] for r in i['canonical_records']))
        batch=store.read(lambda tx:store._batch(tx,saved['batch_id'],scope))
        token=store.prepare_batch_review(scope,batch['id'],[i['id'] for i in batch['items']],'confirm');store.apply_batch_review(scope,token['token'])
        check('confirmation immediately invalidates stale pending binding',not store.index_bindings('pending'))
        approved=job('approved');await pipeline.project_index(approved,store)
        check('approved episode created only after human review',bool(store.index_bindings('approved')))
        result=await memory.retrieve_context(sample+'由哪个实验室保管？')
        check('approved retrieval exposes current confirmed state',any(oid in i['canonical_refs'] and all(r['object']['confirmed'] for r in i['canonical_records']) for i in result['items']))
        await pipeline.lock.acquire()
        try:
            result=await asyncio.wait_for(memory.retrieve_context(sample+'由哪个实验室保管？'),timeout=5)
            check('busy indexing does not block canonical fallback',result['status']=='indexing' and store.detail(oid)['object']['confirmed'])
        finally:
            pipeline.lock.release()
        store.index_result(approved['id'])
        check('index failure does not remove canonical record',store.detail(oid)['object']['confirmed'])
        store.retry_indexes(scope)
        check('retry remains durable',store.health()['projection_pending']>0)
        report={'dataset':store.dataset,'passed':len(checks),'checks':checks}
        Path('artifacts/pending-memory-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    finally:
        await pipeline.close();store.close()

asyncio.run(main())
