#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/apps/minti_studio"
LOG_DIR="$ROOT/logs"
ENV_FILE="$ROOT/.env"
BLOG_SCOUT_ENV_FILE="/etc/minti/blog-scout.env"
LOG_FILE="$LOG_DIR/blog_pipeline.log"

mkdir -p "$LOG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
# Proxy config is intentionally duplicated from the worker drop-ins; centralize later (planned infra cleanup).
if [[ -r "$BLOG_SCOUT_ENV_FILE" ]]; then
  # shellcheck disable=SC1091
  source "$BLOG_SCOUT_ENV_FILE"
fi
set +a

cd "$ROOT"
export PYTHONPATH="$ROOT"

{
  echo "== blog pipeline $(date -Is) =="
  "$ROOT/scripts/blog_scout.py"
  "$ROOT/scripts/blog_judge.py"
} >>"$LOG_FILE" 2>&1
