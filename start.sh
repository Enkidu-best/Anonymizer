#!/usr/bin/env bash
# Anonymizer: поднять одной командой и открыть окно (стандарт 6.2).
#   окружение Python -> сервер на 127.0.0.1:8070 (он же отдаёт окно) -> проверка /api/info -> окно Chrome.
#   NO_BROWSER=1 - без окна (проверки); ANONYMIZER_PORT - другой порт (тесты - 8074); ANONYMIZER_DATA_DIR - своя папка.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
PORT="${ANONYMIZER_PORT:-8070}"
RUN="$ROOT/.run"; mkdir -p "$RUN"
PY="$ROOT/.venv312/bin/python"
LOCK="$RUN/start.lock"            # двойной щелчок по значку - второй запуск не мешает первому
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -f "$LOCK/pid" ] && kill -0 "$(cat "$LOCK/pid")" 2>/dev/null; then
    echo "Программа уже запускается (pid $(cat "$LOCK/pid")) - дождитесь окна"; exit 0
  fi
  rm -rf "$LOCK"; mkdir "$LOCK"
fi
echo $$ >"$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

echo "1/3 Окружение Python"
if [ ! -x "$PY" ]; then
  python3.12 -m venv .venv312
  "$PY" -m pip install -q --upgrade pip
  "$PY" -m pip install -q -r requirements.txt
fi
"$PY" -c "import flask, docx, pymupdf, spacy, pymorphy3, platformdirs" 2>/dev/null || "$PY" -m pip install -q -r requirements.txt

echo "2/3 Сервер"
. "$ROOT/common/buildinfo/launch.sh"
running="$(curl -sf "http://127.0.0.1:$PORT/api/info" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d.get("version","") if d.get("name")=="Anonymizer" else "")' 2>/dev/null || true)"
# код сборки (стандарт 6.5): сервер старше кода на диске - перезапустить; старый без /api/restart - остановить и запустить
if [ -n "$running" ] && ! bi_ensure_fresh "http://127.0.0.1:$PORT/api/info" "$PY" "$ROOT" "$RUN/server.log"; then
  echo "  сервер старой сборки - останавливаю и запускаю заново"
  curl -sf -X POST "http://127.0.0.1:$PORT/api/shutdown" >/dev/null || true
  for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/api/info" >/dev/null || break; sleep 0.3; done
  running=""
fi
if [ -z "$running" ]; then
  ANONYMIZER_PORT="$PORT" nohup "$PY" app.py >>"$RUN/server.log" 2>&1 &
  echo $! >"$RUN/app.pid"
  echo "  запущен (pid $!)"
else
  echo "  уже работает ($running)"
fi

echo "3/3 Ждём готовности"
for _ in $(seq 1 120); do curl -sf "http://127.0.0.1:$PORT/api/info" >/dev/null && break; sleep 0.5; done
curl -sf "http://127.0.0.1:$PORT/api/info" >/dev/null || { echo "Сервер не ответил за минуту - см. $RUN/server.log"; exit 1; }
echo "Окно: http://127.0.0.1:$PORT"
if [ "${NO_BROWSER:-}" != "1" ]; then
  if [ -d "/Applications/Google Chrome.app" ]; then open -na "Google Chrome" --args --app="http://127.0.0.1:$PORT" --new-window
  else open "http://127.0.0.1:$PORT"; fi
fi
