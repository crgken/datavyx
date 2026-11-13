import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import uvicorn
from mcp_backend.auth import Authentication
from mcp_backend.server import create_app,TOOL_INPUTS,WRITES,DESTRUCTIVE
from mcp_backend.permissions import PERMISSIONS,PUBLIC_PERMISSIONS
from uuid import uuid4
from tests_mcp.integration.test_mutations import instruction
from mcp_backend.neo4j_repository import Repository
from tests_mcp.integration.test_ingestion import sample
from tests_mcp.support import test_settings, workspace, cleanup


class AuthenticatedTransport:
    def __init__(self, url, token): self.url, self.token = url, token
    async def __aenter__(self):
        self.stack = AsyncExitStack()
        client = await self.stack.enter_async_context(httpx2.AsyncClient(
            headers={'Authorization': 'Bearer ' + self.token}, trust_env=False, timeout=15))
        return await self.stack.enter_async_context(streamable_http_client(self.url, http_client=client))
    async def __aexit__(self, *args): return await self.stack.__aexit__(*args)


class HTTPProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
        cls.settings = test_settings(port=port, resource_url=f'http://127.0.0.1:{port}/mcp')
        connect = socket.socket.connect
        def guarded_connect(sock, address):
            # Windows asyncio creates loopback socketpairs on ephemeral ports.
            if not isinstance(address, tuple) or address[0] not in {'127.0.0.1', '::1'} or address[1] in {17693, 17695}:
                raise AssertionError('Unexpected network connection in no-model protocol validation.')
            return connect(sock, address)
        cls.network_guard = patch.object(socket.socket, 'connect', guarded_connect)
        cls.network_guard.start()
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.repo = Repository(cls.settings); cls.repo.migrate()
        cls.auth = Authentication(cls.settings, cls.repo, key_resolver=lambda token: cls.private.public_key())
        cls.web = uvicorn.Server(uvicorn.Config(create_app(cls.settings, cls.repo, cls.auth),
            host='127.0.0.1', port=port, log_level='critical', access_log=False))
        cls.thread = threading.Thread(target=cls.web.run, daemon=True); cls.thread.start()
        deadline = time.monotonic() + 15
        while not cls.web.started and cls.thread.is_alive() and time.monotonic() < deadline: time.sleep(0.02)
        if not cls.web.started: raise RuntimeError('Isolated HTTP test server did not start.')

    @classmethod
    def tearDownClass(cls):
        cls.web.should_exit = True; cls.thread.join(timeout=10); cls.repo.close(); cls.network_guard.stop()

    @classmethod
    def token(cls, **changes):
        now = int(time.time())
        return jwt.encode({'iss': cls.settings.issuer, 'aud': cls.settings.audience,
            'sub': 'auth0|synthetic-http', 'azp': 'test-client', 'iat': now-1, 'exp': now+300,
            'scope': 'knowledge:read knowledge:record', **changes}, cls.private, algorithm='RS256')

    def setUp(self):
        self.scope = workspace(self.repo)
        self.identity = self.auth.authenticate('Bearer ' + self.token())
        self.repo.transaction(lambda tx: tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.actor=$a',
            d=self.scope.dataset, a=self.identity.actor).consume())

    def tearDown(self): cleanup(self.repo, self.scope)

    def request(self, path, body=None, headers=None):
        url = self.settings.resource_url.removesuffix('/mcp') + path
        request = Request(url, data=body, headers=headers or {})
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except HTTPError as response:
            return response.code, response.headers, response.read()

    def test_health_auth_challenge_and_resource_discovery(self):
        self.assertEqual(self.request('/health/live')[0], 200)
        self.assertEqual(self.request('/health/ready')[0], 200)
        status, headers, _ = self.request('/mcp', b'{}', {'Content-Type': 'application/json'})
        self.assertEqual(status, 401); self.assertIn('resource_metadata=', headers['WWW-Authenticate'])
        status, _, raw = self.request('/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(status, 200)
        metadata = json.loads(raw)
        self.assertEqual(metadata['resource'], self.settings.resource_url)
        self.assertEqual(metadata['authorization_servers'], [self.settings.issuer])
        self.assertEqual(set(metadata['scopes_supported']), {'knowledge:'+p for p in PUBLIC_PERMISSIONS})

    def test_self_service_new_clients_provision_and_isolate_over_http(self):
        tokens=[self.token(sub='auth0|mcp-test-'+uuid4().hex,azp='new-client-'+uuid4().hex)
                for _ in range(2)]
        with patch.object(self.auth,'settings',replace(self.settings,client_policy='authenticated',clients=())):
            identities=[self.auth.authenticate('Bearer '+token) for token in tokens]
            async def run():
                async with Client(AuthenticatedTransport(self.settings.resource_url,tokens[0]),cache=None) as a:
                    context=await a.call_tool('get_context',{})
                    self.assertFalse(context.is_error)
                    own=context.structured_content['workspace_id']
                    self.assertEqual(own,'personal:'+identities[0].actor)
                    saved=await a.call_tool('record_information',sample())
                    self.assertFalse(saved.is_error)
                    async with Client(AuthenticatedTransport(self.settings.resource_url,tokens[1]),cache=None) as b:
                        context=await b.call_tool('get_context',{})
                        self.assertEqual(context.structured_content['workspace_id'],'personal:'+identities[1].actor)
                        found=await b.call_tool('find_records',{})
                        self.assertEqual(found.structured_content['rows'],[])
                        denied=await b.call_tool('get_context',{'workspace_id':own})
                        self.assertTrue(denied.is_error)
                        self.assertEqual(denied.structured_content['error']['code'],'PERMISSION_DENIED')
            try:asyncio.run(run())
            finally:
                assert self.repo.settings.mode=='test' and self.repo.settings.uri=='bolt://127.0.0.1:17694'
                for identity in identities:
                    assert identity.subject.startswith('auth0|mcp-test-')
                    self.repo.transaction(lambda tx:tx.run('MATCH (n) WHERE n.dataset=$d OR (n:KDData AND n.id=$d) '
                        'DETACH DELETE n',d='personal:'+identity.actor).consume())

    def test_real_protocol_tools_and_complete_save_query_source_receipt(self):
        async def run():
            async with Client(AuthenticatedTransport(self.settings.resource_url, self.token()), cache=None) as client:
                listed = await client.list_tools()
                self.assertEqual({t.name for t in listed.tools}, set(TOOL_INPUTS))
                for tool in listed.tools:
                    self.assertEqual(tool.input_schema, TOOL_INPUTS[tool.name].model_json_schema())
                    self.assertEqual(tool.annotations.read_only_hint, tool.name not in WRITES)
                    self.assertEqual(tool.annotations.destructive_hint,tool.name in DESTRUCTIVE)
                    self.assertEqual(tool.meta['securitySchemes'][0]['type'], 'oauth2')
                declared={tool.name:set(tool.meta['securitySchemes'][0]['scopes']) for tool in listed.tools}
                self.assertEqual(set().union(*declared.values()),{'knowledge:'+p for p in PUBLIC_PERMISSIONS})
                self.assertNotIn('purge_information',declared)
                for name in ('delete_information','restore_information'):
                    self.assertIn('knowledge:bulk',declared[name])
                self.assertEqual(declared['get_context'],{'knowledge:read'})
                context = await client.call_tool('get_context', {'workspace_id': self.scope.workspace_id})
                self.assertFalse(context.is_error)
                data = sample(); data['workspace_id'] = self.scope.workspace_id
                saved = await client.call_tool('record_information', data)
                self.assertFalse(saved.is_error)
                receipt = saved.structured_content
                found = await client.call_tool('find_records', {'workspace_id': self.scope.workspace_id, 'identifier': '001'})
                self.assertEqual(found.structured_content['rows'][0]['id'], receipt['entity_ids']['a'])
                local = await client.call_tool('get_record', {'workspace_id': self.scope.workspace_id,
                    'entity_id': receipt['entity_ids']['a']})
                self.assertEqual(local.structured_content['facts']['total'], 1)
                total = await client.call_tool('query_records', {'workspace_id': self.scope.workspace_id,
                    'operation': 'SUM', 'predicate': 'amount', 'unit': 'USD'})
                self.assertEqual(total.structured_content['sum'], '0.1')
                source = await client.call_tool('get_sources', {'workspace_id': self.scope.workspace_id,
                    'fact_id': receipt['fact_ids'][0]})
                self.assertEqual(source.structured_content['rows'][0]['text'], data['sources'][0]['text'])
                recovered = await client.call_tool('get_receipt', {'workspace_id': self.scope.workspace_id,
                    'request_id': data['request_id']})
                self.assertEqual(recovered.structured_content['receipt']['receipt_id'], receipt['receipt_id'])
        asyncio.run(run())

    def test_legacy_initialize_handshake_is_supported(self):
        async def run():
            async with Client(AuthenticatedTransport(self.settings.resource_url, self.token()), mode='legacy', cache=None) as client:
                self.assertEqual(len((await client.list_tools()).tools), 11)
                result = await client.call_tool('get_context', {'workspace_id': self.scope.workspace_id})
                self.assertFalse(result.is_error)
        asyncio.run(run())

    def test_illegal_fields_unknown_tool_and_read_only_write_fail_explicitly(self):
        async def run():
            async with Client(AuthenticatedTransport(self.settings.resource_url, self.token(scope='knowledge:read')), cache=None) as client:
                invalid = await client.call_tool('get_context', {'workspace_id': self.scope.workspace_id, 'dataset': 'override'})
                self.assertTrue(invalid.is_error); self.assertEqual(invalid.structured_content['error']['code'], 'INVALID_ARGUMENT')
                denied = await client.call_tool('record_information', {**sample(), 'workspace_id': self.scope.workspace_id})
                self.assertTrue(denied.is_error); self.assertEqual(denied.structured_content['error']['code'], 'PERMISSION_DENIED')
                self.assertIn('insufficient_scope', denied.meta['mcp/www_authenticate'][0])
                unknown = await client.call_tool('delete_records', {})
                self.assertTrue(unknown.is_error)
        asyncio.run(run())

    def test_wrong_audience_or_client_is_http_401_and_no_token_is_echoed(self):
        for changes in [{'aud': 'https://wrong.example/'}, {'azp': 'unauthorized-client'}]:
            token = self.token(**changes)
            status, headers, body = self.request('/mcp', b'{}', {'Content-Type': 'application/json', 'Authorization': 'Bearer '+token})
            self.assertEqual(status, 401); self.assertNotIn(token.encode(), body)
            self.assertIn('resource_metadata=', headers['WWW-Authenticate'])

    def test_new_mutations_over_real_http_and_cached_purge_tool_cannot_delete(self):
        async def run():
            token=self.token(scope=' '.join('knowledge:'+p for p in sorted(PERMISSIONS)))
            async with Client(AuthenticatedTransport(self.settings.resource_url,token),cache=None) as client:
                async def call(name,body):
                    result=await client.call_tool(name,{'workspace_id':self.scope.workspace_id,**body})
                    self.assertFalse(result.is_error,str(result.structured_content));return result.structured_content
                data=sample();saved=await call('record_information',data);oid=saved['entity_ids']['a']
                correction={**instruction('http-correction-001',text='Change amount to 65.'),'changes':[{
                    'action':'replace_fact','entity_id':oid,'expected_version':1,'predicate':'amount','fact_ids':saved['fact_ids'],
                    'quotes':[{'source_ref':'s','quote':'65'}],'replacements':[{'value':{'type':'number','decimal':'65','unit':'USD'},
                        'inherit_unit':True,'quotes':[{'source_ref':'s','quote':'65'}]}]}]}
                await call('update_information',correction)
                selection={'resource':'entity','ids':[oid]}
                matched=await call('find_records',{'selection':selection})
                deleted=await call('delete_information',{**instruction('http-delete-001'),'selection':selection,
                    'expected_revision':matched['revision'],'expected_count':matched['selection_count'],'selection_digest':matched['selection_digest']})
                await call('restore_information',{**instruction('http-restore-001'),'delete_receipt_id':deleted['receipt_id'],
                    'expected_revision':deleted['revision']})
                history=await call('get_history',{'entity_id':oid});self.assertEqual(history['total'],3)
                selection['state']='all';matched=await call('find_records',{'selection':selection})
                refused=await client.call_tool('purge_information',{'workspace_id':self.scope.workspace_id,
                    **instruction(str(uuid4())),'selection':selection,
                    'expected_revision':matched['revision'],'expected_count':matched['selection_count'],
                    'selection_digest':matched['selection_digest']})
                self.assertTrue(refused.is_error)
                self.assertEqual(refused.structured_content['error']['code'],'UNKNOWN_TOOL')
                current=await call('get_record',{'entity_id':oid})
                self.assertEqual(current['facts']['rows'][0]['value']['value'],'65')
                old=await call('get_receipt',{'request_id':data['request_id']})
                self.assertEqual(old['receipt']['status'],'recorded')
                self.assertEqual((await call('get_history',{'entity_id':oid}))['total'],3)
                context=await call('get_context',{})
                self.assertNotIn('purge',context['permissions'])
                self.assertNotIn('purge',context['profile']['supported_operations'])
        asyncio.run(run())

    def test_missing_new_scope_requests_the_correct_scope_but_role_denial_does_not_reauthorize(self):
        async def run():
            body={**instruction('http-denied-001',text='Rename Alpha.'),'changes':[{
                'action':'set_entity_fields','entity_id':'not-a-real-target','expected_version':1,'fields':{'name':'Alpha'},
                'quotes':[{'source_ref':'s','quote':'Alpha'}]}]}
            async with Client(AuthenticatedTransport(self.settings.resource_url,self.token()),cache=None) as client:
                result=await client.call_tool('update_information',{'workspace_id':self.scope.workspace_id,**body})
                self.assertTrue(result.is_error);self.assertIn('knowledge:update',result.meta['mcp/www_authenticate'][0])
            self.repo.transaction(lambda tx:tx.run('MATCH (m:KDWorkspaceMember {workspace_id:$d}) SET m.role="viewer"',d=self.scope.dataset).consume())
            token=self.token(scope=' '.join('knowledge:'+p for p in sorted(PERMISSIONS)))
            async with Client(AuthenticatedTransport(self.settings.resource_url,token),cache=None) as client:
                result=await client.call_tool('update_information',{'workspace_id':self.scope.workspace_id,**body})
                self.assertTrue(result.is_error);self.assertEqual(result.structured_content['error']['code'],'PERMISSION_DENIED')
                self.assertFalse(result.meta and result.meta.get('mcp/www_authenticate'))
        asyncio.run(run())

    def test_bulk_scope_challenge_and_exact_empty_retry_over_http(self):
        async def run():
            selection={'resource':'workspace'}
            async with Client(AuthenticatedTransport(self.settings.resource_url,self.token()),cache=None) as client:
                found=await client.call_tool('find_records',{'workspace_id':self.scope.workspace_id,'selection':selection})
                matched=found.structured_content
            self.assertEqual(matched['selection_count'],0)
            body={'workspace_id':self.scope.workspace_id,**instruction('http-empty-bulk-001',text='Delete the empty acceptance workspace.'),
                'selection':selection,'expected_revision':matched['revision'],'expected_count':0,'selection_digest':matched['selection_digest']}
            token=self.token(scope='knowledge:read knowledge:delete')
            async with Client(AuthenticatedTransport(self.settings.resource_url,token),cache=None) as client:
                denied=await client.call_tool('delete_information',body)
                self.assertTrue(denied.is_error)
                self.assertIn('knowledge:bulk',denied.meta['mcp/www_authenticate'][0])
                self.assertEqual(denied.structured_content['error']['code'],'PERMISSION_DENIED')
            token=self.token(scope='knowledge:read knowledge:delete knowledge:bulk')
            async with Client(AuthenticatedTransport(self.settings.resource_url,token),cache=None) as client:
                result=await client.call_tool('delete_information',body)
                self.assertFalse(result.is_error,str(result.structured_content))
                receipt=result.structured_content
                self.assertEqual((receipt['status'],receipt['applied_count'],receipt['sources_recorded']),('noop',0,0))
                self.assertEqual(receipt['revision'],matched['revision'])
                replay=await client.call_tool('delete_information',body)
                self.assertFalse(replay.is_error)
                self.assertTrue(replay.structured_content['idempotent'])
        asyncio.run(run())

    def test_validation_errors_identify_schema_fields_without_exposing_values_or_unknown_keys(self):
        secret='PRIVATE-INVALID-INPUT-NEVER-LOG-456'
        async def run():
            body={**instruction('http-invalid-v2-001',text=secret),'schema_version':'1.0',secret:secret,
                'workspace_id':self.scope.workspace_id,'changes':[{
                    'action':'set_entity_fields','entity_id':'unused','expected_version':secret,
                    'fields':{'name':'Synthetic'},'quotes':[{'source_ref':'s','quote':secret}]}]}
            async with Client(AuthenticatedTransport(self.settings.resource_url,self.token()),cache=None) as client:
                with self.assertLogs('atlasai.mcp',level='INFO') as captured:
                    result=await client.call_tool('update_information',body)
            self.assertTrue(result.is_error)
            details=result.structured_content['error']['details']
            self.assertEqual(details['expected_schema_version'],'2.0')
            self.assertIn({'path':['schema_version'],'code':'literal_error'},details['validation_errors'])
            self.assertTrue(any('expected_version' in e['path'] and e['code']=='int_type' for e in details['validation_errors']))
            self.assertNotIn(secret,json.dumps(result.model_dump()))
            self.assertNotIn(secret,'\n'.join(captured.output))
            self.assertIn('argument_validation_failed','\n'.join(captured.output))
        asyncio.run(run())

    def test_request_limit_and_origin_host_guards(self):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'Authorization': 'Bearer '+self.token()}
        self.assertEqual(self.request('/mcp', b'x'*(256*1024+1), headers)[0], 413)
        self.assertIn(self.request('/mcp', b'{}', {**headers, 'Origin': 'https://untrusted.example'})[0], [400, 403])
        self.assertIn(self.request('/mcp', b'{}', {**headers, 'Host': 'untrusted.example'})[0], [400, 421])

    def test_unexpected_errors_have_trace_ids_and_logs_do_not_contain_payloads(self):
        secret = 'PRIVATE-RECEIVED-TEXT-NEVER-LOG-123'
        async def run():
            async with Client(AuthenticatedTransport(self.settings.resource_url, self.token()), cache=None) as client:
                with patch.object(self.repo, 'scoped', side_effect=RuntimeError(secret)):
                    with self.assertLogs('atlasai.mcp', level='INFO') as captured:
                        result = await client.call_tool('get_context', {'workspace_id': self.scope.workspace_id})
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content['error']['code'], 'INTERNAL_ERROR')
                self.assertIn('trace_id', result.structured_content['error'])
                self.assertNotIn(secret, json.dumps(result.model_dump()))
                self.assertNotIn(secret, '\n'.join(captured.output))
                self.assertIn('RuntimeError', '\n'.join(captured.output))
        asyncio.run(run())

    def test_database_failure_and_resource_limit_are_explicit_tool_errors(self):
        from mcp_backend.errors import KnowledgeError
        async def run():
            async with Client(AuthenticatedTransport(self.settings.resource_url, self.token()), cache=None) as client:
                with patch.object(self.repo, 'scoped', side_effect=KnowledgeError('SERVICE_UNAVAILABLE', 'Database unavailable.', 503, True)):
                    result = await client.call_tool('get_context', {'workspace_id': self.scope.workspace_id})
                self.assertTrue(result.is_error); self.assertTrue(result.structured_content['error']['retryable'])
                # A legacy source may be large: fail explicitly rather than truncate returned evidence.
                sid = self.scope.dataset + '-large'
                self.repo.transaction(lambda tx: tx.run('CREATE (:KDSource {dataset:$d,id:$id,message:$text})',
                    d=self.scope.dataset, id=sid, text='x'*(512*1024)).consume())
                limited = await client.call_tool('get_sources', {'workspace_id': self.scope.workspace_id, 'source_id': sid})
                self.assertTrue(limited.is_error); self.assertEqual(limited.structured_content['error']['code'], 'LIMIT_EXCEEDED')
        asyncio.run(run())


if __name__ == '__main__': unittest.main()
