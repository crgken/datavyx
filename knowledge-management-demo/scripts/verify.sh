#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
runtime_python="${KD_PYTHON:-.venv/bin/python}"
test_container=atlas-knowledge-demo-test-neo4j
if docker inspect "$test_container" >/dev/null 2>&1; then
  # Verify the exact image/port before reusing a pre-existing named container.
  "$runtime_python" scripts/verify_test_container.py
  docker start "$test_container" >/dev/null
else
  docker run -d --name "$test_container" -p 127.0.0.1:17694:7687 -e NEO4J_AUTH=none \
    -e NEO4J_server_memory_heap_initial__size=256m -e NEO4J_server_memory_heap_max__size=512m \
    -e NEO4J_server_memory_pagecache_size=256m neo4j:5.26-community >/dev/null
fi
for attempt in $(seq 1 45); do
  if docker exec "$test_container" cypher-shell 'RETURN 1' >/dev/null 2>&1; then break; fi
  sleep 2
done
"$runtime_python" scripts/acceptance.py "$@"
