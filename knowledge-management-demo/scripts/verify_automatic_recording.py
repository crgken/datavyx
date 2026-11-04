"""Automatic recording, authorization and migration on disposable Neo4j only."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from demo.config import Config
from demo.contracts import ExecutionScope
from demo.model import Change, DemoError
from demo.service import Service
from demo.store import Store


def main():
    store = Store(Config(uri='bolt://127.0.0.1:17694', dataset='automatic-' + uuid4().hex))
    owner = ExecutionScope(store.dataset, language='en')
    editor = replace(owner, permissions=frozenset({'read','propose','record'}))
    checks = []

    def check(name, value):
        assert value, name
        checks.append(name)
        print('PASS ' + name, flush=True)

    def prepare(objects, facts=None, updates=None, reviews=None, scope=editor):
        text = 'A structured fixture with an explicitly supplied amount of 100 USD.'
        mid = store.message(text, scope.session_id)
        q = {'message_id':mid,'quote':text}
        raw = {'schema_version':3, 'objects':objects,
               'facts':[{**f,'evidence':text,'sources':[q]} for f in facts or []],
               'entity_updates':[{**u,'sources':[q]} for u in updates or []],
               'reviews':[{**r,'sources':[q]} for r in reviews or []]}
        change = Change.model_validate(raw).model_dump()
        return store.prepare_execution_change(change,mid,[mid],scope,store.context()['revision'])

    def obj(ref, name='', oid='', certain=True):
        return {'ref':ref,'name':name,'kind':'Client','resolution':'existing' if oid else 'new',
                'existing_id':oid,'certain':certain,'reason':'' if certain else 'Identity is ambiguous'}

    def amount(value, certain=True, mode='assert'):
        return {'subject':'a','predicate':'amount','value':{'type':'number','value':value,'unit':'USD'},
                'certain':certain,'reason':'' if certain else 'Estimated by the source','mode':mode}

    def total():
        return store.query({'operation':'SUM','kind':'Client','predicate':'amount','unit':'USD'})['sum']

    try:
        store.initialize()
        draft = prepare([obj('a','Automatic client')], [amount('100')])
        request_id = uuid4().hex
        result = store.save_recording(draft['draft_id'],editor,request_id)
        oid = draft['objects'][0]['id']
        check('Editor records ordinary information without approval', result['outcome']=='saved' and not result['saved_pending'])
        check('Ordinary records immediately enter count and sum',store.query({'operation':'COUNT','kind':'Client'})['count']==1 and total()=='100')
        record = store.detail(oid)['object']
        check('Automatic recording has real provenance without forged review',record['recorded_by']==editor.actor and 'reviewed_by' not in record)
        repeated = store.save_recording(draft['draft_id'],editor,request_id)
        check('Durable recording receipt is idempotent',repeated['idempotent'] and repeated['record_counts']==result['record_counts'])
        estimated = prepare([obj('a','Automatic client',oid)], [amount('999',False)])
        estimate = store.save_recording(estimated['draft_id'],editor,uuid4().hex)
        check('Estimate is recorded with a warning and no approval',estimate['outcome']=='saved' and bool(estimate['warnings']))
        check('Estimate does not overwrite or enter precise totals',total()=='100' and len(store.detail(oid)['rows'])==2)
        duplicate = prepare([obj('a','Automatic client',oid)],[amount('999',False)])
        check('Repeated estimate reuses the assertion',store.save_recording(duplicate['draft_id'],editor)['record_counts']['facts_added']==0)
        equal_estimate = prepare([obj('a','Automatic client',oid)],[amount('100',False)])
        store.save_recording(equal_estimate['draft_id'],editor)
        check('Estimate equal to exact value cannot degrade it',total()=='100')
        risk = prepare([obj('a','Automatic client',oid)], [amount('200',mode='replace')])
        risk_request = uuid4().hex
        stopped = store.save_recording(risk['draft_id'],editor,risk_request)
        check('Replacement is blocked before canonical writes',stopped['confirmation_required'] and total()=='100')
        approval = store.bind_approval(editor,risk['draft_id'],uuid4().hex)
        try:
            store.save_recording(risk['draft_id'],editor,risk_request,approval['id'])
        except DemoError as error:
            check('Recording permission does not grant sensitive authorization',error.code=='permission_denied')
        else:
            raise AssertionError('Editor performed a replacement')
        store.approve(owner,risk['draft_id'],approval['id'],risk['revision'])
        approved = store.save_recording(risk['draft_id'],owner,risk_request,approval['id'])
        check('Authorized replacement is applied once',approved['outcome']=='saved' and total()=='200')
        check('Authorized replay does not duplicate changes',store.save_recording(risk['draft_id'],owner,risk_request,approval['id'])['idempotent'])
        unknown = prepare([obj('u','Unresolved fixture',certain=False)])
        question = store.save_recording(unknown['draft_id'],editor,uuid4().hex)
        check('Ambiguous identity requires a concrete question before save',question['clarification_required'] and bool(question['questions']) and store.objects()['total']==1)
        cancellation = prepare([obj('a','Automatic client',oid)],updates=[{'ref':'a','field':'name','previous':'Automatic client','value':'Changed name','reason':'fixture correction'}])
        service = Service(store,None,scope=owner)
        state = store.conversation(owner)
        response, changes, _ = service.recording_result(cancellation,state,uuid4().hex)
        store.save_conversation(owner,{**state,**changes,'last_response':response},state['version'])
        asyncio.run(service.cancel_preview(cancellation['draft_id']))
        check('Cancellation leaves the existing record unchanged',store.detail(oid)['object']['name']=='Automatic client')
        expired = prepare([obj('a','Automatic client',oid)],[amount('250',mode='replace')])
        token=store.bind_approval(owner,expired['draft_id'],uuid4().hex)
        store.approve(owner,expired['draft_id'],token['id'],expired['revision'])
        store.write(lambda tx: tx.run('MATCH (a:KDApproval {id:$id}) SET a.expires_at=$now',id=token['id'],now=time.time()-1).consume())
        try:
            store.save_recording(expired['draft_id'],owner,approval_id=token['id'])
        except DemoError as error:
            check('Previously approved token cannot execute after expiry',error.code=='preview_expired' and total()=='200')
        else:
            raise AssertionError('Expired approval executed')
        stale = prepare([obj('a','Automatic client',oid)],[amount('275',mode='replace')])
        token=store.bind_approval(owner,stale['draft_id'],uuid4().hex)
        store.approve(owner,stale['draft_id'],token['id'],stale['revision'])
        newer=prepare([obj('a','Automatic client',oid)],[amount('201',False)])
        store.save_recording(newer['draft_id'],editor)
        try:
            store.save_recording(stale['draft_id'],owner,approval_id=token['id'])
        except DemoError as error:
            check('Data change invalidates a stale risk plan',error.code=='preview_stale' and total()=='200')
        else:
            raise AssertionError('Stale risk plan executed')
        legacy = prepare([obj('b','Previously pending')],scope=owner)
        store.save_pending(legacy['draft_id'],owner,'legacy-request')
        legacy_risk = prepare([obj('a','Automatic client',oid)],[amount('300',mode='replace')],scope=owner)
        old_approval = store.bind_approval(owner,legacy_risk['draft_id'],uuid4().hex)
        store.save_pending(legacy_risk['draft_id'],owner,'legacy-risk')
        preview = store.migrate_recording_policy()
        check('Migration preview does not activate data',not store.detail(legacy['objects'][0]['id'])['object']['confirmed'] and preview['activated_objects']==1)
        migration = store.migrate_recording_policy(True)
        migrated = store.detail(legacy['objects'][0]['id'])['object']
        check('Legacy ordinary record is activated without human review',migrated['confirmed'] and migrated['record_state']=='active' and 'reviewed_by' not in migrated)
        check('Migration preserves original IDs and is idempotent',store.migrate_recording_policy(True)['already_migrated'] and store.objects()['total']==2)
        receipt = store.pending_receipt(owner,'legacy-request')
        check('Legacy receipt follows automatic recording semantics',receipt['outcome']=='saved' and not receipt['saved_pending'])
        check('Migration stages risky facts without replacing current value',total()=='200' and migration['operation_drafts']==1 and all(f['value'].get('value')!='300' for f in store.detail(oid,history=True)['rows']))
        old = store.read(lambda tx: tx.run('MATCH (a:KDApproval {id:$id}) RETURN a.status AS status',id=old_approval['id']).single()['status'])
        check('Migration revokes old approvals',old=='revoked')
        operation = service.operation_card(legacy_risk['draft_id'])
        applied = asyncio.run(service.confirm(legacy_risk['draft_id'],operation['approval_id'],operation['draft']['revision']))
        check('Migrated risk plan executes only with fresh authorization',applied['outcome']=='saved' and total()=='300')
        readonly = replace(editor,permissions=frozenset({'read'}))
        try:
            store.save_recording(legacy['draft_id'],readonly)
        except DemoError as error:
            check('Read-only user cannot record information',error.code=='permission_denied')
        else:
            raise AssertionError('Read-only user recorded information')
        report = {'dataset':store.dataset,'checks':checks,'migration':migration}
        output = Path(__file__).resolve().parent.parent/'artifacts/automatic-recording.json'
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
    finally:
        store.close()


if __name__=='__main__':
    main()
