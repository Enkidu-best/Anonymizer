"""Journal of what the app does — WITHOUT personal data.

Only tokens, types, counts, sizes and durations are written; never values, file
names or text fragments (those often contain a company or person name). The
end-to-end test greps the log for every value of the session to prove it.

    logs/anonymizer.log          events, rotated 5 × 5 MB
    logs/jobs/<id>.json          one summary per processing job (with heartbeat)
    logs/anonymizer-verbose.log  only when the «verbose» switch is on (contains data,
                                 off by default, switched off again at every start)

Folder: <project>/logs when run from source, ~/Library/Logs/Anonymizer (macOS) or
%LOCALAPPDATA%\\Anonymizer\\Logs when packaged; ANONYMIZER_LOG_DIR overrides.
"""
import contextvars
import json
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import uuid
from pathlib import Path

_log = logging.getLogger('anonymizer')
_verbose_log = logging.getLogger('anonymizer.verbose')
_verbose_log.propagate = False
LOG_DIR: Path = None
VERBOSE = False
HEARTBEAT_S = 10
STALE_S = 120
_current = contextvars.ContextVar('anonymizer_job', default=None)


def default_dir(frozen: bool, project_dir: Path) -> Path:
    if os.environ.get('ANONYMIZER_LOG_DIR'):
        return Path(os.environ['ANONYMIZER_LOG_DIR'])
    if not frozen:
        return project_dir / 'logs'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Logs' / 'Anonymizer'
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Anonymizer' / 'Logs'
    return Path.home() / '.local' / 'state' / 'Anonymizer' / 'logs'


def setup(log_dir: Path):
    global LOG_DIR
    LOG_DIR = Path(log_dir)
    (LOG_DIR / 'jobs').mkdir(parents=True, exist_ok=True)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in _log.handlers):
        h = logging.handlers.RotatingFileHandler(LOG_DIR / 'anonymizer.log', maxBytes=5 * 1024 * 1024,
                                                 backupCount=5, encoding='utf-8')
        h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        _log.addHandler(h)
        _log.setLevel(logging.INFO)
    _mark_interrupted()


def set_verbose(on: bool):
    """Detailed journal with data — for analysing a hard case, off by default."""
    global VERBOSE
    VERBOSE = bool(on)
    if on and LOG_DIR and not _verbose_log.handlers:
        h = logging.handlers.RotatingFileHandler(LOG_DIR / 'anonymizer-verbose.log',
                                                 maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8')
        h.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        _verbose_log.addHandler(h)
        _verbose_log.setLevel(logging.INFO)
    event('verbose_log', on=VERBOSE)


def _fmt(fields: dict) -> str:
    return ' '.join(f'{k}={json.dumps(v, ensure_ascii=False, default=str)}' for k, v in fields.items())


def event(name: str, **fields):
    """One line in the journal. Callers pass only non-personal fields."""
    job = _current.get()
    if job is not None:
        fields = {'job': job.id, **fields}
    _log.info(f'{name} {_fmt(fields)}')


def error(name: str, exc: BaseException = None, **fields):
    job = _current.get()
    if job is not None:
        fields = {'job': job.id, **fields}
        job.errors.append(f'{name}: {type(exc).__name__ if exc else ""}')
    _log.error(f'{name} {_fmt(fields)}', exc_info=exc)


def verbose(msg: str):
    if VERBOSE:
        _verbose_log.info(msg)


def startup_info(version: str, ollama: dict = None):
    try:
        mem = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 2 ** 30
    except (ValueError, OSError, AttributeError):
        mem = None
    event('startup', version=version, python=platform.python_version(),
          os=f'{platform.system()} {platform.mac_ver()[0] or platform.release()}',
          machine=platform.machine(), memory_gb=round(mem, 1) if mem else None,
          frozen=bool(getattr(sys, 'frozen', False)), **(ollama or {}))


class Job:
    """A processing job: stages with durations and counts, heartbeat, JSON summary."""

    def __init__(self, kind: str, **info):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.info = info
        self.stages = []
        self.counts = {}
        self.errors = []
        self.status = 'running'
        self.started = time.time()
        self.updated = self.started
        self._stop = threading.Event()
        self._token = None

    # context manager: sets the current job for event() and stage()
    def __enter__(self):
        self._token = _current.set(self)
        event('job_start', kind=self.kind, **self.info)
        self._write()
        threading.Thread(target=self._beat, daemon=True).start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._stop.set()
        if exc is not None:
            self.status = 'error'
            error('job_error', exc)
        elif self.status == 'running':
            self.status = 'done'
        event('job_end', status=self.status, seconds=round(time.time() - self.started, 2),
              counts=self.counts, errors=len(self.errors))
        self._write()
        _current.reset(self._token)
        return False

    def stage(self, name: str):
        return _Stage(self, name)

    def add(self, **counts):
        for k, v in counts.items():
            self.counts[k] = self.counts.get(k, 0) + v

    def _beat(self):
        while not self._stop.wait(HEARTBEAT_S):
            self.updated = time.time()
            self._write()

    def summary(self) -> dict:
        return {'id': self.id, 'kind': self.kind, 'status': self.status, 'info': self.info,
                'started': self.started, 'updated': time.time(),
                'seconds': round(time.time() - self.started, 2),
                'stages': self.stages, 'counts': self.counts, 'errors': self.errors}

    def _write(self):
        if LOG_DIR is None:
            return
        try:
            (LOG_DIR / 'jobs' / f'{self.id}.json').write_text(
                json.dumps(self.summary(), ensure_ascii=False, indent=1), encoding='utf-8')
        except OSError:
            pass


class _Stage:
    def __init__(self, job, name):
        self.job, self.name = job, name

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        sec = round(time.time() - self.t0, 3)
        self.job.stages.append({'stage': self.name, 'seconds': sec, 'ok': exc is None})
        event('stage', stage=self.name, seconds=sec, ok=exc is None)
        return False


def current_job():
    return _current.get()


def stage(name: str):
    """Stage of the current job, or a no-op outside a job."""
    job = _current.get()
    return job.stage(name) if job else _Noop()


class _Noop:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mark_interrupted():
    """Jobs left «running» by a previous run (crash, quit) are marked «прервана»."""
    for f in (LOG_DIR / 'jobs').glob('*.json'):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
            if d.get('status') == 'running':
                d['status'] = 'interrupted'
                f.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
                event('job_interrupted', id=d.get('id'))
        except (OSError, ValueError):
            continue


def stale_jobs() -> list:
    """Running jobs without a heartbeat for more than STALE_S seconds (shown as «зависла»)."""
    out = []
    if LOG_DIR is None:
        return out
    now = time.time()
    for f in (LOG_DIR / 'jobs').glob('*.json'):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if d.get('status') == 'running' and now - d.get('updated', now) > STALE_S:
            out.append(d.get('id'))
    return out
