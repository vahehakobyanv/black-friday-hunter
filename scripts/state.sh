#!/usr/bin/env bash
# Persist the SQLite database between GitHub Actions runs.
#
# Storage: a separate branch (default "data") holding ONE commit with
#   deals.db.gz        current database (gzip)
#   deals.prev.db.gz   the database from the run before (fallback if corrupted)
#   last_report.html   the latest report
# The branch is force-pushed each run, so the repository does not grow by
# megabytes every hour. Extra dated backups are kept as workflow artifacts.
#
# Usage: scripts/state.sh restore|save
set -euo pipefail

BRANCH="${STATE_BRANCH:-data}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$ROOT/data"
mkdir -p "$DATA"

integrity_ok() {
  python3 - "$1" <<'EOF'
import sqlite3, sys
try:
    con = sqlite3.connect(sys.argv[1])
    ok = con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.execute("SELECT count(*) FROM products").fetchone()
    con.close()
except Exception as exc:
    print(f"integrity check failed: {exc}", file=sys.stderr)
    ok = False
sys.exit(0 if ok else 1)
EOF
}

restore() {
  if ! git -C "$ROOT" ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    echo "state: no '$BRANCH' branch yet — starting with an empty database (first run)."
    return 0
  fi
  git -C "$ROOT" fetch --quiet --depth=1 origin "$BRANCH"
  git -C "$ROOT" show FETCH_HEAD:deals.db.gz > "$DATA/deals.db.gz"
  git -C "$ROOT" show FETCH_HEAD:last_report.html > "$DATA/last_report.html" 2>/dev/null || true
  gunzip -c "$DATA/deals.db.gz" > "$DATA/deals.db"
  if integrity_ok "$DATA/deals.db"; then
    echo "state: restored database ($(du -h "$DATA/deals.db" | cut -f1))."
  else
    echo "::warning::state: current database corrupted — falling back to previous run's copy."
    git -C "$ROOT" show FETCH_HEAD:deals.prev.db.gz | gunzip -c > "$DATA/deals.db"
    integrity_ok "$DATA/deals.db" || { echo "::error::previous copy also corrupted; starting fresh"; rm -f "$DATA/deals.db"; }
  fi
  # Keep the restored copy as the "previous" version for the next save.
  cp "$DATA/deals.db.gz" "$DATA/deals.prev.db.gz"
}

save() {
  if [ ! -f "$DATA/deals.db" ]; then
    echo "::warning::state: no database to save."
    return 0
  fi
  if ! integrity_ok "$DATA/deals.db"; then
    echo "::error::state: database failed integrity check — NOT overwriting the stored copy."
    return 1
  fi
  python3 -c "import sqlite3; c=sqlite3.connect('$DATA/deals.db'); c.execute('VACUUM'); c.close()"
  local tmp
  tmp="$(mktemp -d)"
  gzip -9 -c "$DATA/deals.db" > "$tmp/deals.db.gz"
  [ -f "$DATA/deals.prev.db.gz" ] && cp "$DATA/deals.prev.db.gz" "$tmp/deals.prev.db.gz"
  [ -f "$DATA/last_report.html" ] && cp "$DATA/last_report.html" "$tmp/last_report.html"
  cat > "$tmp/README.md" <<EOF
# Deal Hunter state

Machine-written by the hourly workflow. Do not edit by hand.
Updated: $(date -u +%Y-%m-%dT%H:%M:%SZ) by run ${GITHUB_RUN_ID:-local}.
EOF
  cp "$tmp/deals.db.gz" "$DATA/backup.db.gz"   # uploaded as a workflow artifact
  (
    cd "$tmp"
    git init --quiet
    git checkout --quiet --orphan "$BRANCH"
    git add -A
    git -c user.name="deal-hunter-bot" -c user.email="deal-hunter-bot@users.noreply.github.com" \
      commit --quiet -m "state: run ${GITHUB_RUN_ID:-local} $(date -u +%Y-%m-%dT%H:%MZ)"
    if [ -n "${GITHUB_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
      remote="https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"
    else
      remote="$(git -C "$ROOT" remote get-url origin)"
    fi
    for attempt in 1 2 3; do
      if git push --quiet --force "$remote" "HEAD:refs/heads/$BRANCH" 2>/dev/null; then
        echo "state: saved database to branch '$BRANCH' ($(du -h deals.db.gz | cut -f1) compressed)."
        exit 0
      fi
      echo "state: push attempt $attempt failed, retrying…"
      sleep $((attempt * 5))
    done
    echo "::error::state: could not push database"
    exit 1
  )
  rm -rf "$tmp"
}

case "${1:-}" in
  restore) restore ;;
  save) save ;;
  *) echo "usage: $0 restore|save" >&2; exit 2 ;;
esac
