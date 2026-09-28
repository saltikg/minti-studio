#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/ubuntu/apps/minti_studio"
LOG_DIR="$ROOT/logs"
ENV_FILE="$ROOT/.env"
MODE="${1:-}"
shift || true

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

case "$MODE" in
  daily-metrics)
    /usr/bin/nice -n 15 /usr/bin/ionice -c3 \
      "$ROOT/.venv/bin/python" app/video_shorts/tasks/daily_video_metrics_snapshot.py --quiet \
      >>"$LOG_DIR/daily_video_metrics.log" 2>&1
    exec /usr/bin/nice -n 15 /usr/bin/ionice -c3 \
      "$ROOT/.venv/bin/python" app/video_shorts/tasks/daily_subscriber_snapshot.py \
      >>"$LOG_DIR/daily_subscribers.log" 2>&1
    ;;
  youtube-traffic)
    exec /usr/bin/nice -n 15 /usr/bin/ionice -c3 \
      "$ROOT/.venv/bin/python" app/video_shorts/tasks/youtube_traffic_sources_snapshot.py "$@" \
      >>"$LOG_DIR/youtube_traffic_sources.log" 2>&1
    ;;
  *)
    echo "Usage: $0 {daily-metrics|youtube-traffic} [args...]" >&2
    exit 2
    ;;
esac
