"""Batch outbox equivalence and atomicity on disposable Neo4j port 17694."""
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from demo.config import Config
from demo.contracts import ExecutionScope
from demo.model import Change
from demo.store import Store


class CountingTransaction:
    def __init__(self, tx):
        self.tx = tx
        self.queries = 0

    def run(self, *args, **kwargs):
        self.queries += 1
        return self.tx.run(*args, **kwargs)


def main():
    store = Store(Config(uri="bolt://127.0.0.1:17694", dataset="index-batch-" + uuid4().hex))
    scope = ExecutionScope(store.dataset)
    checks = []

    def check(name, passed):
        assert passed, name
        checks.append(name)
        print("PASS " + name, flush=True)

    def save(raw):
        text = "Isolated structured fixture for index batching."
        mid = store.message(text)
        quote = {"message_id": mid, "quote": text}
        raw = {**raw, "schema_version": 3,
            "facts": [{**fact, "evidence": text, "sources": [quote]} for fact in raw.get("facts", [])],
            "entity_updates": [{**update, "sources": [quote]} for update in raw.get("entity_updates", [])]}
        change = Change.model_validate(raw).model_dump()
        draft = store.prepare_execution_change(change, mid, [mid], scope, store.context()["revision"])
        started = time.monotonic()
        receipt = store.save_pending(draft["draft_id"], scope, uuid4().hex)
        return draft, receipt, time.monotonic() - started

    def compare(ids):
        def read(tx):
            prefetched = store._prefetch_index_snapshots(tx, ids)
            for oid in ids:
                for lane in ("approved", "pending"):
                    single = store._index_snapshot(tx, oid, lane)
                    batch = store._index_snapshot(tx, oid, lane, prefetched)
                    assert single == batch, "Snapshot body/hash changed"
                    job = tx.run("MATCH (h:KDIndexHead {dataset:$d,object_id:$id,lane:$lane}), "
                        "(j:KDIndexJob {id:h.job_id}) RETURN j.snapshot AS snapshot,j.snapshot_hash AS hash",
                        d=store.dataset, id=oid, lane=lane).single()
                    assert job and json.loads(job["snapshot"]) == single["body"] and job["hash"] == single["hash"]
        store.read(read)

    try:
        store.initialize()
        objects = []
        facts = []
        for index in range(5):
            client, person = f"client-{index}", f"person-{index}"
            objects.extend([{"ref": client, "name": client, "kind": "Client", "resolution": "new"},
                            {"ref": person, "name": person, "kind": "Person", "resolution": "new"}])
            facts.extend([{"subject": client, "predicate": "city", "value": {"type": "text", "value": f"City {index}"}},
                          {"subject": client, "predicate": "industry", "value": {"type": "text", "value": f"Industry {index}"}},
                          {"subject": client, "predicate": "contact", "object": person}])
        draft, receipt, seconds = save({"objects": objects, "facts": facts})
        ids = [obj["id"] for obj in draft["objects"]]
        compare(ids)
        check("Ten-object pending snapshots equal single-object snapshots", True)
        def repeat(tx):
            counted = CountingTransaction(tx)
            store.enqueue_indexes(counted, ids)
            return counted.queries
        check("Ten-object outbox uses five database statements", store.write(repeat) == 5)
        compare(ids)
        check("Repeated enqueue preserves snapshot hashes", True)
        job = store.index_jobs()[0]
        store.index_result(job["id"], "test-only-episode")
        store.write(repeat)
        ready = store.read(lambda tx: tx.run("MATCH (j:KDIndexJob {id:$id}) RETURN j.status AS status", id=job["id"]).single()["status"])
        check("Unchanged ready job is not reset", ready == "ready")
        batch = store.batches(scope)["rows"][0]
        token = store.prepare_batch_review(scope, batch["id"], [item["id"] for item in batch["items"]], "confirm")
        store.apply_batch_review(scope, token["token"])
        compare(ids)
        check("Confirmed snapshots equal single-object snapshots", True)
        first = draft["objects"][0]
        _, amendment, _ = save({"objects": [{"ref": "client", "name": first["name"], "kind": first["kind"],
            "resolution": "existing", "existing_id": first["id"]}], "entity_updates": [{
                "ref": "client", "field": "name", "previous": first["name"], "value": "Revised client", "reason": "fixture correction"}]})
        compare(ids)
        check("Pending identity proposal preserves snapshot equivalence", True)
        def dependencies(tx):
            counted = CountingTransaction(tx)
            store.enqueue_indexes(counted, [ids[1]], dependency_ids=[ids[1]])
            return counted.queries
        check("Incoming relationship refresh is batched", store.write(dependencies) == 6)
        compare(ids)
        check("Incoming relationship endpoints retain correct heads", True)
        amendment = store.read(lambda tx: store._batch(tx, amendment["batch_id"], scope))
        token = store.prepare_batch_review(scope, amendment["id"], [i["id"] for i in amendment["items"]], "withdraw")
        store.apply_batch_review(scope, token["token"])
        compare(ids)
        check("Withdrawn proposal preserves snapshot equivalence", True)
        rolled_back_id = uuid4().hex
        with store.driver.session(database=store.config.database) as session:
            with session.begin_transaction(timeout=30) as tx:
                tx.run("CREATE (:KDObject {id:$id,dataset:$d,name:'Rollback fixture',kind:'Fixture',identifier:'',"
                    "namespace:'',identity_key:$id,confirmed:false,archived:false,review_status:'pending',semantic_certain:true})",
                    id=rolled_back_id, d=store.dataset).consume()
                store.enqueue_indexes(tx, [rolled_back_id])
                tx.rollback()
        remaining = store.read(lambda tx: tx.run("MATCH (n) WHERE n.id=$id OR "
            "(n:KDIndexHead AND n.dataset=$d AND n.object_id=$id) RETURN count(n) AS n",
            id=rolled_back_id, d=store.dataset).single()["n"])
        orphan_jobs = store.read(lambda tx: tx.run("MATCH (j:KDIndexJob {dataset:$d,object_id:$id}) RETURN count(j) AS n",
            id=rolled_back_id, d=store.dataset).single()["n"])
        check("Rollback removes both canonical record and outbox writes", remaining == 0 and orphan_jobs == 0)
        report = {"dataset": store.dataset, "checks": checks, "save_seconds": round(seconds, 3)}
        output = Path(__file__).resolve().parent.parent / "artifacts/index-batch-acceptance.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
