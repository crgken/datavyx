#!/usr/bin/env bash
set -euo pipefail
# Isolated candidate only. Does not stop old API/worker, edit nginx or touch production DB.
if [[ $EUID -ne 0 || $# -ne 3 ]]; then
  echo "Usage: sudo bash install-candidate.sh ARCHIVE SHA256 COMMIT" >&2
  exit 2
fi
archive=$(realpath -e -- "$1")
case "$archive" in /home/ubuntu/atlasai-deploy/mcp/release-*.tar) ;; *) echo "Unexpected upload path" >&2; exit 2 ;; esac
[[ $2 =~ ^[0-9a-f]{64}$ && $3 =~ ^[0-9a-f]{40}$ ]] || exit 2
helper=$(realpath -e -- "$(dirname -- "$0")/release_artifact.py")
config=/etc/atlasai/mcp-candidate.env
test -f "$config"
[[ $(stat -c %a "$config") == 600 && $(stat -c %U "$config") == root ]] || { echo "Config must be root-owned mode 600" >&2; exit 2; }
release="/opt/atlasai-mcp/candidates/$3"
python3 "$helper" verify "$archive" --sha256 "$2" --commit "$3"
# Reuse the exact verified source and completed dependency environment.
if [[ -e "$release" ]]; then
  python3 "$helper" installed --target "$release" --commit "$3"
else
  mkdir -p /opt/atlasai-mcp/candidates
  python3 "$helper" extract "$archive" --sha256 "$2" --commit "$3" --target "$release"
fi
backend="$release/knowledge-management-demo"
if [[ ! -x "$backend/.venv-mcp/bin/python" ]]; then python3.12 -m venv "$backend/.venv-mcp"; fi
lock_hash=$(sha256sum "$backend/requirements-mcp.lock.txt" | cut -d' ' -f1)
if [[ ! -f "$release/.mcp-dependencies.sha256" || $(cat "$release/.mcp-dependencies.sha256") != "$lock_hash" ]]; then
  "$backend/.venv-mcp/bin/pip" install --disable-pip-version-check -r "$backend/requirements-mcp.lock.txt"
  printf '%s\n' "$lock_hash" > "$release/.mcp-dependencies.sha256"
fi
"$backend/.venv-mcp/bin/pip" check
python3 "$release/deploy/mcp/run_env.py" --env "$config" --python "$backend/.venv-mcp/bin/python" --backend "$backend" --candidate --action check-config
python3 "$release/deploy/mcp/run_env.py" --env "$config" --python "$backend/.venv-mcp/bin/python" --backend "$backend" --candidate --action migrate
previous=$(readlink -f /opt/atlasai-mcp/candidate || true)
ln -sfn "$release" /opt/atlasai-mcp/candidate
install -o root -g root -m 0644 "$release/deploy/mcp/atlasai-mcp-candidate.service" /etc/systemd/system/atlasai-mcp-candidate.service
systemctl daemon-reload
systemctl restart atlasai-mcp-candidate.service
for attempt in {1..15}; do
  if curl --silent --fail --max-time 3 http://127.0.0.1:8098/health/ready >/dev/null; then
    echo "Candidate ready; commit=$3; old production unchanged; real OAuth/client validation still required."
    exit 0
  fi
  sleep 1
done
systemctl stop atlasai-mcp-candidate.service
if [[ "$previous" == /opt/atlasai-mcp/candidates/* ]]; then
  ln -sfn "$previous" /opt/atlasai-mcp/candidate
  systemctl start atlasai-mcp-candidate.service
fi
echo "Candidate health failed; previous candidate restored when available. No production action taken." >&2
exit 1
