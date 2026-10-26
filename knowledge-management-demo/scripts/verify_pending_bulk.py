"""45-row generated fixture, isolated DB; never reuses the user's old table."""
import asyncio
import json
from pathlib import Path
from uuid import uuid4
from demo.bulk_import import TablePlan,parse_table
from demo.config import Config
from demo.contracts import ExecutionScope
from demo.graphiti_extension import Pipeline
from demo.service import Service
from demo.store import Store

async def main():
    config=Config(uri='bolt://127.0.0.1:17694',password='atlas-demo-local',pending_uri='bolt://127.0.0.1:17696',dataset='pending-bulk-'+uuid4().hex)
    store=Store(config);store.initialize();pipeline=Pipeline(config);service=Service(store,pipeline);scope=service.scope
    source='| 编号 | 记录名称 | 委托机构 | 经办人员 | 费用（元） |\n| --- | --- | --- | --- | --- |\n'+'\n'.join(f'| R-{i:03} | 样本记录{i} | 实验室{i%5} | 经办员{i%5} | {88 if i==45 else 100+i} |' for i in range(1,46))
    table=parse_table(source)
    plan=TablePlan.model_validate({'action':'import','entities':[
        {'key':'record','kind':'实验记录','name_column':1,'identifier_column':0,'identity_policy':'identifier','identity_columns':[0]},
        {'key':'lab','kind':'机构','name_column':2,'identity_policy':'stable_name','identity_columns':[2]},
        {'key':'person','kind':'人员','name_column':3,'identity_policy':'contextual_name','identity_columns':[3,2]}],
        'relations':[{'subject':'record','predicate':'委托机构','object':'lab'},{'subject':'record','predicate':'经办人','object':'person'}],
        'values':[{'subject':'record','predicate':'费用','column':4,'value_type':'number','unit':'元','unit_quote':'元'}]})
    mid=store.message(source);jid=str(uuid4());job={'id':jid,'status':'running','table':table,'source_id':mid,'source_text':source,'total_rows':45,'processed_rows':0,'plan':plan.model_dump(),'chunks':[],'error':'','questions':[],'draft_id':'','entity_refs':{}}
    store.save_import_job(scope,job)
    state=store.conversation(scope)
    store.save_conversation(scope,{**state,'pending':{'status':'import','import_id':jid,'source_message_ids':[mid]}},state['version'])
    await service.bulk.run(jid)
    done=store.import_job(scope,jid)
    assert done['status']=='saved_pending',done.get('error')
    batch=store.read(lambda tx:store._batch(tx,done['batch_id'],scope))
    checks=[]
    def check(n,v):assert v,n;checks.append(n)
    check('all 45 rows saved pending',store.objects('实验记录',limit=100)['total']==45)
    check('customers and people saved',store.objects('机构',limit=100)['total']==5 and store.objects('人员',limit=100)['total']==5)
    graph=store.graph();check('90 pending relations visible in graph',graph['edge_count']==90 and all(e['review_status']=='pending' for e in graph['edges']))
    check('bulk not included in precise statistics',store.query({'operation':'COUNT','kind':'实验记录'})['count']==0)
    check('bulk no longer blocks conversation',store.conversation(scope).get('pending') is None)
    check('whole table durable coverage',batch['metadata']['bulk']['processed_rows']==45 and len(batch['metadata']['bulk']['coverage'])==45)
    token=store.prepare_batch_review(scope,batch['id'],[i['id'] for i in batch['items']],'confirm')
    result=store.apply_batch_review(scope,token['token'])
    check('batch review supports more than 40 operations',not result['idempotent'] and len(batch['items'])>40)
    check('45 confirmed records counted',store.query({'operation':'COUNT','kind':'实验记录'})['count']==45)
    expected=sum(88 if i==45 else 100+i for i in range(1,46))
    check('literal final amount 88 preserved exactly',store.query({'operation':'SUM','kind':'实验记录','predicate':'费用','unit':'元'})['sum']==str(expected))
    check('review all idempotent',store.apply_batch_review(scope,token['token'])['idempotent'])
    report={'dataset':store.dataset,'passed':len(checks),'checks':checks,'objects':len(graph['nodes']),'relations':len(graph['edges']),'sum':expected}
    Path('artifacts/pending-bulk-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    await pipeline.close();store.close()

asyncio.run(main())
