"""Общая проверка блока buildinfo для каждой программы (стандарт 6.5, «_Порядок 03», п. 1):
«сервер старой сборки → ярлык перезапускает, окно видит новый build».

    from common.buildinfo.testing import check_restart
    check_restart(cmd=[python, "-m", "uvicorn", "app.api:app", "--port", "8044"], port=8044, root=ROOT, env={...})

1. Сервер программы стартует на тестовом порту со своими данными и `BUILDINFO_AT_START=old…` - «старая сборка».
2. `bi_state` - stale; окно (Playwright, безголовый Chromium) показывает «Программа обновилась».
3. `bi_ensure_fresh` из `launch.sh` (то же, что делает ярлык) перезапускает сервер: `build` = коду на диске, тот же PID.
4. Окно после «Перезапустить» - без полосы, видит новый `build`.
Возвращает словарь проверок {название: да / нет}; сервер останавливается в конце.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _info(url):
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.loads(r.read())


def _wait(url, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            return _info(url)
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f'сервер не ответил: {url}')


def check_restart(cmd: list[str], port: int, root: Path, env: dict | None = None, page_path: str = '/',
                  info_path: str = '/api/info', python: str | None = None, ready_selector: str = 'body',
                  browser=None) -> dict:
    """browser - уже запущенный браузер Playwright (внутри тестов, где sync_playwright уже открыт)."""
    root = Path(root)
    url = f'http://127.0.0.1:{port}'
    log = Path(tempfile.mkdtemp(prefix='bi_check_')) / 'launcher.log'
    e = dict(os.environ, **(env or {}), BUILDINFO_AT_START='0ld0ld0ld0ld', HEAVY_LOCK_WHO='background')
    out = open(log.with_name('server.log'), 'w')
    proc = subprocess.Popen(cmd, cwd=str(root), env=e, stdout=out, stderr=subprocess.STDOUT)
    res = {}
    try:
        d = _wait(url + info_path)
        disk = subprocess.run([python or sys.executable, '-m', 'common.buildinfo', 'hash'], cwd=str(root),
                              capture_output=True, text=True).stdout.strip()
        res['сервер старой сборки: build ≠ disk_build'] = d.get('build') == '0ld0ld0ld0ld' and d.get('disk_build') == disk
        import contextlib
        from playwright.sync_api import sync_playwright
        with (contextlib.nullcontext(None) if browser else sync_playwright()) as p:
            br = browser or p.chromium.launch()
            pg = br.new_page(locale='ru-RU')
            pg.goto(url + page_path)
            pg.wait_for_selector(ready_selector)
            pg.evaluate("window.BuildInfo && BuildInfo.check()")
            try:
                pg.wait_for_function("window.BuildInfo && BuildInfo.state.stale === true", timeout=15000)
                stale_seen = True
            except Exception:
                stale_seen = False
            res['окно: «Программа обновилась»'] = stale_seen
            sh = (f'. "{HERE / "launch.sh"}"; bi_ensure_fresh "{url + info_path}" "{python or sys.executable}" '
                  f'"{root}" "{log}"')
            rc = subprocess.run(['bash', '-c', sh], timeout=120).returncode
            n = _wait(url + info_path)
            res['ярлык (bi_ensure_fresh) перезапустил сервер на код с диска'] = rc == 0 and n.get('build') == disk
            res['тот же процесс (PID), порт освобождён и занят им же'] = proc.poll() is None
            res['в журнале ярлыка - строка о перезапуске'] = 'перезапускаю сервер' in log.read_text(encoding='utf-8')
            pg.reload()
            pg.wait_for_selector(ready_selector)
            pg.evaluate("window.BuildInfo && BuildInfo.check()")
            pg.wait_for_timeout(1500)
            res['окно после перезапуска: новый build, полосы нет'] = pg.evaluate(
                "window.BuildInfo && BuildInfo.state.stale === false && BuildInfo.state.last.build") == disk
            pg.close()
            if not browser:
                br.close()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        out.close()
    return res
