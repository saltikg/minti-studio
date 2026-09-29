#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/apps/minti_studio"
ENV_FILE="$ROOT/.env"
LOG_FILE="$ROOT/logs/restart_discovery_worker_when_idle.log"
MAX_SECONDS="${DISCOVERY_RESTART_IDLE_MAX_SECONDS:-3600}"
INTERVAL_SECONDS="${DISCOVERY_RESTART_IDLE_INTERVAL_SECONDS:-30}"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  printf '%s %s\n' "$(date -Is)" "$*" >>"$LOG_FILE"
}

if [[ ! -f "$ENV_FILE" ]]; then
  log "missing env file: $ENV_FILE"
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

deadline=$(( $(date +%s) + MAX_SECONDS ))
log "waiting for discovery worker to become idle max_seconds=$MAX_SECONDS interval_seconds=$INTERVAL_SECONDS"

while (( $(date +%s) < deadline )); do
  busy_count="$(
    cd "$ROOT"
    "$ROOT/.venv/bin/python" - <<'PY'
from app.video_shorts.services.db import get_db_readonly, table_columns

conn = get_db_readonly()
try:
    count = 0
    count += int(conn.execute(
        """
        SELECT COUNT(*)
        FROM shorts_render_jobs
        WHERE status = 'processing'
          AND COALESCE(payload_json->>'job_origin', '') = 'discovery_demo'
        """
    ).fetchone()[0] or 0)
    if table_columns(conn, "discovery_promote_requests"):
        count += int(conn.execute(
            """
            SELECT COUNT(*)
            FROM discovery_promote_requests
            WHERE status = 'processing'
            """
        ).fetchone()[0] or 0)
    print(count)
finally:
    conn.close()
PY
  )"
  if [[ "$busy_count" == "0" ]]; then
    log "discovery worker idle; restarting service"
    sudo systemctl restart minti_studio_discovery_worker.service
    log "discovery worker restart requested"
    exit 0
  fi
  log "discovery worker busy; processing_jobs=$busy_count"
  sleep "$INTERVAL_SECONDS"
done

log "timed out waiting for discovery worker idle; no restart requested"
exit 0
