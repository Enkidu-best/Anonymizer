"""GPU queue `.heavy-lock` - the shared module `common/lock` («Разработка ПО/_общие модули/lock», description - README.md
there; do not edit the copy in common/). Binding for Anonymizer (standard 2.4, 2.5, 6.7):

  * a job with the LLM started by the user («Точно», «Только нейросеть») - a person: the GPU is taken by another program -
    the window «Видеокарта занята» in the app (the choice comes in the request: wait - queue, now - «Запустить сейчас»,
    cpu - «На процессоре», Ollama without the GPU) - job(); the lock is held for the whole job;
  * a single request outside a job (chat command, cases on restore) - call(): waits ANONYMIZER_LOCK_WAIT seconds, then Busy;
  * model warm-up at start - background: the GPU is busy - skipped;
  * ANONYMIZER_HEAVY_LOCK_HELD=1 (or HEAVY_LOCK_HELD=1) - the lock is held by whoever started us (night chain, run).
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

from common.lock import heavy_lock as hl

OWNER = 'Anonymizer'
WAIT = float(os.environ.get('ANONYMIZER_LOCK_WAIT', '60'))
# «На процессоре» (add. 21:40): how many times slower Ollama is on the CPU than on the GPU of this Mac; the forecast in
# the window = usual job time × factor. Measured 24.10 («_общие модули/lock/замеры/ollama-2026-10-24-0941.json»): qwen3.5:9b, a 1,800-token request - 3.4,
# reading the request - 4.2
CPU_FACTOR = float(os.environ.get('ANONYMIZER_CPU_FACTOR', '4'))
JOB_SEC = {'accurate': 60.0, 'llm_only': 120.0}     # usual job time on the GPU - for the forecast
_local = threading.local()          # this thread: held - inside job(), cpu - «На процессоре»


class Busy(RuntimeError):
    pass


def path() -> Path:
    return Path(os.environ.get('ANONYMIZER_HEAVY_LOCK') or hl.default_path())


def new(what: str, who: str | None = None) -> hl.HeavyLock:
    return hl.HeavyLock(OWNER, what, who=who, path=path(), held_env='ANONYMIZER_HEAVY_LOCK_HELD', idle_after=None)


def on_cpu() -> bool:
    return bool(getattr(_local, 'cpu', False))


def _inside() -> bool:
    return bool(getattr(_local, 'held', 0)) or on_cpu()


def holder_name() -> str:
    h = hl.status(path())
    return (h.program or '?') if h and h.program != OWNER else ''


@contextlib.contextmanager
def job(choice: str | None, what: str, wait: float | None = None, cancel: threading.Event | None = None,
        on_wait=None):
    """The whole job under the lock with the choice of the window «Видеокарта занята». on_wait(holder name) - while
    waiting in the queue. Busy - the wait time ran out; cancel set - Busy too."""
    choice = choice if choice in ('wait', 'now', 'cpu') else 'wait'
    if choice == 'cpu':
        _local.cpu = True
        try:
            yield
        finally:
            _local.cpu = False
            from core import llm
            llm.unload()
        return
    lk = new(what)
    if choice == 'now':
        if not lk.try_acquire():
            lk.run_now()
    elif not lk.acquire(wait=WAIT if wait is None else wait, cancel=cancel, poll=0.5,
                        on_wait=(lambda h, _ahead: on_wait(h.program if h else '')) if on_wait else None):
        lk.release()
        raise Busy(f'Видеокарта занята программой «{holder_name() or "?"}» - повторите позже')
    _local.held = getattr(_local, 'held', 0) + 1
    try:
        yield
    finally:
        _local.held -= 1
        lk.release()


@contextlib.contextmanager
def call(what: str = 'запрос к нейросети'):
    """One model request: inside job() or on the CPU - nothing to take; otherwise take the lock for this request."""
    if _inside():
        yield
        return
    with job('wait', what):
        yield


def try_background(what: str) -> hl.HeavyLock | None:
    """Background (model warm-up): free - the lock taken (release it after), busy - None."""
    lk = new(what, who='background')
    return lk if lk.try_acquire() else None


def status_json(kind: str = 'accurate') -> dict:
    d = hl.status_json(path())
    d['ours'] = bool(d['holder'] and d['holder']['program'] == OWNER)
    d['cpu'] = {'seconds': JOB_SEC.get(kind, 60.0) * CPU_FACTOR, 'factor': CPU_FACTOR}
    return d
