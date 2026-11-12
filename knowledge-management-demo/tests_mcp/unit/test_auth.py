import time
import unittest
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp_backend.auth import Authentication
from mcp_backend.errors import KnowledgeError
from test_settings import settings


class TokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.auth=Authentication(settings(),None,key_resolver=lambda token:cls.private.public_key())

    def token(self,**changes):
        now=int(time.time())
        data={'iss':'https://issuer.example/','aud':'https://mcp.example/mcp','sub':'auth0|synthetic',
              'azp':'test-client','iat':now-1,'exp':now+300,'scope':'knowledge:read knowledge:record'}
        return jwt.encode({**data,**changes},self.private,algorithm='RS256')

    def test_signed_identity_is_derived_not_supplied(self):
        identity=self.auth.authenticate('Bearer '+self.token(actor='untrusted'))
        self.assertNotEqual(identity.actor,'untrusted')
        self.assertEqual(identity.permissions,frozenset({'read','record'}))

    def test_wrong_issuer_audience_client_expiry_and_scope(self):
        for change in ({'iss':'https://wrong.example/'},{'aud':'wrong'}, {'azp':'wrong'},
                       {'exp':1},{'scope':'openid profile'},{'sub':'machine@clients'}):
            with self.subTest(change=change),self.assertRaises(KnowledgeError):
                self.auth.authenticate('Bearer '+self.token(**change))

    def test_read_only_token_never_acquires_record_permission(self):
        identity=self.auth.authenticate('Bearer '+self.token(scope='knowledge:read'))
        self.assertEqual(identity.permissions,frozenset({'read'}))

    def test_unsigned_and_tampered_tokens_fail(self):
        token=self.token()
        with self.assertRaises(KnowledgeError):self.auth.authenticate('Bearer '+token[:-10]+'AAAAAAAAAA')
        with self.assertRaises(KnowledgeError):self.auth.authenticate('Basic '+token)

    def test_new_scopes_are_explicit_and_unknown_scopes_never_become_permissions(self):
        from mcp_backend.permissions import PERMISSIONS,PUBLIC_PERMISSIONS
        identity=self.auth.authenticate('Bearer '+self.token(scope=' '.join('knowledge:'+p for p in PERMISSIONS)+' knowledge:execute'))
        self.assertEqual(identity.permissions,PUBLIC_PERMISSIONS)
        self.assertNotIn('execute',identity.permissions);self.assertNotIn('purge',identity.permissions)


if __name__=='__main__':unittest.main()
