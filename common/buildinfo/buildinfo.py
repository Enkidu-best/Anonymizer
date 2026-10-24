"""Код сборки (стандарт 6.5) - окно и ярлык не говорят со старым сервером.

`build` - хеш содержимого файлов кода и окна в момент старта сервера; `disk_build` - то же по файлам на диске сейчас.
Разные - сервер запущен до правки кода: ярлык перезапускает сервер (`launch.sh`), окно показывает «Программа обновилась»
(`web/buildinfo.js`). Ошибка 24.10 (Настольный помощник): версия «0.1.0» во всех заданиях, ярлык открыл окно к серверу
до задания 03 - кнопки молчали. Эталоны: Interview Coach и «Понятный документ» `app/fingerprint.py`, LDB
`ldb/buildinfo.py`, Настольный помощник `tabletop/buildinfo.py`.

Что считать кодом - `buildinfo.json` в корне проекта: {"parts": ["app", "web/static", "common"]}. Только стандартная
библиотека: запускатель вызывает `python -m common.buildinfo check …`, не загружая сервер.

    from common.buildinfo import buildinfo as bi
    BUILD = bi.at_start()                         # при старте сервера
    bi.info()                                     # {"build", "disk_build", "stale"} - в /api/info
    bi.restart_later()                            # POST /api/restart: тот же процесс заново (os.execv), порт освобождается

    python -m common.buildinfo hash               # код на диске
    python -m common.buildinfo check http://127.0.0.1:8070/api/info     # fresh | stale | old | down | foreign
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

SUFFIXES = {'.py', '.js', '.mjs', '.css', '.html', '.json', '.yaml', '.yml', '.toml'}
SKIP_DIRS = {'__pycache__', 'tests', 'node_modules', '.venv', '.git', '.run'}
ENV_AT_START = 'BUILDINFO_AT_START'      # проверки: сервер «старой сборки» без правки файлов
_at_start: str | None = None


def project_root() -> Path:
    """Корень проекта: копия модуля лежит в <проект>/common/buildinfo/."""
    return Path(__file__).resolve().parents[2]


def parts(root: Path | None = None) -> list[str]:
    root = Path(root or project_root())
    try:
        return list(json.loads((root / 'buildinfo.json').read_text(encoding='utf-8'))['parts'])
    except (OSError, ValueError, KeyError):
        return ['app', 'web', 'static', 'common']


def code_hash(root: Path | None = None, parts_: list[str] | None = None) -> str:
    """12 знаков SHA-1 по путям и содержимому файлов кода и окна (без __pycache__, тестов, окружения)."""
    root = Path(root or project_root())
    h = hashlib.sha1()
    for part in parts_ or parts(root):
        base = root / part
        files = [base] if base.is_file() else sorted(base.rglob('*')) if base.is_dir() else []
        for f in files:
            if not f.is_file() or f.suffix not in SUFFIXES or SKIP_DIRS & set(f.relative_to(root).parts):
                continue
            h.update(f.relative_to(root).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


def at_start(root: Path | None = None) -> str:
    """Сборка, с которой стартовал сервер (считается один раз)."""
    global _at_start
    if _at_start is None:
        _at_start = os.environ.get(ENV_AT_START) or code_hash(root)
    return _at_start


def info(root: Path | None = None) -> dict:
    build, disk = at_start(root), code_hash(root)
    return {'build': build, 'disk_build': disk, 'stale': build != disk}


def restart_later(delay: float = 0.4) -> None:
    """Перезапуск сервера тем же способом, каким его запустили (sys.orig_argv), после ответа на запрос. Дескрипторы
    (сокет порта) не переходят в новый процесс - порт освобождается; PID тот же, pid-файлы запускателей верны."""
    def go():
        time.sleep(delay)
        os.environ.pop(ENV_AT_START, None)
        sys.stdout.flush()
        sys.stderr.flush()
        argv = list(getattr(sys, 'orig_argv', None) or [sys.executable, *sys.argv])
        # сокет порта не должен перейти в новый процесс: werkzeug (Flask) делает его наследуемым - порт был бы занят
        for fd in range(3, 4096):
            try:
                os.set_inheritable(fd, False)
            except OSError:
                pass
        os.execv(sys.executable, [sys.executable, *argv[1:]])
    threading.Thread(target=go, daemon=True).start()


def check(url: str, root: Path | None = None, name: str | None = None) -> str:
    """fresh - сервер на коде с диска; stale - код поменялся после старта; old - сервер без кода сборки;
    down - не отвечает; foreign - на порту другая программа (name - ожидаемое поле «name», если есть)."""
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            d = json.loads(r.read().decode('utf-8'))
    except Exception as e:   # noqa: BLE001 - любой сбой = не отвечает
        return 'foreign' if getattr(e, 'code', None) else 'down'
    if not isinstance(d, dict) or (name and d.get('name') not in (None, name)):
        return 'foreign'
    if 'build' not in d:
        return 'old'
    return 'fresh' if d['build'] == code_hash(root) else 'stale'


def main(argv: list[str]) -> int:
    root = None
    if '--root' in argv:
        i = argv.index('--root')
        root = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if argv[:1] == ['check'] and len(argv) >= 2:
        print(check(argv[1], root, argv[2] if len(argv) > 2 else None))
        return 0
    print(code_hash(root))
    return 0
