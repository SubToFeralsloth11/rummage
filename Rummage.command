#!/bin/bash
# Double-click to start Rummage. Close this window (or press Ctrl+C) to quit.
cd "$(dirname "$0")" || exit 1
PORT=47615
if curl -s -D - -o /dev/null "http://127.0.0.1:$PORT/api/status" | grep -qi "^server: Rummage"; then
  echo "Rummage is already running. Opening it..."
  open "http://127.0.0.1:$PORT/"
  exit 0
fi
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
if [ ! -x "$UV" ]; then
  echo "Rummage needs uv: https://docs.astral.sh/uv/  (curl -LsSf https://astral.sh/uv/install.sh | sh)"
  read -r -p "Press Enter to close."
  exit 1
fi
exec "$UV" run --quiet rummage serve --port "$PORT"
