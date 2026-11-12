#!/usr/bin/env bash
set -euo pipefail
# Operator gate after U17 authorization. No database/container/volume operations.
if [[ $EUID -ne 0 || $# -lt 3 ]]; then
  echo "Usage: sudo ATLASAI_RELEASE_AUTHORIZED_COMMIT=COMMIT bash switch-production.sh prepare|activate|open|rollback COMMIT BACKUP_DIR [REVIEWED_NGINX SHA256]" >&2
  exit 2
fi
action=$1
commit=$2
[[ "$commit" =~ ^[0-9a-f]{40}$ && ${ATLASAI_RELEASE_AUTHORIZED_COMMIT:-} == "$commit" ]] || { echo "Exact release authorization marker required" >&2; exit 2; }
backup=$(realpath -e -- "$3")
case "$backup" in /home/ubuntu/atlasai-deploy/backups/mcp-*) ;; *) echo "Unexpected backup directory" >&2; exit 2 ;; esac
test -f "$backup/manifest.json"  # Final consistent backup evidence must exist first.
release="/opt/atlasai-mcp/candidates/$commit"
backend="$release/knowledge-management-demo"
helper="$(dirname -- "$0")/release_artifact.py"
nginx=/etc/nginx/sites-available/kb.datavyx.com
config=/etc/atlasai/mcp.env
state="$backup/mcp-switch.commit"
python3 "$helper" installed --target "$release" --commit "$commit"
[[ $(stat -c %a "$config") == 600 && $(stat -c %U "$config") == root ]] || exit 2
ready() {
  for attempt in {1..15}; do
    if curl --silent --fail --max-time 3 http://127.0.0.1:8099/health/ready >/dev/null; then return 0; fi
    sleep 1
  done
  return 1
}
if [[ "$action" != rollback ]]; then
  ! systemctl is-active --quiet atlasai-knowledge-api.service || { echo "Old requests must first be drained and API stopped under the authorized maintenance plan" >&2; exit 2; }
fi
case "$action" in
prepare)
  install -d -o ubuntu -g ubuntu -m 0700 /var/lib/atlasai-mcp/purge-ledger
  [[ ! -e "$state" ]] || { echo "Preparation already recorded; inspect/reuse it" >&2; exit 2; }
  python3 "$release/deploy/mcp/run_env.py" --env "$config" --python "$backend/.venv-mcp/bin/python" --backend "$backend" --production-closed --action check-config
  umask 077
  cp -- "$nginx" "$backup/kb.datavyx.com.before"
  readlink -f /opt/atlasai-mcp/current > "$backup/mcp-current.before" || true
  printf '%s\n' "$commit" > "$state"
  ln -sfn "$release" /opt/atlasai-mcp/current
  install -o root -g root -m 0644 "$release/deploy/mcp/atlasai-mcp.service" /etc/systemd/system/atlasai-mcp.service
  systemctl daemon-reload
  systemctl restart atlasai-mcp.service
  ready || { systemctl stop atlasai-mcp.service; echo "Preparation failed; writes stay closed. Inspect or explicitly rollback." >&2; exit 1; }
  echo "Production process ready with recording closed; routes not switched."
  ;;
activate)
  [[ $# -eq 5 && $(cat "$state") == "$commit" && $5 =~ ^[0-9a-f]{64}$ ]] || exit 2
  python3 "$release/deploy/mcp/run_env.py" --env "$config" --python "$backend/.venv-mcp/bin/python" --backend "$backend" --production-closed --action check-config
  reviewed=$(realpath -e -- "$4")
  [[ $(sha256sum "$reviewed" | cut -d' ' -f1) == "$5" ]] || { echo "Reviewed nginx checksum mismatch" >&2; exit 2; }
  ready
  install -o root -g root -m 0644 "$reviewed" "$nginx"
  if ! nginx -t; then
    install -o root -g root -m 0644 "$backup/kb.datavyx.com.before" "$nginx"
    echo "Invalid new gateway configuration rejected; live gateway was not reloaded." >&2
    exit 1
  fi
  systemctl disable atlasai-knowledge-api.service
  systemctl enable atlasai-mcp.service
  systemctl reload nginx
  touch "$backup/MCP_ROUTE_ACTIVATED"
  echo "Routes activated; recording remains closed. Verify real auth, old endpoint retirement and data before open."
  ;;
open)
  [[ $(cat "$state") == "$commit" && -f "$backup/MCP_ROUTE_ACTIVATED" ]] || exit 2
  ready
  # Written before config/restart: a crash must never permit unsafe old-volume rollback.
  touch "$backup/NEW_WRITES_MAY_EXIST"
  python3 - "$config" <<'PY'
import os,re,sys
from pathlib import Path
path=Path(sys.argv[1]);text=path.read_text()
line='MCP_RECORDING_ENABLED=1'
text=re.sub(r'^MCP_RECORDING_ENABLED=.*$',line,text,flags=re.M) if re.search(r'^MCP_RECORDING_ENABLED=',text,re.M) else text+'\n'+line+'\n'
temporary=path.with_name('.mcp.env.opening')
fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as stream:stream.write(text)
os.replace(temporary,path)
PY
  systemctl restart atlasai-mcp.service
  ready
  echo "Recording opened. Protect new records/receipts; old-volume rollback is now prohibited."
  ;;
rollback)
  [[ $(cat "$state") == "$commit" && ! -e "$backup/NEW_WRITES_MAY_EXIST" ]] || { echo "Rollback blocked: new writes may exist. Preserve them and use a forward fix." >&2; exit 2; }
  systemctl stop atlasai-mcp.service
  install -o root -g root -m 0644 "$backup/kb.datavyx.com.before" "$nginx"
  nginx -t
  systemctl reload nginx
  previous=$(cat "$backup/mcp-current.before")
  if [[ "$previous" == /opt/atlasai-mcp/candidates/* || "$previous" == /opt/atlasai-mcp/releases/* ]]; then
    ln -sfn "$previous" /opt/atlasai-mcp/current
  fi
  systemctl enable --now atlasai-knowledge-api.service
  echo "Old route/API explicitly restored before reopening writes. No database restore was performed."
  ;;
*) echo "Unknown release phase" >&2; exit 2 ;;
esac
