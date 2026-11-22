#!/usr/bin/env bash
# Unified local launcher & status manager for Yami Learning Companion:
#   1) yami-api             (FastAPI + Guardian Web on :8000)
#   2) yami-worker          (Reminders + Graphiti Outbox worker)
#   3) yami-dingtalk-stream (DingTalk Stream Mode outbound bot daemon)
#   4) yami-dws-poller      (Automatic dws CLI school-notice/group poller)
#
# Usage:
#   ./scripts/yami_up.sh start
#   ./scripts/yami_up.sh stop
#   ./scripts/yami_up.sh restart
#   ./scripts/yami_up.sh status

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT_DIR/.yami_run"
VENV_PY="$ROOT_DIR/.venv/bin/python"
VENV_UVICORN="$ROOT_DIR/.venv/bin/uvicorn"

export PATH="$HOME/.local/bin:$PATH"
mkdir -p "$RUN_DIR"

start_proc() {
  local name="$1"
  shift
  local pid_file="$RUN_DIR/${name}.pid"
  local log_file="$RUN_DIR/${name}.log"

  if [[ -f "$pid_file" ]]; then
    local old_pid
    old_pid="$(cat "$pid_file")"
    if kill -0 "$old_pid" 2>/dev/null; then
      echo "[OK] $name already running (pid=$old_pid)"
      return 0
    fi
    rm -f "$pid_file"
  fi

  (
    cd "$ROOT_DIR"
    nohup "$@" >> "$log_file" 2>&1 &
    echo $! > "$pid_file"
  )
  sleep 0.5
  local new_pid
  new_pid="$(cat "$pid_file")"
  if kill -0 "$new_pid" 2>/dev/null; then
    echo "[STARTED] $name (pid=$new_pid) -> $log_file"
  else
    echo "[FAILED] $name failed to start; inspect $log_file"
    return 1
  fi
}

stop_proc() {
  local name="$1"
  local pid_file="$RUN_DIR/${name}.pid"
  if [[ ! -f "$pid_file" ]]; then
    echo "[STOPPED] $name (no pid file)"
    return 0
  fi
  local pid
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    sleep 0.4
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
    echo "[STOPPED] $name (pid=$pid)"
  else
    echo "[CLEANED] $name stale pid=$pid"
  fi
  rm -f "$pid_file"
}

status_proc() {
  local name="$1"
  local pid_file="$RUN_DIR/${name}.pid"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "[RUNNING] $name (pid=$pid) | log: $RUN_DIR/${name}.log"
      return 0
    fi
  fi
  echo "[DOWN]    $name"
}

cmd="${1:-status}"
case "$cmd" in
  start)
    start_proc "yami-api" "$VENV_UVICORN" backend.api.main:app --reload --host 0.0.0.0 --port 8000
    start_proc "yami-worker" "$VENV_PY" -m backend.worker
    start_proc "yami-dingtalk-stream" "$VENV_PY" -m backend.adapters.dingtalk_stream
    start_proc "yami-dws-poller" "$VENV_PY" -m backend.adapters.dws_poller --interval 120
    echo ""
    echo "Guardian Web: http://127.0.0.1:8000/guardian"
    ;;
  stop)
    stop_proc "yami-dws-poller"
    stop_proc "yami-dingtalk-stream"
    stop_proc "yami-worker"
    stop_proc "yami-api"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    status_proc "yami-api"
    status_proc "yami-worker"
    status_proc "yami-dingtalk-stream"
    status_proc "yami-dws-poller"
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
