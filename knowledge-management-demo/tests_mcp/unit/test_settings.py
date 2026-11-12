from dataclasses import replace
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
from mcp_backend.settings import Settings
from mcp_backend.errors import KnowledgeError


def settings(**updates):
    base=Settings(uri='bolt://127.0.0.1:17694',database='neo4j',user='neo4j',password='',
        issuer='https://issuer.example/',audience='https://mcp.example/mcp',clients=('test-client',),
        resource_url='http://127.0.0.1:8098/mcp',cursor_secret='test-only-secret-'*4,mode='test')
    return replace(base,**updates)


class SettingsTests(unittest.TestCase):
    def test_test_database_is_fail_closed(self):
        for uri in ('bolt://127.0.0.1:17693','bolt://127.0.0.1:17695','bolt://remote.example:17694'):
            with self.subTest(uri=uri),self.assertRaises(KnowledgeError):settings(uri=uri)

    def test_credentials_never_enter_repr(self):
        s=settings(password='private-password')
        self.assertNotIn('private-password',repr(s))
        self.assertNotIn(s.cursor_secret,repr(s))

    def test_missing_config_does_not_load_legacy_env(self):
        with patch.dict(os.environ,{'KD_NEO4J_URI':'bolt://production:17693','LLM_API_KEY':'unused'},clear=True):
            with self.assertRaises(KnowledgeError):Settings.from_env()

    def test_canonical_resources_and_limits(self):
        for change in ({'resource_url':'https://user:secret@example/mcp'}, {'cursor_secret':'short'},
                       {'issuer':'http://issuer.example/'},{'host':'0.0.0.0'},{'clients':()}):
            with self.subTest(change=change),self.assertRaises(KnowledgeError):settings(**change)

    def test_import_does_not_initialize_legacy_or_network(self):
        code="import sys; import mcp_backend; import mcp_backend.contracts; import mcp_backend.settings; assert not any(n.startswith(('demo','graphiti_core','pydantic_ai','openai')) for n in sys.modules)"
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_config_check_requires_no_model_key_or_database_connection(self):
        env={**os.environ,'MCP_NEO4J_URI':'bolt://127.0.0.1:17694','MCP_NEO4J_PASSWORD':'',
             'MCP_AUTH_ISSUER':'https://issuer.example/','MCP_AUTH_AUDIENCE':'test-audience',
             'MCP_AUTH_CLIENT_IDS':'test-client','MCP_RESOURCE_URL':'http://127.0.0.1:8098/mcp',
             'MCP_CURSOR_SECRET':'test-secret-'*5,'MCP_MODE':'test'}
        env={k:v for k,v in env.items() if not k.startswith(('LLM_','OPENAI_','GRAPHITI_','EMBEDDING_'))}
        result=subprocess.run([sys.executable,'-m','mcp_backend','--check-config'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('configuration_valid',result.stdout)
        self.assertNotIn(env['MCP_CURSOR_SECRET'],result.stdout)

    def test_production_cannot_omit_the_external_erasure_boundary(self):
        with self.assertRaises(KnowledgeError):settings(mode='production',password='test-only',resource_url='https://mcp.example/mcp')


if __name__=='__main__':unittest.main()
