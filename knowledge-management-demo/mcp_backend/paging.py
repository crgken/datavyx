"""Authenticated, revision-bound cursors for bounded deterministic results."""
import base64
import hashlib
import hmac
import json
import time
from .domain import encoded, digest
from .errors import KnowledgeError, require


def _encode(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _decode(value):
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


class Pager:
    def __init__(self, secret):
        self.secret = secret.encode('utf-8')

    def page(self, rows, scope, revision, data, purpose, cursor=None):
        fingerprint = digest([purpose, scope.actor, scope.client_id, scope.workspace_id,
                              scope.dataset, data.model_dump(exclude={'cursor', 'facts_cursor', 'relations_cursor'})])
        offset = 0
        if cursor:
            try:
                payload, signature = cursor.split('.')
                raw = _decode(payload)
                expected = hmac.new(self.secret, raw, hashlib.sha256).digest()
                if not hmac.compare_digest(expected, _decode(signature)):
                    raise ValueError()
                value = json.loads(raw)
                if (set(value) != {'v', 'query', 'revision', 'offset', 'expires'} or value['v'] != 1
                        or value['query'] != fingerprint or type(value['offset']) is not int
                        or value['offset'] < 0 or type(value['expires']) is not int
                        or value['expires'] <= time.time()):
                    raise ValueError()
                offset = value['offset']
            except (ValueError, TypeError, KeyError, UnicodeError):
                raise KnowledgeError('INVALID_CURSOR', 'The cursor is invalid for this query.') from None
            require(value['revision'] == revision, 'STALE_CURSOR',
                    'Data changed after this page was issued. Restart the query.', 409)
            require(offset < len(rows), 'INVALID_CURSOR', 'The cursor is outside this result.')
        selected = rows[offset:offset + data.limit]
        end = offset + len(selected)
        more = end < len(rows)
        following = None
        if more:
            raw = encoded({'v': 1, 'query': fingerprint, 'revision': revision, 'offset': end,
                           'expires': min(scope.expires, int(time.time()) + 900)}).encode('utf-8')
            following = _encode(raw) + '.' + _encode(hmac.new(self.secret, raw, hashlib.sha256).digest())
        return {'schema_version': '1.0', 'revision': revision, 'rows': selected, 'total': len(rows),
                'returned_count': len(selected), 'has_more': more, 'next_cursor': following,
                'complete': not more}
