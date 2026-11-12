"""Token scopes never exceed the current workspace role."""
PERMISSIONS=frozenset({'read','record','update','delete','restore','history','purge','bulk'})
PUBLIC_PERMISSIONS=PERMISSIONS-{'purge'}
ROLE_PERMISSIONS={'viewer':frozenset({'read'}),'reviewer':frozenset({'read','record'}),
    'editor':frozenset({'read','record','update','delete','restore','history'}),'owner':PERMISSIONS}
