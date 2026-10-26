"""Bind the current single-dataset deployment to its first personal workspace."""
import argparse
import os
import time

from dotenv import load_dotenv

from .config import Config
from .mobile_auth import MobileAuth
from .model import digest, encoded, require
from .store import Store


def main():
    parser = argparse.ArgumentParser(description='Bind the existing dataset to one personal workspace.')
    parser.add_argument('--subject', required=True, help='Exact Auth0 user_id (sub), not email')
    parser.add_argument('--acknowledge-data-binding', action='store_true',
                        help='Confirm the existing dataset belongs to this Auth0 user')
    args = parser.parse_args()
    if not args.acknowledge_data_binding:
        parser.error('--acknowledge-data-binding is required')
    if not 0 < len(args.subject) <= 200:
        parser.error('subject must be non-empty and at most 200 characters')

    from pathlib import Path
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    config = Config.from_env()
    workspace_id = os.getenv('KD_MOBILE_TENANT_ID', '').strip() or config.dataset
    dataset = config.dataset
    store = Store(config)
    try:
        auth = MobileAuth(store)
        issuer, _, _ = auth.configuration()
        auth.initialize()
        actor = digest([issuer, args.subject])
        now = time.time()

        def bind(tx):
            existing = tx.run('MATCH (m:KDWorkspaceMember {actor:$actor,enabled:true}) '
                              'MATCH (w:KDWorkspace {id:m.workspace_id,status:"active"}) '
                              'RETURN w.id AS id,w.dataset AS dataset,w.kind AS kind,m.role AS role LIMIT 1',
                              actor=actor).single()
            if existing and existing['dataset'] == dataset and existing['role'] == 'owner':
                return
            require(not existing or existing['kind'] == 'personal',
                    'workspace_binding_conflict', '账号已有非个人空间成员关系；迁移不会改写其工作空间。', 409)

            target = tx.run('MATCH (w:KDWorkspace {id:$id}) RETURN w.owner_actor AS owner,'
                            'w.dataset AS dataset', id=workspace_id).single()
            require(not target or target['owner'] in (None, actor),
                    'workspace_binding_conflict', '目标个人空间已属于其他账号。', 409)
            dataset_owner = tx.run('MATCH (w:KDWorkspace {dataset:$dataset}) '
                                   'RETURN w.id AS id,w.owner_actor AS owner LIMIT 1',
                                   dataset=dataset).single()
            require(not dataset_owner or dataset_owner['owner'] == actor,
                    'dataset_binding_conflict', '现有数据已绑定到其他账号。', 409)

            if existing and existing['dataset'] != dataset:
                counts = tx.run('MATCH (n) WHERE n.dataset=$dataset '
                                'AND NOT n:KDWorkspace AND NOT n:KDWorkspaceMember '
                                'AND NOT n:KDUser AND NOT n:KDWorkspaceAccessAudit '
                                'RETURN count(n) AS n',
                                dataset=existing['dataset']).single()['n']
                require(counts == 0, 'workspace_has_data',
                        '该账号的个人空间已有数据；为避免合并丢失，不能自动改绑。', 409)
                workspace_id_for_user = existing['id']
            else:
                workspace_id_for_user = workspace_id

            tx.run('MERGE (u:KDUser {id:$actor}) ON CREATE SET u.issuer=$issuer,u.subject=$subject,u.created_at=$now '
                   'SET u.last_seen_at=$now', actor=actor, issuer=issuer, subject=args.subject, now=now).consume()
            tx.run('MERGE (w:KDWorkspace {id:$id}) ON CREATE SET w.kind="personal",w.name=$name,'
                   'w.owner_actor=$actor,w.status="active",w.created_at=$now '
                   'SET w.dataset=$dataset,w.kind="personal",w.name=coalesce(w.name,$name),'
                   'w.owner_actor=$actor,w.status="active",w.updated_at=$now',
                   id=workspace_id_for_user, dataset=dataset,
                   name=os.getenv('KD_MOBILE_WORKSPACE_NAME', '个人空间'), actor=actor, now=now).consume()
            member_id = digest([workspace_id_for_user, actor])
            previous = tx.run('MATCH (m:KDWorkspaceMember {id:$id}) RETURN properties(m) AS m',
                              id=member_id).single()
            tx.run('MERGE (m:KDWorkspaceMember {id:$id}) ON CREATE SET m.created_at=$now '
                   'SET m.workspace_id=$workspace,m.dataset=$dataset,m.actor=$actor,m.role="owner",'
                   'm.enabled=true,m.updated_at=$now', id=member_id, workspace=workspace_id_for_user,
                   dataset=dataset, actor=actor, now=now).consume()
            tx.run('CREATE (a:KDWorkspaceAccessAudit) SET a=$audit', audit={
                'id': digest(['legacy-dataset-binding', member_id]), 'workspace_id': workspace_id_for_user,
                'member_id': member_id, 'before': encoded(dict(previous['m']) if previous else {}),
                'after': encoded({'workspace_id': workspace_id_for_user, 'dataset': dataset,
                                  'actor': actor, 'role': 'owner', 'enabled': True}),
                'reason': 'Owner-confirmed migration of the existing personal dataset',
                'administrator': 'migration', 'created_at': now}).consume()

        store.write(bind)
        print('Existing dataset bound to the Auth0 user personal workspace.')
    finally:
        store.close()


if __name__ == '__main__':
    main()
