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
    assert done['status']=='saved',done.get('error')
    checks=[]
    def check(n,v):assert v,n;checks.append(n)
    check('all 45 rows recorded automatically',store.objects('实验记录',limit=100)['total']==45)
    check('customers and people saved',store.objects('机构',limit=100)['total']==5 and store.objects('人员',limit=100)['total']==5)
    graph=store.graph();check('90 effective relations visible in graph',graph['edge_count']==90 and all(e['review_status']=='confirmed' for e in graph['edges']))
    check('bulk immediately included in precise statistics',store.query({'operation':'COUNT','kind':'实验记录'})['count']==45)
    check('bulk no longer blocks conversation',store.conversation(scope).get('pending') is None)
    draft=store.draft(done['draft_id'])
    check('whole table durable coverage',draft['bulk']['processed_rows']==45 and len(draft['bulk']['coverage'])==45)
    check('bulk creates no manual review batch',not store.batches(scope)['rows'])
    check('45 confirmed records counted',store.query({'operation':'COUNT','kind':'实验记录'})['count']==45)
    expected=sum(88 if i==45 else 100+i for i in range(1,46))
    check('literal final amount 88 preserved exactly',store.query({'operation':'SUM','kind':'实验记录','predicate':'费用','unit':'元'})['sum']==str(expected))
    check('bulk receipt idempotent',store.save_recording(done['draft_id'],scope)['idempotent'])
    # A changed value plus a new row must pause the ENTIRE table before writes.
    source2='| 编号 | 记录名称 | 费用（元） |\n| --- | --- | --- |\n| R-001 | 样本记录1 | 999 |\n| R-046 | 样本记录46 | 200 |'
    table2=parse_table(source2);mid2=store.message(source2);jid2=str(uuid4())
    plan2=TablePlan.model_validate({'action':'import','entities':[plan.entities[0].model_dump()],
        'values':[{'subject':'record','predicate':'费用','column':2,'value_type':'number','unit':'元','unit_quote':'元'}]})
    job2={**job,'id':jid2,'source_id':mid2,'source_text':source2,'table':table2,'total_rows':2,'processed_rows':0,'plan':plan2.model_dump(),'chunks':[],'entity_refs':{}}
    store.save_import_job(scope,job2)
    state=store.conversation(scope)
    store.save_conversation(scope,{**state,'pending':{'status':'import','import_id':jid2,'source_message_ids':[mid2]}},state['version'])
    await service.bulk.run(jid2)
    stopped=store.import_job(scope,jid2)
    check('risky whole batch pauses before any writes',stopped['status']=='awaiting_confirmation' and store.objects('实验记录',limit=100)['total']==45 and store.query({'operation':'SUM','kind':'实验记录','predicate':'费用','unit':'元'})['sum']==str(expected))
    card=service.operation_card(stopped['draft_id'])
    await service.confirm(stopped['draft_id'],card['approval_id'],card['draft']['revision'])
    check('authorized whole batch applies atomically',store.objects('实验记录',limit=100)['total']==46 and store.query({'operation':'SUM','kind':'实验记录','predicate':'费用','unit':'元'})['sum']==str(expected-101+999+200))
    report={'dataset':store.dataset,'passed':len(checks),'checks':checks,'objects':len(graph['nodes']),'relations':len(graph['edges']),'sum':expected}
    Path('artifacts/pending-bulk-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
    await pipeline.close();store.close()

asyncio.run(main())
