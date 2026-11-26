"""Administrative account-close barriers outside database backup boundaries; no MCP tools."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
from uuid import uuid4
from .domain import digest,encoded
from .errors import require,KnowledgeError


class AccountState:
    def __init__(self,settings):self.settings=settings

    def root(self):
        configured=self.settings.purge_ledger_dir
        local_test=self.settings.mode=='test' and self.settings.resource_url.startswith('http://127.0.0.1:')
        if not configured:
            require(local_test,'ACCOUNT_STATE_UNAVAILABLE','Account state is not configured.',503)
            return None
        root=Path(configured).parent/'account-state'
        if local_test and not root.exists() and not root.is_symlink():return None
        require(root.is_absolute() and root.is_dir() and not root.is_symlink(),
            'ACCOUNT_STATE_UNAVAILABLE','The persistent account state is not ready.',503)
        self._private(root)
        key=root/'signing-key'
        require(key.is_file() and not key.is_symlink() and key.stat().st_size==64,
            'ACCOUNT_STATE_UNAVAILABLE','The account-state signing key is unavailable.',503)
        self._private(key)
        return root

    @staticmethod
    def _private(path):
        if os.name!='nt':
            require((os.getuid()==0 or path.stat().st_uid==os.getuid()) and path.stat().st_mode&0o077==0,
                'ACCOUNT_STATE_UNAVAILABLE','Account state must have owner-only permissions.',503)

    @classmethod
    def initialize(cls,settings):
        require(bool(settings.purge_ledger_dir),'ACCOUNT_STATE_UNAVAILABLE','A persistent parent is required.',503)
        parent=Path(settings.purge_ledger_dir).parent
        require(parent.is_absolute() and parent.is_dir() and not parent.is_symlink(),
            'ACCOUNT_STATE_UNAVAILABLE','Invalid persistent account-state parent.',503)
        root=parent/'account-state'
        require(not root.exists(),'ACCOUNT_STATE_EXISTS','Inspect existing state instead of initializing it again.',409)
        root.mkdir(mode=0o700)
        with os.fdopen(os.open(root/'signing-key',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
            stream.write(os.urandom(32).hex());stream.flush();os.fsync(stream.fileno())
        return cls(settings).root()

    def path(self,actor):
        root=self.root();require(root is not None,'ACCOUNT_STATE_UNAVAILABLE','Initialize persistent account state.',503)
        return root/('closed-'+digest(actor)+'.json')

    def signature(self,body):
        key=(self.root()/'signing-key').read_bytes()
        return hmac.new(key,b'AtlasAI account-state v1\0'+encoded(body).encode(),hashlib.sha256).hexdigest()

    def load(self,path):
        require(not path.is_symlink() and path.is_file() and path.stat().st_size<=2*1024*1024,
            'ACCOUNT_STATE_INVALID','Invalid account state.',503)
        self._private(path)
        try:
            data=json.loads(path.read_text(encoding='utf-8'));body=data['body']
            require(hmac.compare_digest(data['signature'],self.signature(body)) and body['format']==1
                and path.name=='closed-'+digest(body['actor'])+'.json'
                and isinstance(body['profiles'],dict) and isinstance(body['verified_case_digest'],str),
                'ACCOUNT_STATE_INVALID','Invalid account-state signature or scope.',503)
            return body
        except (ValueError,KeyError,TypeError):
            raise KnowledgeError('ACCOUNT_STATE_INVALID','Invalid account state.',503) from None

    def write(self,body,exclusive=False):
        path=self.path(body['actor'])
        temporary=path if exclusive else path.with_name(path.name+'.'+uuid4().hex+'.pending')
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
        try:
            fd=os.open(temporary,flags,0o600)
            if os.name!='nt' and os.getuid()==0:os.fchown(fd,path.parent.stat().st_uid,path.parent.stat().st_gid)
            with os.fdopen(fd,'w',encoding='utf-8') as stream:
                stream.write(encoded({'body':body,'signature':self.signature(body)}));stream.flush();os.fsync(stream.fileno())
            if not exclusive:os.replace(temporary,path)
            if os.name!='nt':
                fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
        except OSError:
            raise KnowledgeError('ACCOUNT_STATE_UNAVAILABLE','Could not durably write account state.',503,True) from None

    @contextmanager
    def locked(self):
        root=self.root();require(root is not None,'ACCOUNT_STATE_UNAVAILABLE','Initialize account state.',503)
        path=root/'admin.lock'
        fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            if os.name=='nt':
                import msvcrt
                if os.fstat(fd).st_size==0:os.write(fd,b'0')
                os.lseek(fd,0,0);msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            os.close(fd);raise KnowledgeError('ACCOUNT_ADMIN_BUSY','Another account operation is running.',409,True) from None
        try:yield
        finally:
            if os.name=='nt':os.lseek(fd,0,0);msvcrt.locking(fd,msvcrt.LK_UNLCK,1)
            else:fcntl.flock(fd,fcntl.LOCK_UN)
            os.close(fd)

    def entries(self):
        root=self.root()
        if root is None:return []
        paths=list(root.glob('closed-*.json'))
        require(len(paths)<=self.settings.max_records,'LIMIT_EXCEEDED','Account state needs maintenance.',503)
        return [self.load(path) for path in sorted(paths)]

    def check_actor(self,tx,actor):
        root=self.root()
        if root is not None:
            path=root/('closed-'+digest(actor)+'.json')
            if path.exists() or path.is_symlink():
                self.load(path)
                raise KnowledgeError('ACCOUNT_CLOSED','This Datavyx account is closed.',403)
        row=tx.run('MATCH (c:KDMCPAccountClosure {id:$a}) RETURN c.id AS id',a=actor).single()
        require(not row,'ACCOUNT_CLOSED','This Datavyx account is closed.',403)

    def check_restore(self,tx,bodies=None):
        for body in self.entries() if bodies is None else bodies:
            profile=body['profiles'].get(self.settings.resource_url,{})
            if profile.get('state')!='erased':continue
            actor=body['actor']
            count=tx.run('MATCH (n) WHERE n.dataset IN $datasets OR (n:KDData AND n.id IN $datasets) '
                'OR (n:KDWorkspace AND n.id IN $workspaces) OR (n:KDWorkspaceMember AND n.actor=$actor) '
                'RETURN count(n) AS n',datasets=profile['datasets'],workspaces=profile['workspaces'],actor=actor).single()['n']
            require(count==0,'RECOVERY_REQUIRED','Reapply account-close records before serving a restored database.',503)

    def start(self,actor,case_reference,now=None):
        require(isinstance(actor,str) and len(actor)==64 and all(c in '0123456789abcdef' for c in actor),
            'INVALID_ARGUMENT','Use the trusted identity actor digest, never an email or arbitrary workspace.')
        require(isinstance(case_reference,str) and 8<=len(case_reference)<=200,
            'INVALID_ARGUMENT','An identity-verified support-case reference is required.')
        path=self.path(actor)
        if path.exists():
            body=self.load(path)
            require(body['verified_case_digest']==digest(case_reference),'ACCOUNT_STATE_CONFLICT','Another verified case owns this close operation.',409)
            return body
        now=time.time() if now is None else now
        body={'format':1,'actor':actor,'verified_case_digest':digest(case_reference),'verified_at':now,
            'active_data_due':now+30*86400,'backup_data_due':now+60*86400,'profiles':{},
            'provider_tasks':{'auth0':{'state':'pending'},'legacy_copies':{'state':'pending'}}}
        self.write(body,exclusive=True)
        return body
