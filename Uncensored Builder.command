#!/bin/bash
# Doble clic (alternativa a la .app): mismo comportamiento que UncensoredBuilder.app
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
PORT="${UNCENSORED_BUILDER_PORT:-7788}"
if [[ ! -f "$DIR/venv/bin/activate" ]]; then
  python3 -m venv "$DIR/venv"
  "$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"
fi
# shellcheck disable=SC1091
source "$DIR/venv/bin/activate"
mkdir -p "$DIR/memory"
LOG_FILE="$DIR/memory/builder_server.log"
if lsof -iTCP:"$PORT" -sTCP:LISTEN -P -n >/dev/null 2>&1; then
  gq=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 --max-time 5 "http://127.0.0.1:${PORT}/api/groq/test" 2>/dev/null || echo "000")
  orc=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 --max-time 5 "http://127.0.0.1:${PORT}/api/openrouter/test" 2>/dev/null || echo "000")
  if [[ "$gq" == "200" && "$orc" == "200" ]]; then
    open "http://127.0.0.1:${PORT}/dashboard"
    exit 0
  fi
  if [[ "$gq" == "404" || "$orc" == "404" ]]; then
    echo "$(date "+%Y-%m-%d %H:%M:%S") Reiniciando servidor antiguo (404 test Groq/OpenRouter)…" >>"$LOG_FILE"
    pids=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
    if [[ -n "${pids:-}" ]]; then
      kill $pids 2>/dev/null || true
      sleep 1.2
    fi
  else
    open "http://127.0.0.1:${PORT}/dashboard"
    exit 0
  fi
fi
nohup python3 "$DIR/builder_server.py" >>"$LOG_FILE" 2>&1 &
echo $! >"$DIR/memory/builder_server.pid"
sleep 1.2
open "http://127.0.0.1:${PORT}/dashboard"
exit 0
