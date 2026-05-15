"""Pytest fixtures shared across the anonymizer test suite."""
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
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


@pytest.fixture(scope='session')
def test_files_dir():
    """Folder with realistic Russian DOCX/PDF samples bundled with the repo."""
    p = ROOT / 'Тестовые_файлы'
    if not p.exists():
        pytest.skip(f'Test files directory missing: {p}')
    return p
