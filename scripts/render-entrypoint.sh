#!/bin/sh
# Container entrypoint for single-service hosts (Render, Fly, a plain VM).
#
# The API and the arq worker normally run as separate containers. A host that
# only allows one web service can run both here: set EMBED_WORKER=1 and the
# worker starts in the background while the API keeps the HTTP port.
# EMBED_CRON_WORKER=0 keeps the cron scheduler off this machine.
set -eu

if [ "${EMBED_WORKER:-0}" = "1" ]; then
  echo "Starting embedded worker (video + cron) alongside the API"
  .venv/bin/python -m src.workers.run_workers &
  WORKER_PID=$!

  # If the worker dies the API must not keep accepting work it cannot finish.
  shutdown() {
    if kill -0 "$WORKER_PID" 2>/dev/null; then
      kill -TERM "$WORKER_PID" 2>/dev/null || true
    fi
  }
  trap shutdown TERM INT
fi

exec .venv/bin/uvicorn src.main:app --host 0.0.0.0 --port "${PORT:-8000}"
