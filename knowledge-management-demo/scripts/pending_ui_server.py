"""Isolated HTTP acceptance fixture. No live data is read or modified."""
import json
import os
from pathlib import Path
from uuid import uuid4
from demo.config import Config
from demo.contracts import ExecutionScope
from demo.model import Change
from demo.store import Store

dataset='pending-ui-'+uuid4().hex
store=Store(Config(uri='bolt://127.0.0.1:17694',password='atlas-demo-local',pending_uri='bolt://127.0.0.1:17696',dataset=dataset));store.initialize();scope=ExecutionScope(dataset)
def seed(objects,facts,text):
    mid=store.message(text)
    change=Change.model_validate({'objects':objects,'facts':[{**f,'evidence':text,'sources':[{'message_id':mid,'quote':text}]} for f in facts]})
    d=store.prepare_execution_change(change.model_dump(),mid,[mid],scope,store.context()['revision']);return store.save_pending(d['draft_id'],scope)
a=seed([{'ref':'p','name':'沈工','kind':'人员','resolution':'new'},{'ref':'l','name':'南岸实验室','kind':'机构','resolution':'new'}],[{'subject':'p','predicate':'联络机构','object':'l'}],'沈工是南岸实验室的联络人。')
b=seed([{'ref':'b','name':'待撤回样本','kind':'样本','resolution':'new'}],[],'登记一个样本，名称为待撤回样本。')
Path('artifacts/pending-ui-server.json').write_text(json.dumps({'dataset':dataset,'batch_a':a['batch_id'],'batch_b':b['batch_id'],'pid':os.getpid()}),encoding='utf-8');store.close()
os.environ.update(KD_NEO4J_URI='bolt://127.0.0.1:17694',KD_NEO4J_PASSWORD='atlas-demo-local',KD_PENDING_NEO4J_URI='bolt://127.0.0.1:17696',KD_DATASET=dataset,KD_PORT='8097')
from demo.__main__ import run
run()
