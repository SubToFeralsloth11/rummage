#!/bin/bash
# Double-click to start Rummage. Close this window (or press Ctrl+C) to quit.
# If Rummage is already running it's reused, unless the code changed since it started:
# then the old copy is stopped and the new version starts (the index is kept).
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1
PORT=47615
URL="http://127.0.0.1:$PORT/"

running_pids() { pgrep -f "$DIR/.venv/bin/rummage serve"; }

code_changed_since() {  # $1 = pid; true if any source file is newer than that process
  local start
  start="$(ps -o lstart= -p "$1" 2>/dev/null)" || return 1
  [ -n "$start" ] || return 1
  start="$(date -j -f "%a %b %d %T %Y" "$start" "+%Y-%m-%d %H:%M:%S" 2>/dev/null)" || return 1
  [ -n "$(find "$DIR/src" "$DIR/pyproject.toml" "$DIR/uv.lock" -type f -newermt "$start" \
          ! -path "*/__pycache__/*" ! -name ".DS_Store" 2>/dev/null | head -1)" ]
}

PIDS="$(running_pids)"
if [ -n "$PIDS" ]; then
  STALE=0
  for p in $PIDS; do code_changed_since "$p" && STALE=1; done
  if [ "$STALE" = 0 ] && curl -s -D - -o /dev/null "$URL/api/status" | grep -qi "^server: Rummage"; then
    echo "Rummage is already running and up to date. Opening it..."
    open "$URL"
    exit 0
  fi
  echo "Stopping the old Rummage so the new version can start..."
  kill $PIDS 2>/dev/null
  for _ in $(seq 1 20); do [ -z "$(running_pids)" ] && break; sleep 0.5; done
  [ -n "$(running_pids)" ] && kill -9 $(running_pids) 2>/dev/null
fi

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is being used by another program, so Rummage can't start."
  lsof -nP -iTCP:$PORT -sTCP:LISTEN
  read -r -p "Press Enter to close."
  exit 1
fi

UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  echo "Rummage needs uv: https://docs.astral.sh/uv/  (curl -LsSf https://astral.sh/uv/install.sh | sh)"
  read -r -p "Press Enter to close."
  exit 1
fi
exec "$UV" run --quiet rummage serve --port "$PORT"
