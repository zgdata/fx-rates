#!/usr/bin/env bash
# Box-side fetcher for sources that block GitHub runners (sources_config.json -> box_only).
# Cron calls this every hour at :47. It does real work when the UTC hour is 04 (before the
# 05:17 UTC GitHub workflow), or as catch-up whenever box_sources.json is older than 23 h
# (e.g. the box was off at 04:47). Use --force to run immediately.
set -uo pipefail
export HOME="${HOME:-/home/box}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO" || exit 1
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

age_h=999
if [ -f box_sources.json ]; then
  age_h=$(python3 -c "import json,datetime as d;g=json.load(open('box_sources.json'))['generated_at'];print(int((d.datetime.now(d.timezone.utc)-d.datetime.fromisoformat(g)).total_seconds()//3600))" 2>/dev/null || echo 999)
fi
if [ "${1:-}" != "--force" ] && [ "$(date -u +%H)" != "04" ] && [ "$age_h" -lt 23 ]; then
  exit 0
fi

exec 9>/tmp/fx-rates-box.lock
flock -n 9 || { echo "$(ts) already running"; exit 0; }

echo "=== $(ts) box run start (age ${age_h}h)"
git pull --rebase --autostash -q || { echo "$(ts) git pull failed"; exit 1; }
python3 fetch_rates.py --box-run || echo "$(ts) fetch reported no ok source"
if [ -z "$(git status --porcelain -- box_sources.json)" ]; then
  echo "$(ts) box_sources.json unchanged – nothing to commit"
else
  git add box_sources.json
  git -c user.name=zgdata -c user.email=zgdata@users.noreply.github.com commit -q -m "box: $(ts)"
  for i in 1 2 3; do
    git push -q && { echo "$(ts) pushed"; break; }
    echo "$(ts) push failed, retry $i"; sleep 10; git pull --rebase -q
  done
fi
echo "=== $(ts) box run end"
