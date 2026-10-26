"""Read the actual installed package, not guessed APIs or prior documentation."""
import hashlib
import argparse
import importlib.metadata
import json
from pathlib import Path


def audit(write=False):
    root = Path(importlib.metadata.distribution("graphiti-core").locate_file("graphiti_core"))
    files = ["graphiti.py", "utils/maintenance/node_operations.py", "utils/maintenance/edge_operations.py",
             "driver/neo4j_driver.py", "llm_client/openai_generic_client.py"]
    result = {"distribution": "graphiti-core", "version": importlib.metadata.version("graphiti-core"),
              "git_commit": None, "note": "Installed distribution has no git commit metadata; version and exact source hashes identify the inspected baseline.",
              "files": {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}}
    target = Path(__file__).resolve().parent.parent / "docs" / "graphiti-source-lock.json"
    if write:
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    else:
        expected = json.loads(target.read_text(encoding="utf-8"))
        if result["version"] != expected["version"] or result["files"] != expected["files"]:
            raise SystemExit("Installed Graphiti source differs from the audited baseline; inspect differences before updating the lock.")
        result["matches_audited_source"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="Explicitly replace the source lock after review.")
    audit(parser.parse_args().write)
