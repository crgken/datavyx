"""24-object archive workflow on the disposable database only."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from demo.config import Config
from demo.contracts import ExecutionScope
from demo.mobile_gateway import MobileGateway
from demo.model import Change
from demo.service import Service
from demo.store import Store


async def main():
    config=Config(uri='bolt://127.0.0.1:17694',dataset='archive-actions-'+uuid4().hex)
    store=Store(config)
    scope=ExecutionScope(config.dataset,session_id=uuid4().hex,actor='archive-verifier',language='en')
    gateway=MobileGateway(store,None,workspace_id=config.dataset)
    service=Service(store,None,scope=scope)
    checks=[]
    def check(name,passed):
        assert passed,name
        checks.append(name)
        print('PASS '+name,flush=True)
    def prepare(raw,text):
        mid=store.message(text,scope.session_id)
        quote={'message_id':mid,'quote':text}
        raw={**raw,'schema_version':3,'reviews':[{**r,'sources':[quote]} for r in raw.get('reviews',[])]}
        return store.prepare_execution_change(Change.model_validate(raw).model_dump(),mid,[mid],scope,store.context()['revision'])
    def visible():
        return gateway.browse(scope,{'limit':1})
    try:
        store.initialize()
        draft=prepare({'objects':[{'ref':f'o{i}','kind':'ArchiveFixture','name':f'Fixture {i}','resolution':'new'} for i in range(24)]},'24 explicitly separate synthetic test records.')
        saved=store.save_recording(draft['draft_id'],scope,uuid4().hex)
        ids=saved['entity_refs']
        store.write(lambda tx:tx.run('CREATE (:KDMobileConversation {id:$id,dataset:$d,actor:$a,refs:$refs})',id=scope.session_id,d=config.dataset,a=scope.actor,refs=ids).consume())
        check('Conversation list initially includes all 24 objects',visible()['total']==24)
        change=prepare({'reviews':[{'target_type':'entity','target_id':oid,'action':'archive','reason':'Remove synthetic incorrect records'} for oid in ids]},'Archive all 24 synthetic records, retaining history.')
        state=store.conversation(scope)
        response,changes,_=service.recording_result(change,state,uuid4().hex)
        store.save_conversation(scope,{**state,**changes,'last_response':response},state['version'])
        check('Proposal does not archive before authorization',visible()['total']==24 and response['confirmation_required'])
        result=await service.confirm(change['draft_id'],response['approval_id'],change['revision'])
        check('Durable completion receipt identifies all 24 changes',result['operation_completed'] and result['item_count']==24 and result['message']=='Completed 24 changes.')
        check('Conversation list and count exclude archived records',visible()['total']==0 and not visible()['rows'])
        check('Archived objects are absent from current types and graph',store.types()['total']==0 and store.graph()['nodes']==[])
        check('Archived objects do not enter current statistics',store.query({'operation':'COUNT','kind':'ArchiveFixture'})['count']==0)
        remaining=store.read(lambda tx:tx.run('MATCH (o:KDObject {dataset:$d,archived:true}) RETURN count(o) AS n',d=config.dataset).single()['n'])
        check('Archive retains original objects for history',remaining==24)
        state=store.conversation(scope)
        check('Completion state survives refresh',state['last_response']['operation_completed'] and state['last_response']['message']=='Completed 24 changes.')
        pending=store.read(lambda tx:tx.run("MATCH (p:KDDraft {dataset:$d,status:'pending',recording_policy_version:2}) RETURN count(p) AS n",d=config.dataset).single()['n'])
        check('Completed operation no longer appears as pending',pending==0)
        replay=await service.confirm(change['draft_id'],response['approval_id'],change['revision'])
        check('Authorization retry remains idempotent',replay['idempotent'] and visible()['total']==0)
        report={'checks':checks,'passed':True,'objects_archived':remaining,'visible_count':visible()['total']}
        target=Path(__file__).resolve().parent.parent/'artifacts/archive-actions.json'
        target.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
    finally:
        await gateway.close()
        store.close()


if __name__=='__main__':
    asyncio.run(main())
