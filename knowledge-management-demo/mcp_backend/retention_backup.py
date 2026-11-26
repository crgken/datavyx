"""Bounded consistent logical backup and isolated restore; no production restore entry point."""
import gzip
import json
import os
from pathlib import Path
import re
import time
from .domain import digest,encoded
from .errors import require
from neo4j.time import DateTime,Date,Time,Duration
from neo4j.spatial import Point,CartesianPoint,WGS84Point


def pack_value(value):
    for kind in (DateTime,Date,Time,Duration):
        if isinstance(value,kind):
            result={'neo4j_type':kind.__name__,'value':value.iso_format()}
            if kind is DateTime and getattr(value.tzinfo,'zone',None):result['zone']=value.tzinfo.zone
            return result
    if isinstance(value,Point):return {'neo4j_type':'Point','srid':value.srid,'value':list(value)}
    if isinstance(value,(bytes,bytearray)):return {'neo4j_type':'Bytes','value':bytes(value).hex()}
    if isinstance(value,(list,tuple)):return [pack_value(v) for v in value]
    require(value is None or isinstance(value,(str,bool,int,float)),
        'BACKUP_TYPE_UNSUPPORTED','Backup contains an unsupported property type.',409)
    return value


def unpack_value(value):
    if isinstance(value,list):return [unpack_value(v) for v in value]
    if isinstance(value,dict):
        kind=value.get('neo4j_type');types={'DateTime':DateTime,'Date':Date,'Time':Time,'Duration':Duration}
        if kind in types:
            result=types[kind].from_iso_format(value['value'])
            if kind=='DateTime' and 'zone' in value:
                import pytz
                result=result.as_timezone(pytz.timezone(value['zone']))
            return result
        if kind=='Bytes':return bytearray.fromhex(value['value'])
        if kind=='Point':
            require(value['srid'] in {4326,4979,7203,9157},'BACKUP_INVALID','Unknown point coordinate system.',409)
            return (WGS84Point if value['srid'] in {4326,4979} else CartesianPoint)(value['value'])
        require(False,'BACKUP_INVALID','Unknown encoded property type.',409)
    return value

TOKEN=re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')


def export_graph(repo,datasets=None,now=None,quiescent_legacy=False):
    if datasets is not None:
        require(repo.settings.mode=='test' and all(d.startswith('mcp-test-') for d in datasets),
            'PERMISSION_DENIED','Partial backup selection is only for synthetic test datasets.',403)
    def capture(tx):
        verification=repo.settings.uri=='bolt://127.0.0.1:17696'
        if quiescent_legacy:
            require(repo.settings.uri=='bolt://127.0.0.1:17695','PERMISSION_DENIED','Quiescent capture is only for the stopped legacy semantic writer.',403)
        elif not verification:repo.metadata_lock(tx)
        available=[r['d'] for r in tx.run('MATCH (d:KDData) RETURN d.id AS d ORDER BY d.id')]
        selected=available if datasets is None else sorted(set(datasets))
        require(set(selected)<=set(available) and len(selected)<=repo.settings.max_records,
            'BACKUP_SCOPE_INVALID','Unknown or excessive backup dataset selection.',409)
        if not quiescent_legacy and not verification:
            for dataset in selected:repo.revision(tx,dataset,write=True)
        scope='' if datasets is None else 'WHERE n.dataset IN $d OR (n:KDData AND n.id IN $d)'
        nodes=[dict(r) for r in tx.run('MATCH (n) '+scope+' RETURN elementId(n) AS ref,labels(n) AS labels,'
            'properties(n) AS properties ORDER BY ref LIMIT $limit',d=selected,limit=20*repo.settings.max_records+1)]
        require(len(nodes)<=20*repo.settings.max_records,'LIMIT_EXCEEDED','Backup node limit exceeded.',413)
        ids=[n['ref'] for n in nodes]
        edges=[dict(r) for r in tx.run('MATCH (a)-[r]->(b) WHERE elementId(a) IN $ids AND elementId(b) IN $ids '
            'RETURN elementId(a) AS source,elementId(b) AS target,type(r) AS type,properties(r) AS properties '
            'ORDER BY source,target,type LIMIT $limit',ids=ids,limit=40*repo.settings.max_records+1)]
        require(len(edges)<=40*repo.settings.max_records,'LIMIT_EXCEEDED','Backup relationship limit exceeded.',413)
        for item in nodes+edges:item['properties']={key:pack_value(value) for key,value in item['properties'].items()}
        require(all(TOKEN.fullmatch(label) for n in nodes for label in n['labels'])
            and all(TOKEN.fullmatch(e['type']) for e in edges),'BACKUP_TYPE_UNSUPPORTED','Unsupported schema name in backup.',409)
        body={'format':1,'profile':repo.settings.resource_url,'created_at':time.time() if now is None else now,
            'datasets':selected,'scope':'full' if datasets is None else 'synthetic','nodes':nodes,'edges':edges}
        require(len(encoded(body).encode())<=128*1024*1024,'LIMIT_EXCEEDED','Backup size limit exceeded.',413)
        return {'body':body,'sha256':digest(body)}
    return repo.transaction(capture)


def write_backup(path,snapshot):
    path=Path(path)
    require(path.parent.is_dir() and not path.is_symlink() and not path.exists(),
        'BACKUP_PATH_INVALID','Backup needs an unused regular-file path.',409)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600),'wb') as target:
        with gzip.GzipFile(fileobj=target,mode='wb',mtime=0) as stream:stream.write(encoded(snapshot).encode())
        target.flush();os.fsync(target.fileno())
    if os.name!='nt':
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


def read_backup(path):
    path=Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size<=128*1024*1024,
        'BACKUP_INVALID','Invalid backup file.',409)
    with gzip.open(path,'rb') as stream:raw=stream.read(128*1024*1024+1)
    require(len(raw)<=128*1024*1024,'BACKUP_INVALID','Expanded backup exceeds limit.',413)
    data=json.loads(raw);body=data['body']
    require(body['format']==1 and data['sha256']==digest(body),'BACKUP_INVALID','Backup checksum mismatch.',409)
    ids={n['ref'] for n in body['nodes']}
    require(len(ids)==len(body['nodes']) and all(TOKEN.fullmatch(label) for n in body['nodes'] for label in n['labels'])
        and all(e['source'] in ids and e['target'] in ids and TOKEN.fullmatch(e['type']) for e in body['edges']),
        'BACKUP_INVALID','Invalid or cross-boundary backup graph.',409)
    return data


def graph_fingerprint(body):
    """Independent of storage element IDs/order while preserving labels, data and all edges."""
    nodes={n['ref']:digest({'labels':sorted(n['labels']),'properties':n['properties']}) for n in body['nodes']}
    return digest({'nodes':sorted(nodes.values()),'edges':sorted(digest({'source':nodes[e['source']],
        'target':nodes[e['target']],'type':e['type'],'properties':e['properties']}) for e in body['edges'])})


def restore_isolated(repo,snapshot):
    require(repo.settings.mode=='test' and repo.settings.uri=='bolt://127.0.0.1:17694'
        or repo.settings.uri=='bolt://127.0.0.1:17696',
        'PERMISSION_DENIED','Restore is restricted to an isolated verification database.',403)
    body=snapshot['body'];require(snapshot['sha256']==digest(body),'BACKUP_INVALID','Backup checksum mismatch.',409)
    if body['scope']=='synthetic':
        require(all(d.startswith('mcp-test-') for d in body['datasets']),
            'BACKUP_INVALID','Partial restore contains non-synthetic data.',409)
    def restore(tx):
        occupied=tx.run('MATCH (n) '+('WHERE n.dataset IN $d OR (n:KDData AND n.id IN $d)' if body['scope']=='synthetic' else '')+
            ' RETURN count(n) AS n',d=body['datasets']).single()['n']
        require(occupied==0,'RESTORE_TARGET_NOT_EMPTY','Never overwrite an occupied restore target.',409)
        refs={}
        for node in body['nodes']:
            require(node['labels'] and all(TOKEN.fullmatch(label) for label in node['labels']),
                'BACKUP_INVALID','Invalid node labels.',409)
            if body['scope']=='synthetic':
                props=node['properties']
                require(props.get('dataset') in body['datasets'] or 'KDData' in node['labels'] and props.get('id') in body['datasets'],
                    'BACKUP_INVALID','Node is outside the synthetic scope.',409)
                for label in node['labels']:
                    if 'id' in props:
                        clash=tx.run(f'MATCH (n:{label} {{id:$id}}) RETURN count(n) AS n',id=props['id']).single()['n']
                        require(not clash,'RESTORE_ID_CONFLICT','An existing resource uses this ID.',409)
            refs[node['ref']]=tx.run('CREATE (n:'+':'.join(node['labels'])+') SET n=$p RETURN elementId(n) AS ref',
                p={key:unpack_value(value) for key,value in node['properties'].items()}).single()['ref']
        for edge in body['edges']:
            require(edge['source'] in refs and edge['target'] in refs and TOKEN.fullmatch(edge['type']),
                'BACKUP_INVALID','Invalid relationship.',409)
            tx.run('MATCH (a),(b) WHERE elementId(a)=$a AND elementId(b)=$b '
                f'CREATE (a)-[r:{edge["type"]}]->(b) SET r=$p',a=refs[edge['source']],b=refs[edge['target']],
                p={key:unpack_value(value) for key,value in edge['properties'].items()}).consume()
        return {'nodes':len(refs),'edges':len(body['edges'])}
    return repo.transaction(restore)
