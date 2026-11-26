from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
import tempfile
import time
from uuid import uuid4
from mcp_backend.settings import Settings
from mcp_backend.neo4j_repository import Repository,Scope


def test_settings(**updates):
    base=Settings(uri='bolt://127.0.0.1:17694',database='neo4j',user='neo4j',password='',
        issuer='https://issuer.example/',audience='https://mcp.example/mcp',clients=('test-client',),
        resource_url='http://127.0.0.1:8098/mcp',cursor_secret='test-only-secret-'*4,mode='test')
    return replace(base,**updates)


@contextmanager
def production_settings(base):
    """Isolated production-mode maintenance fixture with the required external state."""
    from mcp_backend.account_state import AccountState
    parent=Path(__file__).resolve().parents[1]/'.mcp-artifacts';parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as temporary:
        root=Path(temporary).resolve();assert root.is_relative_to(parent.resolve())
        (root/'purge').mkdir()
        settings=replace(base,mode='production',password='unused-test-only',resource_url=base.audience,
            purge_ledger_dir=str(root/'purge'),purge_ledger_secret='synthetic-ledger-key-'*3)
        AccountState.initialize(settings)
        yield settings


def workspace(repo,role='owner'):
    dataset='mcp-test-'+uuid4().hex
    scope=Scope('test-actor','test-client',dataset,dataset,frozenset({'read','record'}),int(time.time())+3600)
    repo.transaction(lambda tx:tx.run('CREATE (:KDWorkspace {id:$d,dataset:$d,status:"active",kind:"personal"}) '
        'CREATE (:KDWorkspaceMember {id:$id,workspace_id:$d,dataset:$d,actor:$a,enabled:true,role:$role}) '
        'CREATE (:KDData {id:$d,revision:0,mcp_catalog_revision:0})',d=dataset,id=uuid4().hex,a=scope.actor,role=role).consume())
    return scope


def cleanup(repo,*scopes):
    assert repo.settings.mode=='test' and repo.settings.uri=='bolt://127.0.0.1:17694'
    for scope in scopes:
        assert scope.dataset.startswith('mcp-test-')
        repo.transaction(lambda tx:tx.run('MATCH (n) WHERE n.dataset=$d OR (n:KDData AND n.id=$d) '
            'DETACH DELETE n',d=scope.dataset).consume())
        from mcp_backend.purge_ledger import PurgeLedger
        from mcp_backend.domain import digest
        root=PurgeLedger(repo.settings).root(scope.dataset)
        if root:
            # Only this synthetic scope's regular files inside its verified ledger root.
            for path in root.glob(digest(scope.dataset)+'-*'):
                assert path.resolve().is_relative_to(root.resolve()) and not path.is_symlink() and path.is_file()
                path.unlink()
