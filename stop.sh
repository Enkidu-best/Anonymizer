#!/usr/bin/env bash
# Остановить программу. Ollama не останавливаем: она общая для других проектов.
PORT="${ANONYMIZER_PORT:-8070}"
RUN="$(cd "$(dirname "$0")" && pwd)/.run"
if curl -sf -X POST "http://127.0.0.1:$PORT/api/shutdown" >/dev/null 2>&1; then
  for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/api/info" >/dev/null 2>&1 || break; sleep 0.3; done
  echo "Anonymizer остановлен"
else
  echo "Anonymizer не запущен"
fi
rm -f "$RUN/app.pid"
