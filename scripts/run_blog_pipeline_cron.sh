#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/apps/minti_studio"
LOG_DIR="$ROOT/logs"
ENV_FILE="$ROOT/.env"
LOG_FILE="$LOG_DIR/blog_pipeline.log"

mkdir -p "$LOG_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

cd "$ROOT"
export PYTHONPATH="$ROOT"

{
  echo "== blog pipeline $(date -Is) =="
  "$ROOT/scripts/blog_scout.py"
  "$ROOT/scripts/blog_judge.py"
} >>"$LOG_FILE" 2>&1
