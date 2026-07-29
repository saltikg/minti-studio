#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/apps/minti_studio"
LOG_DIR="$ROOT/logs"
ENV_FILE="$ROOT/.env"
MODE="${1:-publish}"

mkdir -p "$LOG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

pick_log_file() {
  local target="$1"
  if touch "$target" 2>/dev/null; then
    printf '%s\n' "$target"
    return 0
  fi
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  printf '%s\n' "${target%.log}.fallback.${ts}.log"
}

if [[ "$MODE" == "youtube-comments" ]]; then
  LOG_FILE="$(pick_log_file "$LOG_DIR/social_youtube_comments.log")"
  COMMENT_SCAN_SCOPE="recent50"
  if (( $(date -u +%H) % 4 == 0 )); then
    COMMENT_SCAN_SCOPE="all"
  fi
  CMD=(
    "$ROOT/.venv/bin/python"
    "app/video_shorts/tasks/run_social_jobs.py"
    "--only-youtube-comments"
    "--comment-scan-scope" "$COMMENT_SCAN_SCOPE"
  )
else
  LOG_FILE="$(pick_log_file "$LOG_DIR/social_all.log")"
  CMD=(
    "$ROOT/.venv/bin/python"
    "app/video_shorts/tasks/run_social_jobs.py"
    "--max-instagram" "5"
    "--max-tiktok" "5"
    "--comments-limit" "0"
    "--skip-youtube-comments"
  )
fi

cd "$ROOT"
export PYTHONPATH="$ROOT"
exec "${CMD[@]}" >>"$LOG_FILE" 2>&1
