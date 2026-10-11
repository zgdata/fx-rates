#!/usr/bin/env bash
# Box-side fetcher for sources that block GitHub runners (sources_config.json -> box_only).
# Cron calls this every 30 min. schedule_config.json "box" decides which sources are due (local
# mornings + midday in their own time zone; a missed window is caught up at the next call, e.g.
# after the box was off). The hourly GitHub workflow merges box_sources.json (36 h stale rule).
# Use --force to fetch all box sources immediately.
set -uo pipefail
export HOME="${HOME:-/home/box}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"
REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO" || exit 1
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

FORCE=""
[ "${1:-}" = "--force" ] && FORCE="--force"
if [ -z "$FORCE" ]; then
  due=$(python3 fetch_rates.py --box-due 2>/dev/null) || exit 0     # nothing due -> silent exit
else
  due="all"
fi

exec 9>/tmp/fx-rates-box.lock
flock -n 9 || { echo "$(ts) already running"; exit 0; }

echo "=== $(ts) box run start (due: $due)"
git pull --rebase --autostash -q || { echo "$(ts) git pull failed"; exit 1; }
python3 fetch_rates.py --box-run $FORCE || echo "$(ts) fetch reported no ok source"
if [ -z "$(git status --porcelain -- box_sources.json)" ]; then
  echo "$(ts) box_sources.json unchanged – nothing to commit"
else
  git add box_sources.json
  git -c user.name=zgdata -c user.email=zgdata@users.noreply.github.com commit -q -m "box: $(ts) [$due]"
  for i in 1 2 3; do
    git push -q && { echo "$(ts) pushed"; break; }
    echo "$(ts) push failed, retry $i"; sleep 10; git pull --rebase -q
  done
fi
echo "=== $(ts) box run end"
