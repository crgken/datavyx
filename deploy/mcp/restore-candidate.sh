#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -ne 0 || $# -ne 1 || ! $1 =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: sudo bash restore-candidate.sh PREVIOUS_VERIFIED_COMMIT" >&2
  exit 2
fi
target="/opt/atlasai-mcp/candidates/$1"
test -x "$target/knowledge-management-demo/.venv-mcp/bin/python"
python3 "$(dirname -- "$0")/release_artifact.py" installed --target "$target" --commit "$1"
ln -sfn "$target" /opt/atlasai-mcp/candidate
systemctl restart atlasai-mcp-candidate.service
for attempt in {1..15}; do
  if curl --silent --fail --max-time 3 http://127.0.0.1:8098/health/ready >/dev/null; then
    echo "Previous candidate restored; production and databases unchanged."
    exit 0
  fi
  sleep 1
done
echo "Candidate restore health failed; production was not changed." >&2
exit 1
