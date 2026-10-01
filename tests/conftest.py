"""Pytest fixtures shared across the anonymizer test suite."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# tests never write into the owner's journal
os.environ.setdefault('ANONYMIZER_LOG_DIR', tempfile.mkdtemp(prefix='anon_logs_'))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_db(tmp_path):
    """A fresh SQLite DB in an isolated temp dir."""
    from core.db import init_db
    db_path = tmp_path / 'anon.db'
    init_db(db_path)
    return db_path


@pytest.fixture
def session_id(tmp_db):
    """A session row pre-created in tmp_db. Returns its id."""
    from core.db import create_session
    return create_session(tmp_db, 'test-session')


# Owner's real documents — local only, git-ignored. Override with ANONYMIZER_SAMPLES.
SAMPLES_DIR = Path(os.environ.get('ANONYMIZER_SAMPLES', ROOT / 'Проверка распознавания текста'))


def sample_files(exts=None):
    if not SAMPLES_DIR.exists():
        return []
    return sorted(f for f in SAMPLES_DIR.iterdir()
                  if f.is_file() and not f.name.startswith('.')
                  and (exts is None or f.suffix.lower() in exts))


@pytest.fixture(scope='session')
def test_files_dir():
    if not SAMPLES_DIR.exists():
        pytest.skip(f'Local samples folder missing: {SAMPLES_DIR}')
    return SAMPLES_DIR
