"""Acceptance against the dedicated disposable Neo4j, never the live database."""
import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from demo.config import Config
from demo.contracts import ExecutionScope,ReviewRequest
from demo.model import Change, DemoError
from demo.store import Store

config=Config(uri='bolt://127.0.0.1:17694',password='atlas-demo-local',dataset='pending-acceptance-'+str(uuid4()),pending_uri='bolt://127.0.0.1:17696')
assert ':17694' in config.uri
store=Store(config);store.initialize();scope=ExecutionScope(config.dataset)
checks=[]
def check(name,value):
    assert value,name
    checks.append(name)
def rejected(name,fn):
    try: fn()
    except DemoError: checks.append(name);return
    raise AssertionError(name)
def save(objects,facts=None,updates=None,reviews=None,text='fixture source',request_id=''):
    mid=store.message(text)
    q={'message_id':mid,'quote':text}
    raw={'schema_version':3,'objects':objects,'facts':[{**f,'evidence':text,'sources':[q]} for f in facts or []],
         'entity_updates':[{**u,'sources':[q]} for u in updates or []], 'reviews':[{**r,'sources':[q]} for r in reviews or []]}
    change=Change.model_validate(raw).model_dump()
    draft=store.prepare_execution_change(change,mid,[mid],scope,store.context()['revision'])
    result=store.save_pending(draft['draft_id'],scope,request_id)
    return result,store.read(lambda tx:store._batch(tx,result['batch_id'],scope)),draft
def apply(batch,ids=None,action='confirm'):
    ids=ids or [i['id'] for i in batch['items'] if i['status']=='pending']
    token=store.prepare_batch_review(scope,batch['id'],ids,action)
    return store.apply_batch_review(scope,token['token'])
def obj(ref,name='',identifier='',kind='仪器',oid=''):
    return {'ref':ref,'name':name,'kind':kind,'identifier':identifier,'resolution':'existing' if oid else 'new','existing_id':oid}
def count():return store.query({'operation':'COUNT','kind':'仪器'})['count']
def total():return store.query({'operation':'SUM','kind':'仪器','predicate':'价格','unit':'CNY'})['sum']

result,b1,draft=save([obj('a','A002')],[{'subject':'a','predicate':'价格','value':{'type':'number','value':'100','unit':'CNY'}}],request_id='durable-key')
aid=draft['objects'][0]['id'];fid=b1['items'][1]['target_id']
entity=store.detail(aid)['object']
check('explicit name does not populate identifier',entity['name']=='A002' and entity['identifier']=='')
check('save pending separates numeric precision',not entity['confirmed'] and store.detail(aid)['rows'][0]['numeric_precise'])
check('pending excluded from precise count and sum',count()==0 and total()=='0')
check('pending searchable',store.search_records({'text':'A002'})['total']==1)
check('pending visible in full graph',any(n['id']==aid and not n['confirmed'] for n in store.graph()['nodes']))
check('pending persisted across store recreation',Store(config).detail(aid)['object']['id']==aid)
check('save idempotent',store.save_pending(draft['draft_id'],scope)['idempotent'])
store.save_pending(draft['draft_id'],scope,'durable-key')
check('request receipt is durable',store.pending_receipt(scope,'durable-key','fixture source')['batch_id']==result['batch_id'])
rejected('idempotency key cannot change payload',lambda:store.pending_receipt(scope,'durable-key','different source'))
rejected('record_pending permission enforced',lambda:store.save_pending(draft['draft_id'],replace(scope,permissions=frozenset({'read','propose'}))))
rejected('batch ownership enforced',lambda:store.batches(replace(scope,dataset='other')))

_,b2,d2=save([obj('b',identifier='B003')])
bid=d2['objects'][0]['id'];e2=store.detail(bid)['object']
check('explicit identifier does not populate name',e2['identifier']=='B003' and not e2['name'])
check('multiple durable batches',store.batches(scope)['total']==2)
check('request retry survives intervening request',store.pending_receipt(scope,'durable-key','fixture source')['batch_id']==b1['id'])
frozen=store.prepare_batch_review(scope,b1['id'],[i['id'] for i in b1['items']],'confirm')
_,b3,_=save([obj('c','无关对象')])
applied=store.apply_batch_review(scope,frozen['token'])
check('unrelated save does not invalidate frozen selection',not applied['idempotent'])
check('confirmed values participate in statistics',count()==1 and total()=='100')
check('confirmation idempotent',store.apply_batch_review(scope,frozen['token'])['idempotent'])
check('no automatic confirmation of other batches',not store.detail(bid)['object']['confirmed'])

_,correction,dc=save([obj('a','A002',oid=aid)],[{'subject':'a','predicate':'价格','value':{'type':'number','value':'250','unit':'CNY'},'mode':'replace'}])
check('pending new fact does not demote approved identity',store.detail(aid)['object']['confirmed'])
check('pending correction leaves old precise sum',total()=='100')
check('both current and proposed values visible',len(store.detail(aid)['rows'])==2)
apply(correction)
check('correction switches statistic atomically',total()=='250')
check('old value retained in history',len(store.detail(aid,history=True)['rows'])==2 and not next(f for f in store.detail(aid,history=True)['rows'] if f['id']==fid)['active'])

_,fields,_=save([obj('a','A002',oid=aid)],updates=[{'ref':'a','field':'name','previous':'A002','value':'新名称','reason':'名称更正'}])
check('pending identity edit keeps current field',store.detail(aid)['object']['name']=='A002')
check('identity edit is visible as proposal',bool(store.detail(aid)['pending_proposals']))
apply(fields)
check('identity edit applies on review',store.detail(aid)['object']['name']=='新名称')

_,person,dp=save([obj('p','测试人员',kind='人员'),obj('o','测试机构',kind='机构')])
apply(person)
pid,oid=[o['id'] for o in dp['objects']]
_,rel,_=save([obj('p','测试人员',kind='人员',oid=pid),obj('o','测试机构',kind='机构',oid=oid)],facts=[{'subject':'p','predicate':'联系人','object':'o','cardinality':'many'}])
check('correct person type only adds relationship',store.detail(pid)['object']['kind']=='人员' and len(rel['items'])==1 and rel['items'][0]['type']=='fact')
check('pending relation ordinary neighbors visible',store.neighbors(pid)['total']==1)
check('pending relation graph is marked',next(e for e in store.graph()['edges'] if e['source']==pid)['review_status']=='pending')
apply(rel)

_,bad,db=save([obj('x','误分类对象',kind='旧类型')]);apply(bad);xid=db['objects'][0]['id']
_,types,_=save([],reviews=[{'target_type':'entity','target_id':xid,'action':'reclassify','new_kind':'新类型','classification_error':True,'reason':'核实分类错误'}])
check('pending reclassification does not alter type',store.detail(xid)['object']['kind']=='旧类型')
apply(types)
check('reclassification preserves canonical id',store.detail(xid)['object']['kind']=='新类型' and store.detail(xid)['object']['id']==xid)

_,approx,da=save([obj('z','估值仪器')],facts=[{'subject':'z','predicate':'价格','value':{'type':'number','value':'900','unit':'CNY'},'certain':False,'reason':'估计金额'}])
apply(approx)
check('review cannot make approximate number precise',not store.detail(da['objects'][0]['id'])['rows'][0]['numeric_precise'] and total()=='250')

_,withdraw,dw=save([obj('w','撤回仪器')],facts=[{'subject':'w','predicate':'价格','value':{'type':'number','value':'30','unit':'CNY'}}])
wid=dw['objects'][0]['id'];apply(withdraw,action='withdraw')
check('withdrawn pending object hidden from graph',all(n['id']!=wid for n in store.graph()['nodes']))
check('withdrawal retains historical facts',not store.detail(wid,history=True)['rows'][0]['active'])

_,partial,dpart=save([obj('part','部分确认')],facts=[{'subject':'part','predicate':'价格','value':{'type':'number','value':'45','unit':'CNY'}}])
apply(partial,[partial['items'][0]['id']])
check('partial review leaves other item pending',store.read(lambda tx:store._batch(tx,partial['id'],scope))['items'][1]['status']=='pending')
check('partial review does not include amount in statistics',total()=='250')

_,stale,ds=save([obj('s','版本对象')])
freeze=store.prepare_batch_review(scope,stale['id'],[stale['items'][0]['id']],'confirm')
store.write(lambda tx:tx.run('MATCH (o:KDObject {id:$id}) SET o.name=$name',id=ds['objects'][0]['id'],name='版本已修改').consume())
rejected('target mutation invalidates frozen review',lambda:store.apply_batch_review(scope,freeze['token']))
rejected('confirmation permission enforced',lambda:store.apply_batch_review(replace(scope,permissions=frozenset({'read','propose','record_pending'})),freeze['token']))

pending_jobs=store.index_jobs()
check('transactional outbox contains both lanes',bool(store.read(lambda tx:tx.run("MATCH (h:KDIndexHead {dataset:$d,lane:'pending'}) RETURN count(h) AS n",d=store.dataset).single()['n'])))
pending_body=store.read(lambda tx:store._index_snapshot(tx,aid,'pending'))
approved_body=store.read(lambda tx:store._index_snapshot(tx,aid,'approved'))
check('confirmed value is absent from pending lane facts',not pending_body['body']['facts'])
check('approved lane contains approved canonical value',any(json.loads(f['value_json'])['value']=='250' for f in approved_body['body']['facts']))
store.initialize()
check('migration is idempotent and does not replay drafts',store.detail(aid)['object']['name']=='新名称')

_,invalid,di=save([obj('invalid','来源测试')],text='来源测试：登记仪器')
frozen=store.prepare_batch_review(scope,invalid['id'],[invalid['items'][0]['id']],'confirm')
store.write(lambda tx:tx.run('MATCH (s:KDSource {id:$id,dataset:$d}) SET s.valid=false',id=di['message_id'],d=store.dataset).consume())
rejected('source validity enforced during confirmation',lambda:store.apply_batch_review(scope,frozen['token']))
check('invalid source confirmation rolls back atomically',not store.detail(di['objects'][0]['id'])['object']['confirmed'])
rejected('existing review entry obeys same source constraint',lambda:store.prepare_button_review(ReviewRequest(operations=[{'target_type':'entity','target_id':di['objects'][0]['id'],'action':'confirm','reason':'测试确认'}],revision=store.context()['revision']),scope))
apply(invalid,action='withdraw')
check('invalid pending source can still be withdrawn',store.detail(di['objects'][0]['id'])['object']['archived'])

part_id=dpart['objects'][0]['id']
_,amended,_=save([obj('part','部分确认',oid=part_id)],[{'subject':'part','predicate':'价格','value':{'type':'number','value':'55','unit':'CNY'},'mode':'replace'}])
check('pending amendment supersedes only prior pending version',store.read(lambda tx:store._batch(tx,partial['id'],scope))['items'][1]['status']=='superseded' and total()=='250')
apply(amended)
check('amended pending value becomes precise only after review',total()=='305')
_,stale_proposal,_=save([],reviews=[{'target_type':'entity','target_id':xid,'action':'reclassify','new_kind':'第三类型','classification_error':True,'reason':'再次分类核实'}])
store.write(lambda tx:tx.run('MATCH (o:KDObject {dataset:$d,id:$id}) SET o.kind=$kind',d=store.dataset,id=xid,kind='其他当前类型').consume())
rejected('proposal base version must match before review',lambda:store.prepare_batch_review(scope,stale_proposal['id'],[stale_proposal['items'][0]['id']],'confirm'))
apply(stale_proposal,action='withdraw')
check('stale proposal can be withdrawn without changing object',store.detail(xid)['object']['kind']=='其他当前类型')

report={'dataset':store.dataset,'passed':len(checks),'checks':checks}
Path('artifacts/pending-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
store.close()
