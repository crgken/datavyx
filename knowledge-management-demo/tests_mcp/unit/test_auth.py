import time
from dataclasses import replace
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

    def test_self_service_accepts_new_clients_without_changing_user_identity(self):
        auth=Authentication(replace(settings(),client_policy='authenticated',clients=()),None,
                            key_resolver=lambda token:self.private.public_key())
        a=auth.authenticate('Bearer '+self.token(azp='new-dcr-client-a'))
        b=auth.authenticate('Bearer '+self.token(azp='new-dcr-client-b'))
        self.assertEqual(a.actor,b.actor)
        self.assertNotEqual(a.client_id,b.client_id)
        self.assertNotEqual(a.actor,auth.authenticate('Bearer '+self.token(sub='auth0|other')).actor)

    def test_self_service_preserves_token_and_identity_checks(self):
        auth=Authentication(replace(settings(),client_policy='authenticated',clients=()),None,
                            key_resolver=lambda token:self.private.public_key())
        for change in ({'iss':'https://wrong.example/'},{'aud':'wrong'},{'exp':1},
                       {'scope':'openid profile'},{'sub':'machine@clients'},
                       {'gty':'client-credentials'},{'azp':''},{'azp':None},{'azp':[]},
                       {'sub':'   '},{'azp':'a','client_id':'b'},{'iat':True}):
            with self.subTest(change=change),self.assertRaises(KnowledgeError):
                auth.authenticate('Bearer '+self.token(**change))
        for token in ('Basic '+self.token(), 'Bearer '+self.token()[:-10]+'AAAAAAAAAA'):
            with self.assertRaises(KnowledgeError):auth.authenticate(token)
        other_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        with self.assertRaises(KnowledgeError):
            auth.authenticate('Bearer '+jwt.encode({'sub':'fake'},other_key,algorithm='RS256'))
        restricted=Authentication(replace(auth.settings,organization='org-approved'),None,
                                  key_resolver=lambda token:self.private.public_key())
        with self.assertRaises(KnowledgeError):restricted.authenticate('Bearer '+self.token(org_id='other'))
        self.assertEqual(restricted.authenticate('Bearer '+self.token(org_id='org-approved')).subject,'auth0|synthetic')
        self.assertEqual(auth.authenticate('Bearer '+self.token(scope='knowledge:read knowledge:purge')).permissions,
                         frozenset({'read'}))


if __name__=='__main__':unittest.main()
