import sqlite3
import uuid
from pathlib import Path


def get_conn(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path):
    with get_conn(db_path) as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                id   TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS mappings (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id     TEXT NOT NULL,
                token          TEXT NOT NULL,
                canonical_form TEXT NOT NULL,
                entity_type    TEXT NOT NULL,
                created_at     TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(session_id, canonical_form, entity_type)
            );
        ''')


def create_session(db_path, name: str) -> str:
    sid = str(uuid.uuid4())
    with get_conn(db_path) as conn:
        conn.execute('INSERT INTO sessions (id, name) VALUES (?,?)', (sid, name))
    return sid


def get_all_sessions(db_path):
    with get_conn(db_path) as conn:
        rows = conn.execute('''
            SELECT s.id, s.name, s.created_at,
                   COUNT(m.id) as mapping_count
            FROM sessions s
            LEFT JOIN mappings m ON m.session_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
        ''').fetchall()
    return [dict(r) for r in rows]


def delete_session(db_path, session_id: str):
    with get_conn(db_path) as conn:
        conn.execute('DELETE FROM mappings WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM sessions  WHERE id=?',         (session_id,))


def get_session_mappings(db_path, session_id: str):
    with get_conn(db_path) as conn:
        rows = conn.execute('''
            SELECT token, canonical_form, entity_type, created_at
            FROM mappings
            WHERE session_id=?
            ORDER BY entity_type, token
        ''', (session_id,)).fetchall()
    return [dict(r) for r in rows]


# Token prefix map  (entity_type → ASCII prefix so tokens are PDF-safe)
_PREFIX = {
    'ФИО':      'FIO',
    'ЮЛ':       'YUL',
    'ИНН':      'INN',
    'ОГРН':     'OGRN',
    'КПП':      'KPP',
    'РС':       'RS',
    'КС':       'KS',
    'БИК':      'BIK',
    'СНИЛС':    'SNILS',
    'ПАСПОРТ':  'PASSPORT',
    'ТЕЛЕФОН':  'TEL',
    'EMAIL':    'EMAIL',
    'SWIFT':    'SWIFT',
    'АДРЕС':    'ADR',
    'ДАТАРОЖД': 'DOB',
    'ЛИЦЕНЗИЯ': 'LIC',
    'URL':      'URL',
}


def get_or_create_token(db_path, session_id: str,
                        canonical_form: str, entity_type: str) -> str:
    """Return existing token or create a new one for this entity."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            'SELECT token FROM mappings '
            'WHERE session_id=? AND canonical_form=? AND entity_type=?',
            (session_id, canonical_form, entity_type)
        ).fetchone()
        if row:
            return row['token']

        count = conn.execute(
            'SELECT COUNT(*) FROM mappings WHERE session_id=? AND entity_type=?',
            (session_id, entity_type)
        ).fetchone()[0]

        prefix = _PREFIX.get(entity_type, entity_type.upper())
        token  = f'{prefix}_{count + 1}'

        conn.execute(
            'INSERT OR IGNORE INTO mappings '
            '(session_id, token, canonical_form, entity_type) VALUES (?,?,?,?)',
            (session_id, token, canonical_form, entity_type)
        )
    return token


def delete_mapping(db_path, session_id: str, token: str):
    """Remove a single mapping by token from a session."""
    with get_conn(db_path) as conn:
        conn.execute(
            'DELETE FROM mappings WHERE session_id=? AND token=?',
            (session_id, token)
        )


def update_mapping(db_path, session_id: str, token: str, data: dict):
    """
    Update canonical_form and/or entity_type for an existing mapping.
    data may contain 'canonical_form' and/or 'entity_type'.
    """
    with get_conn(db_path) as conn:
        if data.get('canonical_form'):
            conn.execute(
                'UPDATE mappings SET canonical_form=? WHERE session_id=? AND token=?',
                (data['canonical_form'], session_id, token)
            )
        if data.get('entity_type'):
            conn.execute(
                'UPDATE mappings SET entity_type=? WHERE session_id=? AND token=?',
                (data['entity_type'], session_id, token)
            )


def get_reverse_mappings(db_path, session_id: str) -> dict:
    """Return {token: canonical_form} for deanonymization."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            'SELECT token, canonical_form FROM mappings WHERE session_id=?',
            (session_id,)
        ).fetchall()
    return {r['token']: r['canonical_form'] for r in rows}
