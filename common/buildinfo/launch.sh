# Общий блок buildinfo (стандарт 6.5): для запускателей (desktop/launcher.sh) и start.sh. Подключение:
#   . "$ROOT/common/buildinfo/launch.sh"
#   bi_state  <url /api/info> <python> <корень проекта>        → fresh | stale | old | down | foreign
#   bi_ensure_fresh <url /api/info> <python> <корень> <журнал>  → 0: сервер на коде с диска (перезапущен, если был старый);
#                                                                1: не отвечает или старый без /api/restart - запускатель
#                                                                   останавливает его сам (stop.sh) и запускает заново
# Перезапуск - POST <…>/api/restart (сервер перезапускает сам себя, порт и PID те же), в журнал - строка.

bi_state() {
  (cd "$3" 2>/dev/null && "$2" -m common.buildinfo check "$1" 2>/dev/null) || echo down
}

bi_ensure_fresh() {
  local url="$1" py="$2" root="$3" log="${4:-/dev/null}" st base
  st="$(bi_state "$url" "$py" "$root")"
  case "$st" in
    fresh) return 0 ;;
    down|foreign) return 1 ;;
  esac
  base="${url%/api/info}"
  echo "$(date '+%d.%m.%Y %H:%M:%S') код обновился (сервер: $st, на диске: $(cd "$root" && "$py" -m common.buildinfo hash 2>/dev/null)) - перезапускаю сервер" >>"$log"
  curl -sf -m 5 -X POST -H 'Content-Type: application/json' -d '{}' "$base/api/restart" >/dev/null 2>&1 || return 1
  for _ in $(seq 1 120); do
    sleep 0.5
    [ "$(bi_state "$url" "$py" "$root")" = fresh ] && return 0
  done
  return 1
}
