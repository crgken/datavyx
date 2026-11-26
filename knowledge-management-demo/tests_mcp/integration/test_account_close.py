from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
from mcp_backend.account_close import AccountClose
from mcp_backend.account_state import AccountState
from mcp_backend.auth import Authentication,Identity
from mcp_backend.contracts import FindInput,RecordInput
from mcp_backend.domain import digest
from mcp_backend.errors import KnowledgeError
from mcp_backend.ingestion import Ingestion
from mcp_backend.migrations.snapshot import export_snapshot,restore_test_snapshot
from mcp_backend.neo4j_repository import Repository
from mcp_backend.permissions import PUBLIC_PERMISSIONS
from mcp_backend.records import Records
from tests_mcp.integration.test_ingestion import sample
from tests_mcp.support import test_settings


class AccountCloseTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).resolve().parents[2]/'.mcp-artifacts';parent.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=parent)
        root=Path(self.temp.name).resolve();assert root.is_relative_to(parent.resolve())
        (root/'purge').mkdir()
        self.repo=Repository(test_settings(client_policy='authenticated',clients=(),purge_ledger_dir=str(root/'purge'),
            purge_ledger_secret='synthetic-ledger-'*4))
        AccountState.initialize(self.repo.settings);self.repo.migrate()
        self.auth=Authentication(self.repo.settings,self.repo)
        self.identities=[Identity(digest('mcp-test-'+uuid4().hex),self.repo.settings.issuer,'synthetic',
            'test-client',PUBLIC_PERMISSIONS,int(time.time())+300,'') for _ in range(2)]
        self.scopes=[self.auth.scope(i) for i in self.identities]
        self.extra=[];self.close=AccountClose(self.repo);self.state=self.close.state
        self.actor=self.identities[0].actor

    def tearDown(self):
        assert self.repo.settings.mode=='test' and self.repo.settings.uri=='bolt://127.0.0.1:17694'
        for dataset in [s.dataset for s in self.scopes]+self.extra:
            assert dataset.startswith(('personal:','mcp-test-'))
            self.repo.transaction(lambda tx:tx.run('MATCH (n) WHERE n.dataset=$d OR (n:KDData AND n.id=$d) '
                'DETACH DELETE n',d=dataset).consume())
        self.repo.transaction(lambda tx:tx.run('MATCH (c:KDMCPAccountClosure) WHERE c.id IN $a DELETE c',
            a=[i.actor for i in self.identities]).consume())
        self.repo.close();self.temp.cleanup()

    def save(self,index=0):return Ingestion(self.repo).record_information(self.scopes[index],RecordInput.model_validate(sample()))
    def deny(self,fn,code='ACCOUNT_CLOSED'):
        with self.assertRaises(KnowledgeError) as error:fn()
        self.assertEqual(error.exception.code,code)

    def test_verified_close_blocks_existing_scope_new_login_and_cross_client(self):
        self.save();self.save(1)
        body=self.close.block(self.actor,'identity-verified-case')
        self.assertEqual(body['profiles'][self.repo.settings.resource_url]['datasets'],[self.scopes[0].dataset])
        self.deny(lambda:Records(self.repo).find_records(self.scopes[0],FindInput()))
        self.deny(lambda:self.auth.scope(self.identities[0]))
        self.deny(lambda:self.auth.scope(replace(self.identities[0],client_id='another-client')))
        self.assertEqual(len(Records(self.repo).find_records(self.scopes[1],FindInput())['rows']),1)
        self.close.erase(self.actor)
        self.deny(lambda:self.auth.scope(self.identities[0]))
        self.assertEqual(self.repo.transaction(lambda tx:tx.run('MATCH (n {dataset:$d}) RETURN count(n) AS n',
            d=self.scopes[0].dataset).single()['n']),0)

    def test_database_backup_restore_cannot_revive_closed_identity_or_data(self):
        self.save();snapshot=export_snapshot(self.repo,self.scopes[0].dataset)
        self.close.block(self.actor,'identity-verified-case');self.close.erase(self.actor)
        restore_test_snapshot(self.repo,snapshot)
        self.deny(lambda:self.repo.transaction(self.state.check_restore),'RECOVERY_REQUIRED')
        self.deny(lambda:self.auth.scope(self.identities[0]))
        self.close.block(self.actor,'identity-verified-case');self.close.erase(self.actor)
        self.repo.transaction(self.state.check_restore)
        self.deny(lambda:self.auth.scope(self.identities[0]))

    def test_shared_content_and_other_owner_survive_member_removal(self):
        saved=self.save(1);shared=self.scopes[1].dataset
        self.repo.transaction(lambda tx:tx.run('MATCH (w:KDWorkspace {id:$d}) SET w.kind="shared" '
            'CREATE (:KDWorkspaceMember {id:$id,workspace_id:$d,dataset:$d,actor:$a,role:"editor",enabled:true})',
            d=shared,id=uuid4().hex,a=self.actor).consume())
        self.close.block(self.actor,'identity-verified-case');self.close.erase(self.actor)
        self.assertEqual(len(Records(self.repo).find_records(self.scopes[1],FindInput())['rows']),1)
        count=self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$w,actor:$a}) '
            'RETURN count(m) AS n',w=shared,a=self.actor).single()['n'])
        self.assertEqual(count,0)

    def test_ambiguous_legacy_space_requires_explicit_identity_scoped_classification(self):
        self.save();dataset=self.scopes[0].dataset
        self.repo.transaction(lambda tx:tx.run('MATCH (w:KDWorkspace {id:$d}) REMOVE w.owner_actor SET w.kind="legacy"',d=dataset).consume())
        self.deny(lambda:self.close.block(self.actor,'identity-verified-case'),'ACCOUNT_CLASSIFICATION_REQUIRED')
        self.deny(lambda:self.auth.scope(self.identities[0]))
        self.close.block(self.actor,'identity-verified-case',legacy_personal=[dataset]);self.close.erase(self.actor)
        self.repo.transaction(self.state.check_restore)

    def test_external_relationship_fails_without_partial_content_erasure(self):
        self.save();self.save(1)
        self.repo.transaction(lambda tx:tx.run('MATCH (a:KDObject {dataset:$a}),(b:KDObject {dataset:$b}) '
            'CREATE (a)-[:DIAGNOSTIC_FOREIGN]->(b)',a=self.scopes[0].dataset,b=self.scopes[1].dataset).consume())
        self.close.block(self.actor,'identity-verified-case')
        self.deny(lambda:self.close.erase(self.actor),'ACCOUNT_EXTERNAL_DEPENDENCY')
        count=self.repo.transaction(lambda tx:tx.run('MATCH (o:KDObject {dataset:$d}) RETURN count(o) AS n',d=self.scopes[0].dataset).single()['n'])
        self.assertEqual(count,1)

    def test_pending_external_barrier_survives_database_transaction_failure(self):
        self.save()
        body=self.state.start(self.actor,'identity-verified-case')
        self.deny(lambda:self.auth.scope(self.identities[0]))
        self.assertEqual(body['profiles'],{})
        self.close.block(self.actor,'identity-verified-case');self.close.erase(self.actor)
        other=replace(self.repo.settings,resource_url='https://other-environment.example/mcp')
        # The same host-wide state also protects another client/environment before its DB cleanup.
        other_repo=Repository(other)
        try:self.deny(lambda:other_repo.transaction(lambda tx:AccountState(other).check_actor(tx,self.actor)))
        finally:other_repo.close()

    def test_erasure_retries_after_commit_before_signed_completion(self):
        self.save();self.close.block(self.actor,'identity-verified-case')
        with patch.object(self.state,'write',side_effect=OSError('synthetic interrupted state write')):
            with self.assertRaises(OSError):self.close.erase(self.actor)
        self.deny(lambda:self.auth.scope(self.identities[0]))
        result=self.close.erase(self.actor)
        self.assertEqual(result['state'],'erased');self.assertEqual(result['removed_nodes'],0)
        self.assertTrue(self.close.erase(self.actor)['idempotent'])


if __name__=='__main__':unittest.main()
