"""Real Pydantic AI model acceptance; all writes go to isolated test dataset."""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4
from demo.config import Config,load_model_env
from demo.contracts import ExecutionScope
from demo.graphiti_extension import Pipeline
from demo.service import Service
from demo.store import Store

async def main():
    load_model_env(sys.argv[1])
    config=Config(uri='bolt://127.0.0.1:17694',password='atlas-demo-local',pending_uri='bolt://127.0.0.1:17696',dataset='pending-agent-'+uuid4().hex)
    store=Store(config);store.initialize();pipeline=Pipeline(config);service=Service(store,pipeline)
    checks=[];turns=[]
    def check(name,value):
        assert value,name
        checks.append(name)
    async def turn(text):
        print('TURN',text,flush=True)
        response=await service.chat(text,str(uuid4()));turns.append({'input':text,'response':response})
        print(json.dumps(response,ensure_ascii=False),flush=True)
        return response
    def objects():return store.objects(limit=100)['rows']
    def entity(label):return next(o for o in objects() if o['name']==label or o['identifier']==label)
    def confirm_all():
        for batch in store.batches(service.scope,limit=100)['rows']:
            t=store.prepare_batch_review(service.scope,batch['id'],[i['id'] for i in batch['items'] if i['status']=='pending'],'confirm')
            store.apply_batch_review(service.scope,t['token'])
    try:
        r=await turn('登记一台仪器，名称就叫 A002，仅名称，没有提供编号。')
        check('name only auto saves pending',r.get('saved_pending') and entity('A002')['identifier']=='')
        r=await turn('再登记一份文献，只给编号 DOC-7，不提供标题或名称。')
        check('identifier only without invented name',r.get('saved_pending') and entity('DOC-7')['name']=='')
        r=await turn('登记人员林岚，以及机构青云研究所，先只记录这两个对象。')
        check('new multi object batch auto saves',r.get('saved_pending'))
        confirm_all()
        pid=entity('林岚')['id']
        r=await turn('补充：林岚是青云研究所的联络人。人员类型正确，只补充这个关系。')
        check('person role supplements relationship without type change',r.get('saved_pending') and entity('林岚')['kind']=='人员' and entity('林岚')['id']==pid and any('联络' in f['predicate'] for f in store.detail(pid)['rows']))
        r=await turn('建立一个对象，名称为星桥设备，类型明确记录为项目。这是原始分类。')
        check('classification seed saved',r.get('saved_pending'))
        confirm_all()
        xid=entity('星桥设备')['id']
        r=await turn('更正星桥设备的实体分类：原来的项目类型错误，正确类型是设备。保留这个对象及它的历史。')
        check('type correction saved as proposal retaining old type',r.get('saved_pending') and entity('星桥设备')['kind']=='项目')
        confirm_all()
        check('confirmed type correction keeps object id',entity('星桥设备')['kind']=='设备' and entity('星桥设备')['id']==xid)
        r=await turn('查询有哪些文献，包括尚未确认的信息。')
        check('ordinary query uses all-status canonical search','record_query' in r.get('result',{}))
        report={'dataset':store.dataset,'passed':len(checks),'checks':checks,'turns':turns}
        Path('artifacts/pending-agent-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('PASSED',len(checks),flush=True)
    finally:
        if service._runtime:await service.runtime.close()
        await pipeline.close();store.close()

asyncio.run(main())
