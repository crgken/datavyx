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
from mcp_backend.contracts import TOOL_INPUTS
from mcp_backend.server import create_app
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
        self.assertEqual(set(metadata['scopes_supported']), {'knowledge:read', 'knowledge:record'})

    def test_real_protocol_tools_and_complete_save_query_source_receipt(self):
        async def run():
            async with Client(AuthenticatedTransport(self.settings.resource_url, self.token()), cache=None) as client:
                listed = await client.list_tools()
                self.assertEqual({t.name for t in listed.tools}, set(TOOL_INPUTS))
                for tool in listed.tools:
                    self.assertEqual(tool.input_schema, TOOL_INPUTS[tool.name].model_json_schema())
                    self.assertEqual(tool.annotations.read_only_hint, tool.name != 'record_information')
                    self.assertFalse(tool.annotations.destructive_hint)
                    self.assertEqual(tool.meta['securitySchemes'][0]['type'], 'oauth2')
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
                self.assertEqual(len((await client.list_tools()).tools), 7)
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
