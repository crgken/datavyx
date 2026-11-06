"""Replay one real conversation using candidate code; roll back all business writes.

Run a committed candidate from a separate staging directory with the installed
runtime. Refuse in-flight requests. Never confirm a proposed business operation.
Reports contain only counts, statuses and fingerprints, never message contents.
"""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from demo.config import Config,load_model_env
from demo.contracts import ExecutionScope
from demo.diagnostics import log_failure
from demo.graphiti_extension import Pipeline
from demo.mobile_auth import ROLES
from demo.service import Service
from demo.store import Store

LABELS=['KDData','KDObject','KDFact','KDSource','KDDraft','KDApproval','KDMutation',
        'KDBatch','KDPendingRequest','KDConversation','KDMessage','KDQuery',
        'KDMobileConversation','KDMobileRequest','KDIndexHead']


def snapshot(store):
    with store.driver.session(database=store.config.database) as session:
        nodes=[r.data() for r in session.run('MATCH (n) WHERE (n.dataset=$d OR (n:KDData AND n.id=$d)) AND any(l IN labels(n) WHERE l IN $labels) RETURN elementId(n) AS id,labels(n) AS labels,properties(n) AS p ORDER BY id',d=store.dataset,labels=LABELS)]
        edges=[r.data() for r in session.run('MATCH (a)-[r]->(b) WHERE a.dataset=$d AND b.dataset=$d AND any(l IN labels(a) WHERE l IN $labels) AND any(l IN labels(b) WHERE l IN $labels) RETURN elementId(r) AS id,elementId(a) AS a,elementId(b) AS b,type(r) AS type,properties(r) AS p ORDER BY id',d=store.dataset,labels=LABELS)]
        raw=json.dumps({'nodes':nodes,'edges':edges},sort_keys=True,default=str).encode()
        return {'sha256':hashlib.sha256(raw).hexdigest(),'nodes':len(nodes),'relationships':len(edges)}


class ReplayStore(Store):
    tx=None
    def __init__(self,config):
        super().__init__(config)
        self.replay_lock=threading.RLock()
    def write(self,fn):
        with self.replay_lock:
            assert self.tx is not None,'replay_transaction_required'
            return fn(self.tx)
    read=write


async def main(args):
    load_model_env(args.model_env)
    load_dotenv(args.env)
    os.environ['GRAPHITI_TELEMETRY_ENABLED']='false'
    base=Store(Config.from_env())
    pipeline=None
    service=None
    replay=None
    report={'business_writes_committed':False,'passed':False}
    started=time.monotonic()
    try:
        with base.driver.session(database=base.config.database) as session:
            assert session.run("MATCH (j:KDMobileRequest) WHERE j.status IN ['running','queued'] RETURN count(j) AS n").single()['n']==0,'in_flight_requests'
            row=session.run('MATCH (c:KDMobileConversation {id:$id}) RETURN c.dataset AS dataset,c.actor AS actor',id=args.conversation).single()
            assert row,'conversation_not_found'
            dataset,actor=row['dataset'],row['actor']
            member=session.run('MATCH (m:KDWorkspaceMember {dataset:$d,actor:$a,enabled:true}) RETURN m.role AS role LIMIT 1',d=dataset,a=actor).single()
            assert member and 'propose' in ROLES[member['role']],'scope_permission_required'
        config=replace(base.config,dataset=dataset)
        scoped=base.for_dataset(dataset)
        scope=ExecutionScope(dataset,session_id=args.conversation,actor=actor,permissions=frozenset(ROLES[member['role']]),language='en')
        assert scoped.operation_context(scope)['active_total']==0,'existing_active_operation_requires_review'
        messages=scoped.messages(args.conversation)
        current=next(m for m in reversed(messages) if m['role']=='user')
        history_entries=len(scoped.conversation(scope)['history'])
        pipeline=Pipeline(config)
        # Warm the same existing semantic clients before opening the rollback
        # transaction; index initialization is idempotent, not a business write.
        await pipeline.graphiti()
        await pipeline.pending.graphiti()
        before=snapshot(scoped)
        replay=ReplayStore(config)
        service=Service(replay,pipeline,scope=scope)
        try:
            with replay.driver.session(database=config.database) as session:
                with session.begin_transaction(timeout=180) as tx:
                    replay.tx=tx
                    try:
                        targets={r['id'] for r in tx.run("MATCH (o:KDObject {dataset:$d}) WHERE NOT coalesce(o.archived,false) AND coalesce(o.record_state,'active')<>'action_draft' RETURN o.id AS id",d=dataset)}
                        result=await service.chat(current['text'],uuid4().hex)
                        reviews=(result.get('draft') or {}).get('reviews',[])
                        proposed={op['target_id'] for op in reviews if op['target_type']=='entity' and op['action']=='archive'}
                        still_active={r['id'] for r in tx.run('MATCH (o:KDObject {dataset:$d}) WHERE NOT coalesce(o.archived,false) RETURN o.id AS id',d=dataset)}
                        state=replay.conversation(scope)
                        report.update(history_entries=history_entries,outcome=result.get('outcome'),
                            confirmation_required=result.get('confirmation_required',False),
                            new_authorization=bool(result.get('approval_id')),
                            target_count=len(proposed),current_record_count=len(targets),
                            all_current_records_covered=proposed==targets,
                            archive_not_applied=still_active==targets,
                            tools=[entry['tool'] for entry in state.get('last_tool_log',[])])
                    finally:
                        tx.rollback()
                        replay.tx=None
        finally:
            after=snapshot(scoped)
            report.update(before=before,after=after,persistent_state_unchanged=before==after)
        report['passed']=bool(report.get('confirmation_required') and report.get('new_authorization') and
            report.get('all_current_records_covered') and report.get('target_count') and
            report.get('archive_not_applied') and report['persistent_state_unchanged'])
    except Exception as error:
        report['error_type']=type(error).__name__
        log_failure('conversation_rollback_replay',error)
    finally:
        if service and service._runtime:
            await service._runtime.close()
        if pipeline:
            await pipeline.close()
        if replay:
            replay.close()
        base.close()
        report['elapsed_seconds']=round(time.monotonic()-started,3)
        target=Path(args.report)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(report,indent=2),encoding='utf-8')
        os.chmod(target,0o600)
        print(json.dumps(report),flush=True)
    return 0 if report['passed'] else 2


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',required=True)
    parser.add_argument('--model-env',required=True)
    parser.add_argument('--conversation',required=True)
    parser.add_argument('--report',required=True)
    sys.exit(asyncio.run(main(parser.parse_args())))
