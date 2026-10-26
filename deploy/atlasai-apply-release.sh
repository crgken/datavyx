#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 || $# -ne 1 ]]; then
  echo "Usage: sudo atlasai-apply-release /home/ubuntu/atlasai-deploy/release.zip" >&2
  exit 2
fi

archive=$(realpath -e -- "$1")
case "$archive" in /home/ubuntu/atlasai-deploy/release-*.zip) ;; *) echo "Release archive must be under /home/ubuntu/atlasai-deploy" >&2; exit 2 ;; esac
stage=$(mktemp -d /tmp/atlasai-release.XXXXXX)
trap 'rm -rf -- "$stage"' EXIT
unzip -q "$archive" -d "$stage"
chmod -R a+rX -- "$stage"

app=/opt/atlasai-knowledge
backend="$stage/knowledge-management-demo"
web="$stage/knowledge-web-pwa/build/web"
test -f "$backend/demo/__main__.py"
test -f "$backend/requirements.lock.txt"
test -f "$web/index.html"

rsync -a --chown=ubuntu:ubuntu --exclude='.env' --exclude='.venv/' "$backend/" "$app/knowledge-management-demo/"
sudo -u ubuntu "$app/knowledge-management-demo/.venv/bin/pip" install --disable-pip-version-check -r "$backend/requirements.lock.txt"
rsync -a --delete --chown=ubuntu:www-data --chmod=D755,F644 "$web/" "$app/knowledge-web-pwa/build/web/"
systemctl restart atlasai-knowledge-api.service

for attempt in {1..20}; do
  code=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 https://kb.datavyx.com/api/mobile/session || true)
  if [[ "$code" == 200 || "$code" == 401 ]]; then
    rm -f -- "$archive"
    echo "Release applied; API responds with HTTP $code."
    exit 0
  fi
  sleep 3
done

systemctl --no-pager --full status atlasai-knowledge-api.service || true
echo "Deployment health check failed (last HTTP status: ${code:-none})." >&2
exit 1
