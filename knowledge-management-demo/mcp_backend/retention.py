"""Signed backup inventory and support-case deadlines. Not part of the MCP registry."""
import calendar
from datetime import datetime,timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4
from .account_state import AccountState
from .domain import digest,encoded
from .errors import require,KnowledgeError
from .retention_backup import export_graph,graph_fingerprint,read_backup

LEGACY_BUNDLES=frozenset({'workspace-migration-20261026','recording-0252dfe-20261104T070345Z',
    'mcp-d1ed5d3-20261112T102743Z','mcp-self-service-20261124T000653Z-87GJtl'})


def support_due(closed_at):
    value=datetime.fromtimestamp(closed_at,timezone.utc)
    return value.replace(year=value.year+1,day=min(value.day,calendar.monthrange(value.year+1,value.month)[1])).timestamp()


def file_sha(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


class Retention:
    def __init__(self,settings,backup_root):
        self.state=AccountState(settings);self.backup_root=Path(backup_root)
        require(self.backup_root.is_absolute() and self.backup_root.is_dir() and not self.backup_root.is_symlink()
            and self.backup_root.resolve()==self.backup_root,'BACKUP_PATH_INVALID','Use the canonical backup root.',409)
        self.path=self.state.root()/'retention.json'

    def initialize(self):
        with self.state.locked():
            require(not self.path.exists(),'RETENTION_EXISTS','Inspect the existing retention inventory.',409)
            self._save({'format':1,'backup_root':str(self.backup_root),'backups':{},'legacy':{},'tickets':{}})

    def _read(self):
        require(self.path.is_file() and not self.path.is_symlink() and self.path.stat().st_size<=16*1024*1024,
            'RETENTION_UNAVAILABLE','Initialize or recover the signed retention inventory.',503)
        self.state._private(self.path)
        try:
            document=json.loads(self.path.read_text(encoding='utf-8'));body=document['body']
            require(body['format']==1 and body['backup_root']==str(self.backup_root)
                and hmac.compare_digest(document['signature'],self.state.signature(body)),
                'RETENTION_INVALID','Retention inventory signature or root mismatch.',503)
            return body
        except (ValueError,KeyError,TypeError):raise KnowledgeError('RETENTION_INVALID','Invalid retention inventory.',503) from None

    def _save(self,body):
        require(len(encoded(body).encode())<=16*1024*1024,'LIMIT_EXCEEDED','Retention inventory requires bounded maintenance.',413)
        pending=self.path.with_name('retention-'+uuid4().hex+'.pending')
        fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        if os.name!='nt' and os.getuid()==0:os.fchown(fd,self.path.parent.stat().st_uid,self.path.parent.stat().st_gid)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(encoded({'body':body,'signature':self.state.signature(body)}));stream.flush();os.fsync(stream.fileno())
        os.replace(pending,self.path)
        if os.name!='nt':
            fd=os.open(self.path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)

    def _file(self,name):
        require(isinstance(name,str) and name not in {'','.','..'} and Path(name).name==name
            and '/' not in name and '\\' not in name,'BACKUP_PATH_INVALID','Only direct registered backup files are eligible.',409)
        path=self.backup_root/name
        require(not path.is_symlink() and path.resolve().parent==self.backup_root,
            'BACKUP_PATH_INVALID','Linked or out-of-root backups are forbidden.',409)
        return path

    def register_backup(self,name,now=None):
        now=time.time() if now is None else now
        with self.state.locked():
            body=self._read();path=self._file(name);snapshot=read_backup(path)
            created=snapshot['body']['created_at']
            require(0<=created<=now+300,'BACKUP_INVALID','Backup creation time is invalid.',409)
            row={'sha256':file_sha(path),'created_at':created,'expires_at':created+30*86400,
                'profile':snapshot['body']['profile'],'graph':graph_fingerprint(snapshot['body']),'state':'available'}
            previous=body['backups'].get(name)
            require(previous is None or previous['sha256']==row['sha256'],
                'BACKUP_CHANGED','An inventoried backup is immutable.',409)
            if previous:return previous
            body['backups'][name]=row;self._save(body);return row

    def verify_restored(self,name,isolated_repo,now=None):
        require(isolated_repo.settings.uri in {'bolt://127.0.0.1:17694','bolt://127.0.0.1:17696'},
            'PERMISSION_DENIED','Backup proof requires an isolated restore.',403)
        with self.state.locked():
            body=self._read();row=body['backups'].get(name);path=self._file(name)
            require(row and row['state']=='available' and file_sha(path)==row['sha256'],
                'BACKUP_CHANGED','Backup inventory or hash mismatch.',409)
            snapshot=read_backup(path)
            restored=export_graph(isolated_repo,snapshot['body']['datasets'] if snapshot['body']['scope']=='synthetic' else None)
            require(graph_fingerprint(restored['body'])==row['graph'],'RESTORE_MISMATCH','Restored graph differs from its backup.',409)
            row['verified_at']=time.time() if now is None else now
            row['verification']='isolated-restore-fingerprint';self._save(body);return row

    def rotate(self,now=None,apply=False,profile=None):
        now=time.time() if now is None else now
        with self.state.locked():
            body=self._read();available={name:row for name,row in body['backups'].items()
                if row['state']=='available' and (profile is None or row['profile']==profile)}
            interrupted={name:row for name,row in body['backups'].items()
                if row['state']=='expiring' and (profile is None or row['profile']==profile)}
            expired=[name for name,row in available.items() if row['expires_at']<=now]+list(interrupted)
            # Preflight the complete inventory before deleting anything; missing or changed files fail closed.
            for name,row in available.items():
                path=self._file(name)
                require(path.is_file() and file_sha(path)==row['sha256'],'BACKUP_CHANGED','Registered backup changed or disappeared.',409)
            for name,row in interrupted.items():
                path=self._file(name)
                require(row['expires_at']<=now and (not path.exists() or path.is_file() and file_sha(path)==row['sha256']),
                    'BACKUP_CHANGED','Interrupted backup disposal changed scope.',409)
            successors={name for name,row in available.items() if name not in expired
                and row.get('verification')=='isolated-restore-fingerprint' and row.get('verified_at',0)>=now-36*3600
                and row['created_at']>=now-36*3600}
            successor_profiles={available[name]['profile'] for name in successors}
            require(all(body['backups'][name]['profile'] in successor_profiles for name in expired),
                'BACKUP_SUCCESSOR_REQUIRED','Verify a fresh recoverable backup for each expired profile before rotation.',409)
            if apply:
                for name in expired:
                    body['backups'][name]['state']='expiring';self._save(body)
                    path=self._file(name)
                    if path.exists():path.unlink()
                    body['backups'][name]['state']='expired';body['backups'][name]['disposed_at']=now
                    self._save(body)
            return {'expired':sorted(expired),'verified_successors':sorted(successors),'applied':apply}

    def close_ticket(self,case_reference,message_ids,closed_at):
        require(isinstance(message_ids,list) and 0<len(message_ids)<=100 and all(isinstance(i,str) and 0<len(i)<=200 for i in message_ids)
            and len(set(message_ids))==len(message_ids),'INVALID_ARGUMENT','Register explicit message IDs for a closed support case.')
        require(isinstance(case_reference,str) and 8<=len(case_reference)<=200 and 0<closed_at<=time.time(),
            'INVALID_ARGUMENT','Use a verified closed case and actual closure time.')
        with self.state.locked():
            body=self._read();key=digest(case_reference)
            value={'message_ids':message_ids,'closed_at':closed_at,'expires_at':support_due(closed_at),'state':'closed'}
            require(key not in body['tickets'] or body['tickets'][key]==value,'TICKET_CONFLICT','Existing case closure differs.',409)
            body['tickets'][key]=value;self._save(body);return {'case_digest':key,**value}

    def due_tickets(self,now=None):
        now=time.time() if now is None else now
        # An action queue, not a fabricated claim that an unconnected mailbox was cleaned.
        return [{'case_digest':key,**row} for key,row in self._read()['tickets'].items()
            if row['state']=='closed' and row['expires_at']<=now]

    def complete_ticket(self,case_digest,provider_receipt,now=None):
        now=time.time() if now is None else now
        require(isinstance(provider_receipt,str) and 8<=len(provider_receipt)<=1000,
            'INVALID_ARGUMENT','Record the actual mailbox disposal receipt.')
        with self.state.locked():
            body=self._read();row=body['tickets'].get(case_digest)
            require(row and row['state']=='closed' and row['expires_at']<=now,'TICKET_NOT_DUE','Only a due, closed case may be completed.',409)
            body['tickets'][case_digest]={'state':'disposed','disposed_at':now,'receipt_digest':digest(provider_receipt)}
            self._save(body)

    def _legacy(self,name):
        require(name in LEGACY_BUNDLES,'BACKUP_PATH_INVALID','Only explicitly inventoried historical backup bundles are eligible.',409)
        path=self.backup_root.parent/name
        require(not path.is_symlink() and path.resolve().parent==self.backup_root.parent and path!=self.backup_root,
            'BACKUP_PATH_INVALID','Historical backup is linked or outside its fixed parent.',409)
        return path

    def _tree(self,path):
        require(path.is_dir(),'BACKUP_CHANGED','Historical backup directory is missing.',409)
        entries=[];total=0;modified=path.stat().st_mtime
        for parent,dirs,files in os.walk(path,followlinks=False):
            for name in sorted(dirs+files):
                item=Path(parent)/name
                require(not item.is_symlink() and item.resolve().is_relative_to(path),
                    'BACKUP_PATH_INVALID','Linked entries in a historical backup are forbidden.',409)
                require(item.is_file() or item.is_dir(),'BACKUP_PATH_INVALID','Special backup entries are forbidden.',409)
                modified=max(modified,item.stat().st_mtime)
                if item.is_file():
                    total+=item.stat().st_size;require(total<=2*1024**3,'LIMIT_EXCEEDED','Historical inventory exceeds 2GiB.',413)
                    entries.append([item.relative_to(path).as_posix(),item.stat().st_size,file_sha(item)])
                else:entries.append([item.relative_to(path).as_posix()+'/'])
                require(len(entries)<=100000,'LIMIT_EXCEEDED','Historical inventory exceeds the entry limit.',413)
        return {'tree_sha256':digest(sorted(entries)),'entries':len(entries),'bytes':total,'modified_at':modified}

    def register_legacy(self,name,profiles,now=None):
        now=time.time() if now is None else now
        require(isinstance(profiles,list) and profiles and all(isinstance(p,str) and p for p in profiles),
            'INVALID_ARGUMENT','Specify every required successor backup profile.')
        with self.state.locked():
            body=self._read();tree=self._tree(self._legacy(name))
            require(tree['modified_at']<=now+300,'BACKUP_CHANGED','Historical backup has a future timestamp.',409)
            row={**tree,'expires_at':tree['modified_at']+30*86400,'profiles':sorted(set(profiles)),'state':'available'}
            previous=body['legacy'].get(name)
            require(previous is None or previous==row,'BACKUP_CHANGED','Historical backup inventory changed.',409)
            body['legacy'][name]=row;self._save(body);return row

    def rotate_legacy(self,now=None,apply=False):
        now=time.time() if now is None else now
        with self.state.locked():
            body=self._read();expired={name:row for name,row in body['legacy'].items()
                if row['state'] in {'available','expiring'} and row['expires_at']<=now}
            for name,row in expired.items():
                path=self._legacy(name)
                if row['state']=='expiring' and not path.exists():continue
                actual=self._tree(path)
                # A partially removed tree after an interruption needs operator inspection, never wider deletion.
                require(actual['tree_sha256']==row['tree_sha256'],'BACKUP_CHANGED','Historical backup contents changed; no deletion applied.',409)
            successors=set()
            for name,row in body['backups'].items():
                if row['state']=='available' and row.get('verification')=='isolated-restore-fingerprint' \
                    and row['created_at']>=now-36*3600 and row.get('verified_at',0)>=now-36*3600:
                    path=self._file(name)
                    require(path.is_file() and file_sha(path)==row['sha256'],'BACKUP_CHANGED','Successor backup changed.',409)
                    successors.add(row['profile'])
            require(all(set(row['profiles'])<=successors for row in expired.values()),
                'BACKUP_SUCCESSOR_REQUIRED','Restore-verify every replacement profile before historical disposal.',409)
            if apply:
                for name,row in expired.items():
                    path=self._legacy(name);row['state']='expiring';self._save(body)
                    if path.exists():
                        # The resolved absolute target is a known direct child, never an active DB, key or root.
                        require(path.resolve().parent==self.backup_root.parent and path.resolve()!=self.backup_root,
                            'BACKUP_PATH_INVALID','Historical disposal scope changed.',409)
                        shutil.rmtree(path)
                    row['state']='expired';row['disposed_at']=now;self._save(body)
            return {'expired':sorted(expired),'applied':apply}
