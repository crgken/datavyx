"""Durable, signed erasure manifests outside the database backup boundary."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from .domain import encoded,digest
from .errors import require,KnowledgeError


class PurgeLedger:
    def __init__(self,settings):self.settings=settings

    def root(self,dataset=None,required=False):
        configured=self.settings.purge_ledger_dir
        if configured:
            root=Path(configured)
            require(root.is_absolute() and not root.is_symlink() and root.is_dir(),
                    'PURGE_LEDGER_UNAVAILABLE','The persistent purge ledger is not ready.',503)
        elif self.settings.mode=='test' and dataset and dataset.startswith('mcp-test-'):
            root=Path(__file__).resolve().parents[1]/'.mcp-artifacts'/'purge-ledger'
            root.mkdir(mode=0o700,parents=True,exist_ok=True)
        else:
            require(not required,'PURGE_LEDGER_UNAVAILABLE','Permanent deletion requires a persistent purge ledger.',503)
            return None
        if os.name!='nt':
            require((os.getuid()==0 or root.stat().st_uid==os.getuid()) and root.stat().st_mode & 0o077==0,
                    'PURGE_LEDGER_UNAVAILABLE','Purge ledger permissions must be owner-only.',503)
        return root

    def path(self,dataset,operation):
        require(len(operation)==64 and all(c in '0123456789abcdef' for c in operation),'INVALID_ARGUMENT','Invalid operation identity.')
        return self.root(dataset,True)/(digest(dataset)+'-'+operation+'.json')

    def signature(self,body):return hmac.new(self.settings.cursor_secret.encode(),encoded(body).encode(),hashlib.sha256).hexdigest()

    def load(self,path):
        require(not path.is_symlink() and path.stat().st_size<=2*1024*1024,'PURGE_LEDGER_INVALID','Invalid purge manifest.',503)
        if os.name!='nt':require((os.getuid()==0 or path.stat().st_uid==os.getuid()) and path.stat().st_mode&0o077==0,'PURGE_LEDGER_INVALID','Purge manifest permissions are invalid.',503)
        try:
            data=json.loads(path.read_text(encoding='utf-8'));body=data['body']
            require(hmac.compare_digest(data['signature'],self.signature(body)) and body['format']==1
                    and body['state'] in {'prepared','committed','aborted'},'PURGE_LEDGER_INVALID','Invalid purge manifest.',503)
            return body
        except (ValueError,KeyError,TypeError):raise KnowledgeError('PURGE_LEDGER_INVALID','Invalid purge manifest.',503) from None

    def _write(self,path,body,exclusive=False):
        content=encoded({'body':body,'signature':self.signature(body)}).encode()
        require(len(content)<=2*1024*1024-1024,'LIMIT_EXCEEDED','Erasure manifest exceeds its durable size limit.',413)
        temporary=path if exclusive else path.with_name(path.name+'.'+uuid4().hex+'.pending')
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
        try:
            fd=os.open(temporary,flags,0o600)
            if os.name!='nt' and os.getuid()==0:os.fchown(fd,path.parent.stat().st_uid,path.parent.stat().st_gid)
            with os.fdopen(fd,'wb') as stream:stream.write(content);stream.flush();os.fsync(stream.fileno())
            if not exclusive:os.replace(temporary,path)
            if os.name!='nt':
                fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
        except OSError:raise KnowledgeError('PURGE_LEDGER_UNAVAILABLE','The purge manifest could not be durably written.',503,True) from None

    def entries(self,dataset=None):
        root=self.root(dataset)
        if root is None:return []
        files=list(root.glob((digest(dataset)+'-' if dataset else '')+'*.json'))
        require(len(files)<=self.settings.max_records,'LIMIT_EXCEEDED','The purge ledger requires maintenance.',503)
        result=[]
        for path in sorted(files):
            body=self.load(path)
            require(path.name==digest(body['dataset'])+'-'+body['operation_id']+'.json' and
                    (dataset is None or body['dataset']==dataset),'PURGE_LEDGER_INVALID','Purge manifest scope mismatch.',503)
            result.append(body)
        return result

    def prepare(self,dataset,operation,resources):
        path=self.path(dataset,operation)
        body={'format':1,'dataset':dataset,'operation_id':operation,'resources':resources,
              'plan_digest':digest(resources),'state':'prepared','created_at':time.time()}
        if path.exists():
            old=self.load(path)
            require(all(old[k]==body[k] for k in ('dataset','operation_id','resources','plan_digest')),
                    'PURGE_LEDGER_CONFLICT','This purge manifest belongs to a different range.',409)
            require(old['state']!='committed','RECOVERY_REQUIRED','Restore the erasure ledger before accepting this operation.',503)
            if old['state']=='aborted':self._write(path,body)
        else:self._write(path,body,True)
        return body

    def confirm(self,dataset,operation):
        path=self.path(dataset,operation)
        if not path.exists():return
        body=self.load(path)
        if body['state']!='committed':self._write(path,{**body,'state':'committed'})

    def check(self,tx,dataset=None,allow_operation=None):
        for body in self.entries(dataset):
            if body['state']=='aborted' or body['state']=='prepared' and body['operation_id']==allow_operation:continue
            row=tx.run('MATCH (p:KDMCPPurgeLedger {dataset:$d,id:$id}) RETURN p.plan_digest AS digest',
                d=body['dataset'],id=body['operation_id']).single()
            require(row and row['digest']==body['plan_digest'],'RECOVERY_REQUIRED',
                    'The database must reconcile its durable erasure manifests before serving this workspace.',503)
            for resource,label in {'entity':'KDObject','fact':'KDFact','source':'KDSource'}.items():
                remaining=tx.run(f'MATCH (n:{label} {{dataset:$d}}) WHERE n.id IN $ids RETURN count(n) AS n',
                    d=body['dataset'],ids=body['resources'][resource]).single()['n']
                require(remaining==0,'RECOVERY_REQUIRED','Restored content must be erased before serving this workspace.',503)

    def reconcile_prepared(self,repo,dataset):
        """Administrative recovery only: require committed proof or no live transaction."""
        for body in self.entries(dataset):
            if body['state']!='prepared':continue
            def outcome(tx):
                row=tx.run('MATCH (p:KDMCPPurgeLedger {dataset:$d,id:$id}) RETURN p.plan_digest AS digest',
                    d=dataset,id=body['operation_id']).single()
                if row:
                    require(row['digest']==body['plan_digest'],'PURGE_LEDGER_CONFLICT','Erasure manifest mismatch.',503)
                    return 'committed'
                require(time.time()-body['created_at']>=120,'RECOVERY_REQUIRED','Erasure outcome is still pending.',503)
                active=tx.run('SHOW TRANSACTIONS YIELD metaData WHERE metaData.mcp_operation=$id RETURN count(*) AS n',
                              id=body['operation_id']).single()['n']
                require(active==0,'RECOVERY_REQUIRED','An erasure transaction is still active.',503)
                again=tx.run('MATCH (p:KDMCPPurgeLedger {dataset:$d,id:$id}) RETURN p.plan_digest AS digest',
                    d=dataset,id=body['operation_id']).single()
                return 'committed' if again else 'aborted'
            state=repo.transaction(outcome)
            self._write(self.path(dataset,body['operation_id']),{**body,'state':state})
