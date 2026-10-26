"""Explicit local administration for team workspace membership."""
import argparse
import time
from uuid import uuid4

from dotenv import load_dotenv

from .config import Config
from .mobile_auth import MobileAuth, ROLES
from .model import digest, encoded, require
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Grant or revoke workspace membership.')
    parser.add_argument('--subject', required=True, help='Exact Auth0 user_id (sub), not email')
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--role', choices=ROLES, default='viewer')
    parser.add_argument('--disable', action='store_true')
    parser.add_argument('--acknowledge-data-binding', action='store_true',
                        help='Confirm this workspace owns the selected dataset')
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    if not args.acknowledge_data_binding:
        parser.error('--acknowledge-data-binding is required')
    if not 0 < len(args.subject) <= 200 or not args.reason.strip():
        parser.error('subject and reason must be non-empty')

    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    store = Store(Config.from_env())
    try:
        auth = MobileAuth(store)
        issuer, _, _ = auth.configuration()
        auth.initialize()
        actor = digest([issuer, args.subject])
        member_id = digest([args.workspace_id, actor])
        now = time.time()

        def update(tx):
            space = tx.run('MATCH (w:KDWorkspace {id:$id,status:"active"}) '
                           'RETURN w.dataset AS dataset', id=args.workspace_id).single()
            require(space, 'workspace_not_found', '工作空间不存在或已停用。', 404)
            previous = tx.run('MATCH (m:KDWorkspaceMember {id:$id}) RETURN properties(m) AS m',
                              id=member_id).single()
            tx.run('MERGE (u:KDUser {id:$id}) ON CREATE SET u.issuer=$issuer,u.subject=$subject,u.created_at=$now '
                   'SET u.last_seen_at=$now', id=actor, issuer=issuer, subject=args.subject, now=now).consume()
            member = {'id': member_id, 'workspace_id': args.workspace_id, 'dataset': space['dataset'],
                      'actor': actor, 'role': args.role, 'enabled': not args.disable,
                      'updated_at': now}
            tx.run('MERGE (m:KDWorkspaceMember {id:$id}) SET m=$member',
                   id=member_id, member=member).consume()
            tx.run('CREATE (a:KDWorkspaceAccessAudit) SET a=$audit', audit={
                'id': str(uuid4()), 'workspace_id': args.workspace_id, 'member_id': member_id,
                'before': encoded(dict(previous['m']) if previous else {}),
                'after': encoded(member), 'reason': args.reason.strip(),
                'administrator': 'local-administrator', 'created_at': now}).consume()

        store.write(update)
        print('Membership disabled.' if args.disable else 'Membership granted: ' + args.role)
    finally:
        store.close()


if __name__ == '__main__':
    main()
