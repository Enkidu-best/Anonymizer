"""GPU queue (shared module common/lock, «_Общие модули 01»): the window «Видеокарта занята» before an LLM job,
«Запустить сейчас», «На процессоре», the background review waits in the queue. Own temp lock (conftest), no model."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from common.lock import heavy_lock as hl
from core import heavy, llm

HOLDER = Path(__file__).resolve().parent.parent / 'common' / 'lock' / 'tests' / '_holder.py'


@pytest.fixture
def foreign():
    p = subprocess.Popen([sys.executable, str(HOLDER), str(heavy.path()), 'Herald', 'human', 'hold', 'озвучка книги'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == 'taken'
    yield p
    if p.poll() is None:
        p.stdin.write('release\n')
        p.stdin.flush()
        p.wait(10)
    p.stdin.close()
    p.stdout.close()


@pytest.fixture
def ollama(monkeypatch):
    """Fake Ollama: records request bodies."""
    sent = []

    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"response": "{}"}'

        def close(self):
            pass

    def fake(req, timeout=0):
        sent.append(json.loads(req.data))
        return R()
    monkeypatch.setattr(llm.urllib.request, 'urlopen', fake)
    return sent


def test_lock_path_is_temp():
    assert str(heavy.path()) == os.environ['ANONYMIZER_HEAVY_LOCK']


def test_call_takes_lock_and_reports_busy(foreign, ollama, monkeypatch):
    monkeypatch.setattr(heavy, 'WAIT', 0.3)
    with pytest.raises(heavy.Busy, match='Herald'):
        llm._ollama_generate('p', 's')
    assert not ollama                                     # nothing reached Ollama while the GPU is busy


def test_cpu_and_now_choices(foreign, ollama):
    with heavy.job('cpu', 'проверка'):
        llm._ollama_generate('p', 's')
    assert ollama[0]['options']['num_gpu'] == 0 and ollama[0]['keep_alive'] == 0
    assert ollama[-1] == {'model': llm._llm_model or llm.DEFAULT_MODEL, 'keep_alive': 0}   # unloaded after the job
    with heavy.job('now', 'проверка'):
        assert [w.program for w in hl.alongside(heavy.path())] == ['Anonymizer']
        llm._ollama_generate('p', 's')
    assert 'num_gpu' not in ollama[-1]['options'] and hl.status(heavy.path()).program == 'Herald'


def test_preload_skipped_when_busy(foreign, ollama, monkeypatch):
    monkeypatch.setattr(llm, 'check_ollama', lambda: None)
    monkeypatch.setattr(llm, '_llm_available', True)
    monkeypatch.setattr(llm, '_llm_model', 'qwen3.5:9b')
    llm.preload_model_async()
    time.sleep(0.5)
    assert not ollama                                     # background warm-up does not touch a busy GPU


def test_status_and_review_waits_in_queue(foreign, monkeypatch, tmp_path):
    import app as appmod
    c = appmod.app.test_client()
    st = c.get('/api/heavy-lock?kind=llm_only').get_json()
    assert st['holder']['program'] == 'Herald' and not st['ours']
    assert st['cpu'] == {'seconds': heavy.JOB_SEC['llm_only'] * heavy.CPU_FACTOR, 'factor': heavy.CPU_FACTOR}
    assert c.get('/common/lock/web/heavy-lock.js').status_code == 200
    src, out = tmp_path / 'a.txt', tmp_path / 'b.txt'
    src.write_text('Иванов', encoding='utf-8')
    out.write_text('[FIO_1]', encoding='utf-8')
    monkeypatch.setattr('core.llm_review.review', lambda *a, **k: {'add': [], 'remove': [], 'merge': []})
    jid = appmod._start_review('s1', src, out, 'wait')
    for _ in range(40):
        if appmod.REVIEWS[jid]['status'] == 'wait_lock':
            break
        time.sleep(0.1)
    assert appmod.REVIEWS[jid]['status'] == 'wait_lock' and appmod.REVIEWS[jid]['holder'] == 'Herald'
    foreign.stdin.write('release\n')
    foreign.stdin.flush()
    foreign.wait(10)
    for _ in range(60):
        if appmod.REVIEWS[jid]['status'] == 'done':
            break
        time.sleep(0.1)
    assert appmod.REVIEWS[jid]['status'] == 'done'
