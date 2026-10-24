"""Общий блок buildinfo: хеш кода, состояния сервера, перезапуск ярлыком и окно (полная проверка на сервере-образце).

    <python с playwright> «_общие модули/buildinfo/tests/test_buildinfo.py»
"""
import json
import shutil
import socket
import sys
import tempfile
from pathlib import Path

MOD = Path(__file__).resolve().parents[1]


def _project() -> Path:
    root = Path(tempfile.mkdtemp(prefix='bi_proj_'))
    shutil.copytree(MOD, root / 'common' / 'buildinfo', ignore=shutil.ignore_patterns('__pycache__', 'tests'))
    (root / 'common' / '__init__.py').write_text('', encoding='utf-8')
    (root / 'app').mkdir()
    (root / 'app' / 'main.py').write_text('x = 1\n', encoding='utf-8')
    (root / 'buildinfo.json').write_text(json.dumps({'parts': ['app', 'common']}), encoding='utf-8')
    shutil.copy(MOD / 'tests' / 'sample_server.py', root / 'sample_server.py')
    return root


def _port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_hash_follows_content():
    sys.path.insert(0, str(MOD.parent))
    root = _project()
    sys.path.insert(0, str(root))
    from common.buildinfo import buildinfo as bi   # копия во временном проекте
    a = bi.code_hash(root)
    (root / 'app' / 'main.py').write_text('x = 2\n', encoding='utf-8')
    b = bi.code_hash(root)
    (root / 'app' / '__pycache__').mkdir()
    (root / 'app' / '__pycache__' / 'm.py').write_text('junk', encoding='utf-8')
    (root / 'app' / 'notes.txt').write_text('не код', encoding='utf-8')
    assert a != b and bi.code_hash(root) == b and len(b) == 12
    assert bi.parts(root) == ['app', 'common']
    assert bi.check('http://127.0.0.1:9/api/info', root) == 'down'


def test_restart_by_launcher_and_window():
    root = _project()
    port = _port()
    sys.path.insert(0, str(root))
    from common.buildinfo.testing import check_restart
    res = check_restart([sys.executable, 'sample_server.py', str(port)], port, root, ready_selector='h1')
    bad = [k for k, v in res.items() if not v]
    assert not bad, bad


if __name__ == '__main__':
    test_hash_follows_content()
    test_restart_by_launcher_and_window()
    print('buildinfo: все проверки прошли')
