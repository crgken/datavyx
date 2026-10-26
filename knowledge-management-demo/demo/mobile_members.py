"""Explicit local administration; never an unauthenticated HTTP endpoint.

Run from the knowledge project: python -m demo.mobile_members --help
"""
import argparse
import time
from uuid import uuid4

from dotenv import load_dotenv

from .config import Config
from .mobile_auth import MobileAuth, ROLES
from .model import digest, encoded
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Grant or revoke knowledge workspace membership.')
    parser.add_argument('--subject', required=True, help='Exact Auth0 user_id (sub), not email')
    parser.add_argument('--role', choices=ROLES, default='viewer')
    parser.add_argument('--disable', action='store_true')
    parser.add_argument('--acknowledge-data-binding', action='store_true',
                        help='Confirm this configured workspace owns the existing dataset')
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    if not args.acknowledge_data_binding:
        parser.error('--acknowledge-data-binding is required; no data is assigned automatically')
    if not 0 < len(args.subject) <= 200 or not args.reason.strip():
        parser.error('subject and reason must be non-empty')
    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    store = Store(Config.from_env())
    try:
        auth = MobileAuth(store)
        issuer, _, _ = auth.configuration()
        auth.initialize()
        member_id = digest([issuer, args.subject, auth.workspace_id])
        actor = digest([issuer, args.subject])
        def update(tx):
            previous = tx.run('MATCH (m:KDMobileMember {id:$id}) RETURN properties(m) AS m',
                              id=member_id).single()
            member = {'id': member_id, 'issuer': issuer, 'subject': args.subject,
                      'actor': actor, 'workspace': auth.workspace_id, 'dataset': store.dataset,
                      'role': args.role, 'enabled': not args.disable, 'updated_at': time.time()}
            tx.run('MERGE (m:KDMobileMember {id:$id}) SET m=$member',
                   id=member_id, member=member).consume()
            tx.run('CREATE (a:KDMobileAccessAudit) SET a=$audit', audit={
                'id': str(uuid4()), 'member_id': member_id, 'workspace': auth.workspace_id,
                'before': encoded(dict(previous['m']) if previous else {}),
                'after': encoded(member), 'reason': args.reason.strip(),
                'administrator': 'local-administrator', 'created_at': time.time()}).consume()
        store.write(update)
        print('Membership disabled.' if args.disable else 'Membership granted: ' + args.role)
    finally:
        store.close()


if __name__ == '__main__':
    main()
