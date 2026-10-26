"""Roles alone do not prove an existing entity classification is incorrect."""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from demo.config import Config, load_model_env
from demo.graphiti_extension import Pipeline
from demo.service import Service
from demo.store import Store
from fixtures import commit

checks, trace = [], []


async def run():
    for name, kind, text in [
        ("王总", "客户", "给王总增加一条关系：王总是广通公司的联系人。"),
        ("顾工", "员工", "顾工还担任澄明研究所的采购经办人，补上这个关联。"),
    ]:
        store = Store(Config(uri="bolt://127.0.0.1:17694", password="", dataset="roles_" + uuid4().hex))
        store.initialize()
        seed = commit(store, "已有对象：" + name, {"objects": [{"ref": "p", "name": name, "kind": kind}], "facts": []})
        oid = seed["objects"][0]["id"]
        pipeline = Pipeline(store.config)
        service = Service(store, pipeline)
        try:
            result = await service.chat(text)
            trace.append({"text": text, "result": result})
            draft = result.get("draft")
            values = [("role_relation_preview_" + name, bool(draft))]
            if draft:
                values.extend([("role_no_automatic_type_change_" + name, not draft["entity_updates"]),
                               ("role_preserves_id_" + name, any(o["id"] == oid for o in draft["objects"])),
                               ("role_adds_relation_" + name, any(f["object"] for f in draft["facts"]))])
                await service.confirm(draft["draft_id"], draft["approval_id"], draft["revision"])
                values.append(("role_current_type_" + name, store.detail(oid)["object"]["kind"] == kind))
            for label, ok in values:
                checks.append({"name": label, "passed": bool(ok)})
                print(("PASS " if ok else "FAIL ") + label, flush=True)
        finally:
            await service.runtime.close()
            await pipeline.close()
            store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-env", required=True)
    args = parser.parse_args()
    load_model_env(args.model_env)
    os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
    try:
        asyncio.run(run())
    except Exception as exc:
        checks.append({"name": "execution_complete", "passed": False, "error_type": type(exc).__name__})
    report = {"passed": sum(c["passed"] for c in checks), "failed": sum(not c["passed"] for c in checks), "checks": checks, "trace": trace}
    (ROOT / "artifacts" / "role-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Passed {report['passed']}; failed {report['failed']}", flush=True)
    raise SystemExit(bool(report["failed"]))


if __name__ == "__main__":
    main()
