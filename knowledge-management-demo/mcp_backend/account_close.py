"""Identity-verified administrative closure. Public MCP erasure stays unavailable."""
import time
from .account_state import AccountState
from .errors import require


class AccountClose:
    def __init__(self,repo):self.repo=repo;self.state=AccountState(repo.settings)

    @staticmethod
    def _plan(tx,actor,legacy_personal=()):
        rows=[dict(r) for r in tx.run('MATCH (m:KDWorkspaceMember {actor:$a}) '
            'MATCH (w:KDWorkspace {id:m.workspace_id}) '
            'OPTIONAL MATCH (other:KDWorkspaceMember {workspace_id:w.id}) WHERE other.actor<>$a '
            'RETURN w.id AS workspace,w.dataset AS dataset,w.kind AS kind,w.owner_actor AS owner,'
            'm.role AS role,collect(other.actor) AS others,collect(other.role) AS other_roles ORDER BY workspace',a=actor)]
        selected=[];ambiguous=[];shared=[]
        for row in rows:
            private=row['kind']=='personal' and row['owner']==actor
            legacy=row['workspace'] in legacy_personal and row['role']=='owner' and not row['others']
            if (private or legacy) and not row['others']:
                require(row['dataset'] and row['role']=='owner','ACCOUNT_SCOPE_INVALID','Personal ownership is inconsistent.',409)
                selected.append(row)
            elif row['role']=='owner' and not row['others']:
                ambiguous.append(row['workspace'])
            else:
                require(row['role']!='owner' or 'owner' in row['other_roles'],
                    'ACCOUNT_OWNER_TRANSFER_REQUIRED','Transfer shared-space ownership before completing closure.',409)
                shared.append(row['workspace'])
        require(not ambiguous,'ACCOUNT_CLASSIFICATION_REQUIRED',
            'Classify sole-owner legacy spaces explicitly before erasure.',409,workspace_count=len(ambiguous))
        workspaces=sorted({r['workspace'] for r in selected});datasets=sorted({r['dataset'] for r in selected})
        # A dataset may not secretly belong to a different workspace/member.
        foreign=tx.run('MATCH (w:KDWorkspace) WHERE w.dataset IN $d AND NOT w.id IN $w '
            'RETURN count(w) AS n',d=datasets,w=workspaces).single()['n']
        require(foreign==0,'ACCOUNT_SCOPE_INVALID','A personal dataset is also bound to another space.',409)
        counts={}
        for label in ('KDObject','KDFact','KDSource','KDMCPRequest','KDMCPChange'):
            counts[label]=tx.run(f'MATCH (n:{label}) WHERE n.dataset IN $d RETURN count(n) AS n',d=datasets).single()['n']
        return {'workspaces':workspaces,'datasets':datasets,'shared_memberships':sorted(set(shared)),'counts':counts}

    def plan(self,actor,legacy_personal=()):
        return self.repo.transaction(lambda tx:self._plan(tx,actor,legacy_personal))

    def block(self,actor,verified_case,legacy_personal=()):
        with self.state.locked():
            # First persist the verified identity barrier; a crash never reopens access.
            body=self.state.start(actor,verified_case)
            return self._block(body,legacy_personal)

    def _block(self,body,legacy_personal):
        actor=body['actor']
        def block(tx):
            self.repo.metadata_lock(tx)
            plan=self._plan(tx,actor,legacy_personal)
            for dataset in sorted(set(plan['datasets'])):self.repo.revision(tx,dataset,write=True)
            tx.run('MERGE (c:KDMCPAccountClosure {id:$a}) SET c.case_digest=$c,c.closed_at=$t '
                'WITH c MATCH (m:KDWorkspaceMember {actor:$a}) SET m.enabled=false',
                a=actor,c=body['verified_case_digest'],t=body['verified_at']).consume()
            return plan
        plan=self.repo.transaction(block)
        body['profiles'][self.repo.settings.resource_url]={**plan,'state':'blocked','legacy_personal':sorted(set(legacy_personal))}
        self.state.write(body);return body

    def reconcile(self):
        results=[]
        for previous in self.state.entries():
            actor=previous['actor'];profile=previous['profiles'].get(self.repo.settings.resource_url,{})
            if profile.get('state')=='erased':
                from .errors import KnowledgeError
                try:
                    self.repo.transaction(lambda tx:self.state.check_restore(tx,[previous]));continue
                except KnowledgeError as error:
                    if error.code!='RECOVERY_REQUIRED':raise
            if profile.get('state')!='blocked':
                with self.state.locked():self._block(self.state.load(self.state.path(actor)),profile.get('legacy_personal',[]))
            results.append(self.erase(actor))
        self.repo.transaction(self.state.check_restore)
        return results

    def erase(self,actor,now=None):
        now=time.time() if now is None else now
        with self.state.locked():
            body=self.state.load(self.state.path(actor))
            profile=body['profiles'].get(self.repo.settings.resource_url)
            require(profile is not None,'ACCOUNT_PROFILE_MISSING','Block and inventory this environment first.',409)
            if profile['state']=='erased':
                self.repo.transaction(lambda tx:self.state.check_restore(tx,[body]))
                return {'state':'erased','actor':actor,'removed_nodes':0,'idempotent':True,
                    'active_data_due':body['active_data_due'],'backup_data_due':body['backup_data_due'],
                    'overdue':False}
            def erase(tx):
                self.repo.metadata_lock(tx)
                marker=tx.run('MATCH (c:KDMCPAccountClosure {id:$a}) RETURN c.case_digest AS case_digest,'
                    'c.erased_at AS erased_at',a=actor).single()
                # DB commit can precede the signed completion record. Only a matching committed
                # closure marker permits retrying that gap; an unexplained empty scope does not.
                if marker and marker['case_digest']==body['verified_case_digest'] and marker['erased_at'] is not None:
                    remaining=tx.run('MATCH (n) WHERE n.dataset IN $d OR (n:KDData AND n.id IN $d) '
                        'OR (n:KDWorkspace AND n.id IN $w) OR (n:KDWorkspaceMember AND n.actor=$a) '
                        'RETURN count(n) AS n',d=profile['datasets'],w=profile['workspaces'],a=actor).single()['n']
                    if remaining==0:return 0
                actual=self._plan(tx,actor,profile['legacy_personal'])
                require(actual['datasets']==profile['datasets'] and actual['workspaces']==profile['workspaces'],
                    'ACCOUNT_SCOPE_CHANGED','Account scope changed; re-inventory this environment before erasure.',409)
                for dataset in profile['datasets']:self.repo.revision(tx,dataset,write=True)
                outside=tx.run('MATCH (n)-[r]-(m) WHERE '
                    '(n.dataset IN $d OR (n:KDData AND n.id IN $d) OR (n:KDWorkspace AND n.id IN $w)) '
                    'AND NOT (coalesce(m.dataset,"") IN $d OR (m:KDData AND m.id IN $d) '
                    'OR (m:KDWorkspace AND m.id IN $w)) RETURN count(r) AS n',d=profile['datasets'],w=profile['workspaces']).single()['n']
                require(outside==0,'ACCOUNT_EXTERNAL_DEPENDENCY','Personal content has an external dependency; no erasure applied.',409)
                total=tx.run('MATCH (n) WHERE n.dataset IN $d OR (n:KDData AND n.id IN $d) '
                    'OR (n:KDWorkspace AND n.id IN $w) OR (n:KDWorkspaceMember AND n.actor=$a) '
                    'RETURN count(n) AS n',d=profile['datasets'],w=profile['workspaces'],a=actor).single()['n']
                require(total<=20*self.repo.settings.max_records,'LIMIT_EXCEEDED','Account exceeds the atomic close limit.',413)
                tx.run('MATCH (n) WHERE n.dataset IN $d OR (n:KDData AND n.id IN $d) '
                    'OR (n:KDWorkspace AND n.id IN $w) OR (n:KDWorkspaceMember AND n.actor=$a) '
                    'DETACH DELETE n',d=profile['datasets'],w=profile['workspaces'],a=actor).consume()
                tx.run('MERGE (c:KDMCPAccountClosure {id:$a}) SET c.case_digest=$c,c.closed_at=$t,c.erased_at=$now',
                    a=actor,c=body['verified_case_digest'],t=body['verified_at'],now=now).consume()
                return total
            removed=self.repo.transaction(erase)
            body['profiles'][self.repo.settings.resource_url]={**profile,'state':'erased','erased_at':now,'removed_nodes':removed}
            self.state.write(body)
            self.repo.transaction(lambda tx:self.state.check_restore(tx,[body]))
            return {'state':'erased','actor':actor,'removed_nodes':removed,'active_data_due':body['active_data_due'],
                'backup_data_due':body['backup_data_due'],'overdue':now>body['active_data_due']}
