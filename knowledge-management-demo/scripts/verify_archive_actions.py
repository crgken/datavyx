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
        cancelled_id=change['draft_id']
        cancelled_approval=response['approval_id']
        cancelled=await service.cancel_preview(cancelled_id)
        context=store.operation_context(scope)
        check('Cancellation closes the owned operation without changing records',cancelled['outcome']=='cancelled' and context['active_total']==0 and visible()['total']==24)
        check('Cancelled authorization is revoked',store.read(lambda tx:tx.run('MATCH (a:KDApproval {id:$id}) RETURN a.status AS status',id=cancelled_approval).single()['status'])=='revoked')
        state=store.conversation(scope)
        check('Cancellation event is durable in model history',any('operation_state_changed' in str(h) and 'cancelled' in str(h) for h in state['history']))
        check('Cancellation is visible in conversation messages',store.messages(scope.session_id)[-1]['text']==cancelled['message'])
        # A stale persisted pointer must not override the database lifecycle state.
        mid=store.message('Archive all 24 synthetic records again.',scope.session_id)
        deps=service.dependencies({**state,'pending':{'draft_id':cancelled_id,'source_message_ids':[]}}, {'id':mid,'text':'Archive all 24 synthetic records again.'})
        check('Closed drafts cannot reappear as current model operations',deps.state['pending'] is None and 'current_draft' not in deps.state and deps.state['operation_state']['active_total']==0)
        change=prepare({'reviews':[{'target_type':'entity','target_id':oid,'action':'archive','reason':'Fresh request after cancellation'} for oid in ids]},'Archive all 24 synthetic records again.')
        state=store.conversation(scope)
        response,changes,_=service.recording_result(change,state,uuid4().hex)
        store.save_conversation(scope,{**state,**changes,'last_response':response},state['version'])
        check('New request receives a fresh draft and authorization',change['draft_id']!=cancelled_id and response['approval_id']!=cancelled_approval and store.operation_context(scope)['active_total']==1)
        result=await service.confirm(change['draft_id'],response['approval_id'],change['revision'])
        check('Durable completion receipt identifies all 24 changes',result['operation_completed'] and result['item_count']==24 and result['message']=='Completed 24 changes.')
        check('Conversation list and count exclude archived records',visible()['total']==0 and not visible()['rows'])
        check('Archived objects are absent from current types and graph',store.types()['total']==0 and store.graph()['nodes']==[])
        check('Archived objects do not enter current statistics',store.query({'operation':'COUNT','kind':'ArchiveFixture'})['count']==0)
        remaining=store.read(lambda tx:tx.run('MATCH (o:KDObject {dataset:$d,archived:true}) RETURN count(o) AS n',d=config.dataset).single()['n'])
        check('Archive retains original objects for history',remaining==24)
        state=store.conversation(scope)
        check('Completion state survives refresh',state['last_response']['operation_completed'] and state['last_response']['message']=='Completed 24 changes.')
        check('Completion event closes model history state',any('operation_state_changed' in str(h) and 'committed' in str(h) for h in state['history']) and store.operation_context(scope)['active_total']==0)
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
