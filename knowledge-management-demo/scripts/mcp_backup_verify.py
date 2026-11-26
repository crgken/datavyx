"""Fresh loopback-only disposable Neo4j restore verification; never uses a live database."""
from dataclasses import replace
import json
import os
import subprocess
import time
from uuid import uuid4
from mcp_backend.errors import require,KnowledgeError
from mcp_backend.neo4j_repository import Repository
from mcp_backend.retention_backup import read_backup,restore_isolated
from neo4j.exceptions import Neo4jError,ServiceUnavailable,SessionExpired


def wait_for_database(repo,attempts=45):
    for attempt in range(attempts):
        try:
            repo.driver.verify_connectivity()
            # DBMS Bolt readiness precedes the target database's online transition.
            with repo.driver.session(database=repo.settings.database) as session:session.run('RETURN 1').consume()
            return
        except (Neo4jError,ServiceUnavailable,SessionExpired,OSError):
            if attempt==attempts-1:raise KnowledgeError('VERIFICATION_UNAVAILABLE','Temporary restore database did not become ready.',503) from None
            time.sleep(1)


def verify_backup(store,name,settings):
    require(os.name=='posix','VERIFICATION_UNAVAILABLE','Isolated verification requires the managed Linux host.',503)
    snapshot=read_backup(store._file(name))
    require(snapshot['body']['scope']=='full','BACKUP_INVALID','Scheduled backups must contain the complete graph.',409)
    image='neo4j:5.26-community'
    subprocess.run(['docker','image','inspect',image],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    nonce=uuid4().hex;container='datavyx-backup-verify-'+nonce;repo=None;created=False
    def owned():
        item=json.loads(subprocess.check_output(['docker','inspect',container],stderr=subprocess.DEVNULL))[0]
        require(item['Config']['Labels'].get('datavyx.backup-verification')==nonce
            and item['HostConfig']['PortBindings']=={'7687/tcp':[{'HostIp':'127.0.0.1','HostPort':'17696'}]}
            and not item['HostConfig'].get('Binds') and all(m['Type']=='tmpfs' for m in item.get('Mounts',[])),
            'VERIFICATION_SCOPE_INVALID','The temporary verification container changed ownership or mount scope.',409)
    try:
        # No volumes, production mounts, image pull, external listener or connection to a production driver.
        subprocess.run(['docker','create','--name',container,'--pull=never','--label','datavyx.backup-verification='+nonce,
            '--memory=1g','--cpus=1','--publish','127.0.0.1:17696:7687','--tmpfs','/data:rw,size=512m',
            '--tmpfs','/logs:rw,size=32m','--env','NEO4J_AUTH=none','--env','NEO4J_server_memory_heap_initial__size=256m',
            '--env','NEO4J_server_memory_heap_max__size=256m','--env','NEO4J_server_memory_pagecache_size=64m',image],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        created=True;owned()
        subprocess.run(['docker','start',container],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        isolated=replace(settings,mode='production',uri='bolt://127.0.0.1:17696',password='temporary-verification-only')
        repo=Repository(isolated)
        wait_for_database(repo)
        owned();restore_isolated(repo,snapshot)
        return store.verify_restored(name,repo)
    finally:
        if repo:repo.close()
        if created:
            owned()
            subprocess.run(['docker','rm','--force',container],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
