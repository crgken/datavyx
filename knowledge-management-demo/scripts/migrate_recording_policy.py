"""Versioned recording policy migration; dry run by default.

Run only against an explicit backend env file. Quiesce writes and take a database
backup before --apply. --rollback-check executes all writes then rolls them back.
No model calls or chat replay are performed. Reports never include source text.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from demo.config import Config
from demo.store import Store


class RollbackStore(Store):
    def write(self, fn):
        with self.driver.session(database=self.config.database) as session:
            with session.begin_transaction(timeout=30) as tx:
                try:
                    return fn(tx)
                finally:
                    tx.rollback()

    read = write


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env',required=True)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true')
    mode.add_argument('--rollback-check',action='store_true')
    parser.add_argument('--dataset')
    parser.add_argument('--report',required=True)
    args=parser.parse_args()
    load_dotenv(args.env,override=True)
    config=Config.from_env()
    base=Store(config)
    try:
        with base.driver.session(database=config.database) as session:
            active=session.run("MATCH (j:KDMobileRequest) WHERE j.status IN ['queued','running'] RETURN count(j) AS n").single()['n']
            if active:
                raise RuntimeError('Wait for in-flight requests before migration')
            datasets=[args.dataset] if args.dataset else [r['id'] for r in session.run('MATCH (d:KDData) RETURN d.id AS id ORDER BY id')]
        reports=[]
        for dataset in datasets:
            store=(RollbackStore if args.rollback_check else Store)(replace(config,dataset=dataset))
            try:
                report=store.migrate_recording_policy(args.apply or args.rollback_check)
                reports.append({'dataset':dataset,**report})
            finally:
                store.close()
        output={'policy_version':2,'mode':'apply' if args.apply else 'rollback_check' if args.rollback_check else 'dry_run','datasets':reports}
        target=Path(args.report)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(output,ensure_ascii=False))
        return 2 if any(r.get('issues') for r in reports) else 0
    finally:
        base.close()


if __name__=='__main__':
    sys.exit(main())
