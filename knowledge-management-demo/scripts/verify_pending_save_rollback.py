"""Replay a pending save with real permissions and forced transaction rollback.

No transaction in this script commits. Run a committed candidate in a staging
directory with the installed runtime, before deploying the candidate source.
"""
import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
from neo4j import GraphDatabase, READ_ACCESS

from demo.config import Config
from demo.diagnostics import log_failure
from demo.mobile_auth import ROLES
from demo.mobile_gateway import MobileScope
from demo.store import Store


class RollbackStore(Store):
    def __init__(self, config, draft_id, refresh_expiry):
        super().__init__(config)
        self.draft_id = draft_id
        self.refresh_expiry = refresh_expiry
        self.statements = 0

    def _rollback(self, fn, refresh_expiry=False):
        with self.driver.session(database=self.config.database) as session:
            with session.begin_transaction(timeout=30) as tx:
                try:
                    if refresh_expiry:
                        tx.run("MATCH (p:KDDraft {dataset:$d,id:$id}) SET p.expires_at=$expires",
                               d=self.dataset, id=self.draft_id, expires=time.time() + 1800).consume()
                    owner = self

                    class Counted:
                        def run(self, *args, **kwargs):
                            owner.statements += 1
                            return tx.run(*args, **kwargs)

                    return fn(Counted())
                finally:
                    tx.rollback()

    def write(self, fn):
        return self._rollback(fn, self.refresh_expiry)

    def read(self, fn):
        return self._rollback(fn)


def persistent_state(session, dataset):
    labels = ["KDObject", "KDFact", "KDSource", "KDPendingRequest", "KDBatch",
              "KDMutation", "KDIndexHead", "KDIndexJob"]
    counts = [r.data() for r in session.run("MATCH (n {dataset:$d}) "
        "WHERE any(label IN labels(n) WHERE label IN $labels) "
        "RETURN labels(n) AS labels,count(n) AS count ORDER BY labels", d=dataset, labels=labels)]
    revision = session.run("OPTIONAL MATCH (d:KDData {id:$d}) RETURN d.revision AS revision",
                           d=dataset).single()["revision"]
    return {"counts": counts, "revision": revision}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", required=True, help="Explicit backend env file; values are never printed")
    parser.add_argument("--refresh-expiry-in-rollback", action="store_true",
                        help="Refresh an expired draft only inside the rolled-back save transaction")
    parser.add_argument("--report", default="artifacts/pending-save-rollback.json")
    args = parser.parse_args()
    load_dotenv(args.env)
    config = Config.from_env()
    report = {}
    with GraphDatabase.driver(config.uri, auth=(config.user, config.password)) as driver:
        with driver.session(database=config.database, default_access_mode=READ_ACCESS) as session:
            active = session.run("MATCH (j:KDMobileRequest {status:'running'}) RETURN count(j) AS n").single()["n"]
            assert active == 0, "Wait for in-flight requests before replaying"
            row = session.run("MATCH (p:KDDraft {status:'pending'}) "
                "MATCH (w:KDWorkspace {dataset:p.dataset,status:'active'}), "
                "(m:KDWorkspaceMember {dataset:p.dataset,actor:p.owner_actor,workspace_id:w.id,enabled:true}) "
                "RETURN properties(p) AS p,w.id AS workspace,m.role AS role ORDER BY p.expires_at DESC LIMIT 1").single()
            assert row, "No pending draft with an active workspace membership"
            draft = dict(row["p"])
            expired = draft["expires_at"] <= time.time()
            assert not expired or args.refresh_expiry_in_rollback, "Draft expired; explicit rollback-only expiry refresh is required"
            payload = json.loads(draft["payload"])
            before = persistent_state(session, draft["dataset"])
        store = RollbackStore(replace(config, dataset=draft["dataset"]), draft["id"], args.refresh_expiry_in_rollback)
        scope = MobileScope(draft["dataset"], draft["owner_session"], draft["owner_actor"],
                            frozenset(ROLES[row["role"]]), "en", row["workspace"])
        started = time.monotonic()
        try:
            receipt = store.save_pending(draft["id"], scope, "rollback-diagnostic-only")
            seconds = time.monotonic() - started
            report = {"passed": seconds < 25, "save_seconds": round(seconds, 3),
                "statements": store.statements, "item_count": receipt.get("item_count"),
                "objects": len(payload["objects"]), "facts": len(payload["facts"]),
                "draft_ref": hashlib.sha256(draft["id"].encode()).hexdigest()[:16],
                "expiry_refreshed_only_in_rollback": args.refresh_expiry_in_rollback}
        except Exception as error:
            log_failure("pending_save_rollback", error, elapsed=time.monotonic() - started)
            report = {"passed": False, "error_type": type(error).__name__, "statements": store.statements}
        finally:
            store.close()
            with driver.session(database=config.database, default_access_mode=READ_ACCESS) as session:
                after = persistent_state(session, draft["dataset"])
            report["persistent_state_unchanged"] = before == after
            report["passed"] = report.get("passed", False) and before == after
    output = Path(args.report)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
