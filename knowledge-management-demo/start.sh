#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ ! -f .env ]]; then cp .env.example .env; fi
runtime_python="${KD_PYTHON:-.venv/bin/python}"
if [[ ! -x "$runtime_python" ]]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install --no-compile -r requirements.lock.txt
  runtime_python=.venv/bin/python
fi
docker compose up -d --wait --wait-timeout 150
exec "$runtime_python" -m demo "$@"
