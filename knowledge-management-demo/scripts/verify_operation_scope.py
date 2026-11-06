"""Complete-set regressions on disposable test ports; never uses a live dataset."""
import argparse
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from pydantic_ai import ModelRetry
from pydantic_ai.models.test import TestModel
from demo.agent_runtime import AgentRuntime,validate_operation_outcome
from demo.config import Config,load_model_env
from demo.contracts import ExecutionScope,RequestIntent,TurnOutput
from demo.model import Change,DemoError,encoded
from demo.service import Service
from demo.store import Store


async def main(args):
    if args.model_env:
        load_model_env(args.model_env)
        if args.active_model_env:
            load_model_env(args.active_model_env)
    config=Config(uri='bolt://127.0.0.1:17694',pending_uri='bolt://127.0.0.1:17696',dataset='complete-scope-'+uuid4().hex)
    store=Store(config)
    scope=ExecutionScope(config.dataset,session_id=uuid4().hex,actor='scope-verifier',language='en')
    service=Service(store,None,scope=scope)
    runtime=AgentRuntime(store,None,model=None if args.model_env else TestModel())
    service._runtime=runtime
    checks=[]
    def check(name,condition):
        assert condition,name
        checks.append(name)
        print('PASS '+name,flush=True)
    def rejected(name,fn,codes):
        try:fn()
        except DemoError as error:check(name,error.code in codes)
        else:raise AssertionError(name)
    def seed(count,kind='Client'):
        ids=[]
        for start in range(0,count,40):
            mid=store.message('Synthetic independent records for complete scope testing.',scope.session_id)
            change=Change(schema_version=3,objects=[{'ref':str(i),'kind':kind,'name':f'{kind} {i} {uuid4().hex[:8]}','resolution':'new'} for i in range(start,min(start+40,count))])
            draft=store.prepare_execution_change(change.model_dump(),mid,[mid],scope,store.context()['revision'])
            ids.extend(store.save_recording(draft['draft_id'],scope,uuid4().hex)['entity_refs'])
        return ids
    def selection(action='archive',kind=''):
        return {'scope':'filtered' if kind else 'workspace','action':action,'kind':kind,'namespace':''}
    async def stub_intent(data,**kwargs):
        return SimpleNamespace(output=RequestIntent(kind='change',target_scope='workspace',operation='archive'))
    try:
        store.initialize()
        if not args.model_env:
            runtime.intent_analyzer=SimpleNamespace(run=stub_intent)
        ids=seed(55)
        seed(2,'OtherType')
        # An incomplete legacy operation must not be reused for an all request.
        mid=store.message('Archive this one synthetic record.',scope.session_id)
        raw=Change(schema_version=3,reviews=[{'target_type':'entity','target_id':ids[0],'action':'archive','reason':'Single target','sources':[{'message_id':mid,'quote':'Archive this one synthetic record.'}]}]).model_dump()
        partial=store.prepare_execution_change(raw,mid,[mid],scope,store.context()['revision'])
        state=store.conversation(scope)
        old,changes,_=service.recording_result(partial,state,uuid4().hex)
        store.save_conversation(scope,{**state,**changes},state['version'])
        coverage=store.operation_targets(scope,selection(),store.context()['revision'])
        rejected('Partial proposal cannot claim a complete scope',lambda:store.prepare_execution_change(raw,mid,[mid],scope,coverage['revision'],coverage=coverage),{'operation_scope_incomplete'})
        foreign=deepcopy(coverage);foreign['dataset']='another-space'
        rejected('A scope manifest cannot cross workspaces',lambda:store.prepare_execution_change(raw,mid,[mid],scope,coverage['revision'],coverage=foreign),{'scope_denied'})
        response=await service.chat('Archive all records of the current space.',uuid4().hex)
        draft=response['draft']
        check('All targets beyond lookup and model page limits are included',response['confirmation_required'] and len(draft['reviews'])==57 and draft['scope_coverage']['target_count']==57)
        check('Existing one-target card cannot replace the complete card',draft['draft_id']!=partial['draft_id'] and response['approval_id']!=old['approval_id'])
        check('Complete preview has no business effects before authorization',store.objects(limit=1)['total']==57)
        if args.model_env:
            for text,expected_scope,expected_kind in [
                ('请把当前空间的全部记录存档。','workspace',''),
                ('Archive every record, including records not displayed on this page.','workspace',''),
                ('Archive all Client records in this space.','filtered','Client')]:
                result=await service.chat(text,uuid4().hex)
                manifest=result['draft']['scope_coverage']
                check('Real intent preserves full scope: '+expected_scope+' '+expected_kind,
                      manifest['selection']==selection(kind=expected_kind) and manifest['target_count']==(55 if expected_kind else 57))
        repeated=await service.chat('Archive all records of the current space.',uuid4().hex)
        check('Same complete scope reuses the exact valid authorization',repeated['draft']['draft_id']==draft['draft_id'] and repeated['approval_id']==response['approval_id'])
        # Tamper with the frozen JSON, keeping its independently stored fingerprint.
        saved_payload=store.read(lambda tx:tx.run('MATCH (p:KDDraft {id:$id}) RETURN p.payload AS p',id=draft['draft_id']).single()['p'])
        for name,modify in [
            ('Omitted target is rejected at commit',lambda p:p['reviews'].pop()),
            ('Duplicated target is rejected at commit',lambda p:p['reviews'].append(p['reviews'][0])),
            ('Missing scope proof is rejected at commit',lambda p:p.pop('scope_coverage')),
            ('Changed action is rejected at commit',lambda p:p['reviews'][0].update(action='restore'))]:
            body=json.loads(saved_payload);modify(body)
            store.write(lambda tx:tx.run('MATCH (p:KDDraft {id:$id}) SET p.payload=$p',id=draft['draft_id'],p=encoded(body)).consume())
            approval=store.approve(scope,draft['draft_id'],response['approval_id'],draft['revision'])
            rejected(name,lambda:store.commit(draft['draft_id'],approval['id'],scope),{'operation_scope_incomplete'})
            check(name+' leaves all current records unchanged',store.objects(limit=1)['total']==57)
        store.write(lambda tx:tx.run('MATCH (p:KDDraft {id:$id}) SET p.payload=$p',id=draft['draft_id'],p=saved_payload).consume())
        result=await service.confirm(draft['draft_id'],response['approval_id'],draft['revision'])
        check('Authorized complete operation commits all 57 targets',result['item_count']==57 and store.objects(limit=1)['total']==0)
        again=await service.confirm(draft['draft_id'],response['approval_id'],draft['revision'])
        check('Authorization retries do not apply targets twice',again['idempotent'] and store.objects(limit=1)['total']==0)
        empty=await service.chat('Archive all records of the current space.',uuid4().hex)
        check('Empty scope is verified without an authorization card',empty.get('outcome')=='no_change' and empty['result']['matched_count']==0 and empty['result']['complete'])
        restore=store.operation_targets(scope,selection('restore'),store.context()['revision'])
        check('Restore resolves all archived targets separately from active targets',restore['target_count']==57)
        # A data revision change between preview and commit must reject all work.
        seed(3,'NewRecord')
        stale=await service.chat('Archive all records of the current space.',uuid4().hex)
        seed(1,'AddedAfterPreview')
        try:await service.confirm(stale['draft']['draft_id'],stale['approval_id'],stale['draft']['revision'])
        except DemoError as error:check('New records invalidate the whole frozen preview',error.code=='preview_stale')
        else:raise AssertionError('stale scope executed')
        check('Stale preview applies no partial work',store.objects(limit=1)['total']==4)
        filtered=store.operation_targets(scope,selection(kind='NewRecord'),store.context()['revision'])
        check('Filtered sets include every matching target and exclude other types',filtered['target_count']==3 and set(filtered['target_ids']).isdisjoint(restore['target_ids']))
        async def restore_intent(data,**kwargs):
            return SimpleNamespace(output=RequestIntent(kind='change',target_scope='workspace',operation='restore'))
        if not args.model_env:
            runtime.intent_analyzer=SimpleNamespace(run=restore_intent)
        restored=await service.chat('Restore all archived records in the current space.',uuid4().hex)
        check('Restore preview covers all archived records without touching active ones',restored['draft']['scope_coverage']['target_count']==57 and len(restored['draft']['reviews'])==57)
        result=await service.confirm(restored['draft']['draft_id'],restored['approval_id'],restored['draft']['revision'])
        check('Authorized restore is complete and keeps previously active records',result['item_count']==57 and store.objects(limit=1)['total']==61)
        if args.model_env:
            try:await service.chat('Archive all clients with overdue invoices above 500 dollars.',uuid4().hex)
            except DemoError as error:check('Unsupported filters fail closed instead of silently broadening scope',error.code=='unsupported_operation_scope')
            else:raise AssertionError('Unsupported filter executed')
        # Oversize query returns an explicit failure, never its first page.
        big=Store(replace(config,dataset='oversize-scope-'+uuid4().hex))
        try:
            big.initialize()
            big.write(lambda tx:tx.run('UNWIND range(1,501) AS i CREATE (:KDObject {dataset:$d,id:$prefix+toString(i),archived:false})',d=big.dataset,prefix=uuid4().hex).consume())
            big_scope=replace(scope,dataset=big.dataset)
            rejected('Oversize scope fails instead of truncating',lambda:big.operation_targets(big_scope,selection(),big.context()['revision']),{'operation_scope_too_large'})
        finally:big.close()
        report={'passed':True,'checks':checks,'backend':'real GPT-6 Luna' if args.model_env else 'deterministic intent','targets':57}
        target=Path(__file__).resolve().parent.parent/'artifacts/operation-scope.json'
        target.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
    finally:
        await runtime.close()
        store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-env')
    parser.add_argument('--active-model-env')
    asyncio.run(main(parser.parse_args()))
