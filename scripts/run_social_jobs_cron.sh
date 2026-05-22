#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/apps/minti_studio"
LOG_DIR="$ROOT/logs"
MODE="${1:-publish}"

mkdir -p "$LOG_DIR"

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
  CMD=(
    "$ROOT/.venv/bin/python"
    "app/video_shorts/tasks/run_social_jobs.py"
    "--only-youtube-comments"
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
