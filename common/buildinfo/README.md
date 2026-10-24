# buildinfo - окно и ярлык не говорят со старым сервером (стандарт 6.5)

Версия 1.0.0 · 25.10.2026 · «_Порядок 03», п. 1. Эталоны: Interview Coach и «Понятный документ» `app/fingerprint.py`
(отпечаток по времени файлов), LDB `ldb/buildinfo.py` (fresh / stale / old / down / foreign), Настольный помощник
`tabletop/buildinfo.py` (хеш содержимого, `build` в `/api/info`).

## Зачем

Ошибка 24.10 (Настольный помощник): номер версии «0.1.0» не менялся между заданиями, ярлык увидел «программа уже
работает» и открыл окно к серверу, запущенному до задания 03, - «Спросить» и «Найти» молчали. Номер версии не годится:
сверяется **код сборки** - хеш содержимого файлов кода и окна.

## Как работает

| Где | Что |
|---|---|
| сервер | `BUILD = bi.at_start()` при старте; `/api/info` → `{…, **bi.info()}` = `build`, `disk_build`, `stale`; `POST /api/restart` → `bi.restart_later()` (тот же процесс заново через `os.execv`: PID тот же, порт освобождается) |
| ярлык (`desktop/launcher.sh`), `start.sh` | «уже работает» → `bi_ensure_fresh` из `launch.sh`: сборка старая - `POST /api/restart`, ждать `fresh`, в журнал строка «код обновился … - перезапускаю сервер»; сервер старый без `/api/restart` - запускатель останавливает его своим `stop.sh` и запускает заново |
| окно | `web/buildinfo.js`: при старте и раз в минуту `/api/info`; `build ≠ disk_build` (сервер старше кода) или `build` сменился с момента открытия окна (окно старше сервера) - полоса «Программа обновилась - перезапустите» и «Перезапустить» (RU / EN); своя полоса программы - `BuildInfo.watch({onStale})` |

Что считать кодом - `buildinfo.json` в корне проекта, например `{"parts": ["app", "cleardoc", "web/static", "common"]}`;
файлы `.py .js .mjs .css .html .json .yaml .yml .toml`, без `__pycache__`, `tests`, `.venv`, `node_modules`.

## Как подключить

1. Копия: `scripts/sync_common.sh buildinfo <проект>` → `<проект>/common/buildinfo/`; `buildinfo.json` в корне.
2. Сервер: `/api/info` + `POST /api/restart`; раздать `common/buildinfo/web/` по адресу `/common/buildinfo/`.
3. Окно: `<script src="/common/buildinfo/buildinfo.js"></script>` и `BuildInfo.watch({lang})`.
4. Ярлык: `. "$ROOT/common/buildinfo/launch.sh"` и `bi_ensure_fresh "$INFO" "$PY" "$ROOT" "$LOG"` в ветке «уже работает».
5. Проверка в программе: `common.buildinfo.testing.check_restart(cmd, port, root, env)` - сервер старой сборки
   (`BUILDINFO_AT_START`) → окно показывает полосу → ярлык перезапускает → окно видит новый `build`.

## Проверка модуля

`<python с playwright> _общие модули/buildinfo/tests/test_buildinfo.py` - хеш по содержимому (без кэша и не-кода),
`down` для пустого порта, полная проверка перезапуска на сервере-образце (`tests/sample_server.py`).

## Где подключён

Anonymizer, Legal Knowledge Desk, Legal Doc Builder, Interview Coach, «Понятный документ» (25.10); Настольный помощник -
после задания 04 (его `tabletop/buildinfo.py` → на модуль). Herald и Scribe (окна Tk) - не нужно.

## Журнал

- 1.0.0 (25.10.2026) - первая версия.
