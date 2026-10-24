#!/bin/bash
# Запускатель программы. Его вызывает «Anonymizer.app» из корня папки проекта (исполняемый файл - программа на C из
# общего сборщика «_общие модули/app», стандарт 6.2); скрипт правится без пересборки. Поднимает сервер (start.sh), ждёт
# /api/info и открывает окно Chrome в режиме приложения. Уже работает - просто открыть окно. Ошибки - окном со ссылкой
# на журнал. ANONYMIZER_NO_WINDOW=1 - без окна (проверка сборки, правило 10).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export LANG="ru_RU.UTF-8" LC_ALL="ru_RU.UTF-8"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
LOG="$HOME/Library/Logs/Anonymizer.log"
PORT="${ANONYMIZER_PORT:-8070}"
WEB="http://127.0.0.1:$PORT"
mkdir -p "$(dirname "$LOG")"
say() { echo "$(date '+%d.%m.%Y %H:%M:%S') $*" >>"$LOG"; }

dialog_error() {
  osascript -e "display dialog \"$1\n\nПодробности - в журнале:\n$LOG\" with title \"Anonymizer\" buttons {\"Открыть журнал\", \"Закрыть\"} default button \"Закрыть\" with icon caution" \
    -e 'if button returned of result is "Открыть журнал" then do shell script "open -a Console " & quoted form of "'"$LOG"'"' >/dev/null 2>&1
}
open_window() {
  [ "${ANONYMIZER_NO_WINDOW:-}" = "1" ] && { say "окно не открываю (ANONYMIZER_NO_WINDOW=1)"; return; }
  if [ -d "/Applications/Google Chrome.app" ]; then open -na "Google Chrome" --args --app="$WEB" --new-window
  else open "$WEB"; fi
}

if [ ! -d "$ROOT" ] || ! cd "$ROOT" 2>/dev/null || ! head -c1 "$ROOT/start.sh" >/dev/null 2>&1; then
  say "нет доступа к папке проекта (защита macOS) или папку перенесли: $ROOT"
  dialog_error "Нет доступа к папке программы:\n$ROOT\n\nЕсли macOS спросила о доступе к «Рабочему столу» - разрешите и запустите ещё раз. Если папку перенесли - соберите приложение заново."
  exit 1
fi
if curl -sf "$WEB/api/info" >/dev/null 2>&1; then
  say "программа уже работает - открываю окно"
  NO_BROWSER=1 ./start.sh >>"$LOG" 2>&1     # сервер старой версии - start.sh перезапустит его
  open_window
  exit 0
fi
say "запуск программы"
if ! NO_BROWSER=1 ./start.sh >>"$LOG" 2>&1; then
  dialog_error "Программа не запустилась за минуту."
  exit 1
fi
say "программа работает, открываю окно"
open_window
