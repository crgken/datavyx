"""Real model cancellation/reissue regression on disposable test ports only."""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
from uuid import uuid4
from pydantic_ai.messages import ModelResponse,TextPart
from pydantic_core import to_jsonable_python
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from demo.config import Config,load_model_env
from demo.contracts import ExecutionScope
from demo.graphiti_extension import Pipeline
from demo.model import Change,DemoError
from demo.service import Service
from demo.store import Store


async def main(args):
    load_model_env(args.model_env)
    if args.active_model_env:
        load_model_env(args.active_model_env)
    config=Config(uri='bolt://127.0.0.1:17694',pending_uri='bolt://127.0.0.1:17696',dataset='cancelled-operation-'+uuid4().hex)
    store=Store(config)
    pipeline=Pipeline(config)
    scope=ExecutionScope(config.dataset,actor='cancel-verifier',language='en')
    service=Service(store,pipeline,scope=scope)
    checks=[]
    def check(name,passed):
        assert passed,name
        checks.append(name)
        print('PASS '+name,flush=True)
    try:
        store.initialize()
        text='Client Test Alpha, client Test Beta, and client Test Gamma are three separate test clients.'
        mid=store.message(text,scope.session_id)
        change=Change.model_validate({'schema_version':3,'objects':[{'ref':str(i),'kind':'Client','name':name,'resolution':'new'} for i,name in enumerate(['Test Alpha','Test Beta','Test Gamma'])]})
        draft=store.prepare_execution_change(change.model_dump(),mid,[mid],scope,store.context()['revision'])
        store.save_recording(draft['draft_id'],scope,uuid4().hex)
        first=await service.chat('Archive all records of the current space.',uuid4().hex)
        check('Real model creates the initial archive operation',first.get('confirmation_required') and len(first['draft']['reviews'])==3)
        old=first['draft']
        legacy_history=[*store.conversation(scope)['history'],*to_jsonable_python([
            ModelResponse(parts=[TextPart(content='The current-space archive request is already covered by the pending archive review for all 3 records. Please use that review to confirm the archive; I have not created a duplicate operation.')]),
        ])]
        store.write(lambda tx:tx.run('MATCH (p:KDDraft {id:$id}) SET p.expires_at=$expires WITH p MATCH (a:KDApproval {draft_id:p.id}) SET a.expires_at=$expires',id=old['draft_id'],expires=time.time()-1).consume())
        check('Expired operations are excluded from the active snapshot',store.operation_context(scope)['active_total']==0)
        await service.cancel_preview(old['draft_id'])
        state=store.conversation(scope)
        check('Cancel leaves no active operation or authorization',state['pending'] is None and state['approval'] is None and store.operation_context(scope)['active_total']==0)
        # Reproduce the already-cancelled production conversation: its old model
        # history has no cancellation event, while the database has closed it.
        store.save_conversation(scope,{**state,'history':legacy_history},state['version'])
        second=await service.chat('Archive all records of the current space.',uuid4().hex)
        check('Reissue creates a fresh operation despite stale model history',second.get('confirmation_required') and second['draft']['draft_id']!=old['draft_id'])
        check('Reissue has a fresh unexpired authorization',second['approval_id']!=first['approval_id'] and second['draft']['expires_at']>time.time() and store.operation_context(scope)['active_total']==1)
        check('Reissue does not apply archive without authorization',store.objects()['total']==3 and store.query({'operation':'COUNT','kind':'Client'})['count']==3)
        check('Another actor cannot see these operation states',store.operation_context(replace(scope,actor='another-actor'))['active_total']==0)
        existing=await service.chat('Show me the existing pending archive review.',uuid4().hex)
        check('Existing valid review is returned as an actionable card',existing.get('confirmation_required') and existing['draft']['draft_id']==second['draft']['draft_id'] and existing['approval_id']==second['approval_id'])
        try:
            await service.confirm(old['draft_id'],first['approval_id'],old['revision'])
        except DemoError as error:
            check('Old expired authorization cannot execute',error.code in {'preview_expired','draft_closed','approval_required'})
        else:
            raise AssertionError('Old authorization executed')
        empty=Store(replace(config,dataset='empty-operation-'+uuid4().hex))
        empty_service=Service(empty,pipeline,scope=replace(scope,dataset=empty.dataset))
        try:
            empty.initialize()
            no_change=await empty_service.chat('Archive all records of the current space.',uuid4().hex)
            check('Empty scope returns verified no-change evidence',no_change.get('outcome')=='no_change' and no_change['result']['complete'] and no_change['result']['matched_count']==0)
        finally:
            if empty_service._runtime:
                await empty_service._runtime.close()
            empty.close()
        report={'passed':True,'checks':checks,'scope':'real GPT-6 Luna backend on isolated test databases'}
        target=Path(__file__).resolve().parent.parent/'artifacts/cancelled-operation.json'
        target.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report),flush=True)
    finally:
        if service._runtime:
            await service._runtime.close()
        await pipeline.close()
        store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-env',required=True)
    parser.add_argument('--active-model-env')
    asyncio.run(main(parser.parse_args()))
