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
                id         TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS mappings (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id     TEXT NOT NULL,
                token          TEXT NOT NULL,
                original_form  TEXT NOT NULL DEFAULT '',
                canonical_form TEXT NOT NULL,
                entity_type    TEXT NOT NULL,
                created_at     TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(session_id, original_form, entity_type)
            );

            CREATE TABLE IF NOT EXISTS user_patterns (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern     TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS exclusions (
                session_id    TEXT NOT NULL,
                original_form TEXT NOT NULL,
                entity_type   TEXT NOT NULL,
                PRIMARY KEY (session_id, original_form, entity_type)
            );
        ''')
        # Migration: add original_form column to existing databases
        cols = {r[1] for r in conn.execute('PRAGMA table_info(mappings)')}
        if 'original_form' not in cols:
            conn.execute("ALTER TABLE mappings ADD COLUMN original_form TEXT NOT NULL DEFAULT ''")
            conn.execute('UPDATE mappings SET original_form = canonical_form WHERE original_form = ""')
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_map_orig ON mappings(session_id, original_form, entity_type)')
        # Enforce token uniqueness within session.
        # Existing databases may contain duplicates from the old COUNT(*)+1 bug —
        # rename the duplicates to free token numbers before adding the index.
        _dedupe_duplicate_tokens(conn)
        try:
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_map_token ON mappings(session_id, token)')
        except Exception as e:
            print(f'[DB] Cannot create idx_map_token: {e}')


def _dedupe_duplicate_tokens(conn):
    """Find any (session_id, token) duplicates and renumber the extras
    to the next free slot for their entity_type."""
    dupes = conn.execute('''
        SELECT session_id, token, COUNT(*) c
        FROM mappings
        GROUP BY session_id, token
        HAVING c > 1
    ''').fetchall()
    if not dupes:
        return
    for d in dupes:
        sid, bad_tok = d['session_id'], d['token']
        # Keep the row with the smallest id, renumber the rest
        rows = conn.execute(
            'SELECT id, entity_type FROM mappings WHERE session_id=? AND token=? ORDER BY id',
            (sid, bad_tok)
        ).fetchall()
        for extra in rows[1:]:
            etype = extra['entity_type']
            prefix = _PREFIX.get(etype, etype.upper())
            # Compute next free number for this prefix in this session
            existing = conn.execute(
                "SELECT token FROM mappings WHERE session_id=? AND token LIKE ?",
                (sid, f'{prefix}_%')
            ).fetchall()
            max_n = 0
            for r in existing:
                try:
                    n = int(r['token'].rsplit('_', 1)[1])
                    if n > max_n:
                        max_n = n
                except (ValueError, IndexError):
                    pass
            new_tok = f'{prefix}_{max_n + 1}'
            conn.execute('UPDATE mappings SET token=? WHERE id=?', (new_tok, extra['id']))
            print(f'[DB] Renamed duplicate {bad_tok} -> {new_tok} (session {sid[:8]})')


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
        conn.execute('DELETE FROM mappings    WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM exclusions  WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM sessions    WHERE id=?',         (session_id,))


def get_session_mappings(db_path, session_id: str):
    with get_conn(db_path) as conn:
        rows = conn.execute('''
            SELECT token, original_form, canonical_form, entity_type, created_at
            FROM mappings
            WHERE session_id=?
            ORDER BY entity_type, token
        ''', (session_id,)).fetchall()
    return [dict(r) for r in rows]


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
    'АДРЕС_ФИЗ': 'ADR',
    'АДРЕС_ЮР': 'ADR',
    'ДАТАРОЖД': 'DOB',
    'ЛИЦЕНЗИЯ': 'LIC',
    'URL':      'URL',
    'FIO':      'FIO',
    'YUL':      'YUL',
    'ADDR_PHYS': 'ADR',
    'ADDR_CORP': 'ADR',
    'PASSPORT':  'PASSPORT',
    'DOB':       'DOB',
}


def _next_token_number(conn, session_id: str, prefix: str) -> int:
    """Return next free numeric suffix for tokens with given prefix.
    Uses MAX(suffix)+1 so deletions never cause collisions."""
    rows = conn.execute(
        "SELECT token FROM mappings WHERE session_id=? AND token LIKE ?",
        (session_id, f'{prefix}_%')
    ).fetchall()
    max_n = 0
    for r in rows:
        try:
            n = int(r['token'].rsplit('_', 1)[1])
            if n > max_n:
                max_n = n
        except (ValueError, IndexError):
            pass
    return max_n + 1


def get_or_create_token(db_path, session_id: str,
                        original_form: str, canonical_form: str,
                        entity_type: str) -> str:
    with get_conn(db_path) as conn:
        row = conn.execute(
            'SELECT token FROM mappings '
            'WHERE session_id=? AND original_form=? AND entity_type=?',
            (session_id, original_form, entity_type)
        ).fetchone()
        if row:
            return row['token']

        prefix = _PREFIX.get(entity_type, entity_type.upper())
        n = _next_token_number(conn, session_id, prefix)
        token = f'{prefix}_{n}'

        conn.execute(
            'INSERT OR IGNORE INTO mappings '
            '(session_id, token, original_form, canonical_form, entity_type) '
            'VALUES (?,?,?,?,?)',
            (session_id, token, original_form, canonical_form, entity_type)
        )
    return token


def delete_mapping(db_path, session_id: str, token: str):
    with get_conn(db_path) as conn:
        conn.execute(
            'DELETE FROM mappings WHERE session_id=? AND token=?',
            (session_id, token)
        )


def update_mapping(db_path, session_id: str, token: str, data: dict):
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


def update_mapping_original(db_path, session_id: str, token: str, new_original: str):
    with get_conn(db_path) as conn:
        conn.execute(
            'UPDATE mappings SET original_form=? WHERE session_id=? AND token=?',
            (new_original, session_id, token)
        )


def get_reverse_mappings(db_path, session_id: str) -> dict:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            'SELECT token, original_form FROM mappings WHERE session_id=?',
            (session_id,)
        ).fetchall()
    return {r['token']: r['original_form'] for r in rows}


def get_top_patterns(db_path, entity_type=None, limit=20) -> list:
    with get_conn(db_path) as conn:
        if entity_type:
            rows = conn.execute(
                'SELECT id, pattern, entity_type, created_at '
                'FROM user_patterns WHERE entity_type=? '
                'ORDER BY created_at DESC LIMIT ?',
                (entity_type, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT id, pattern, entity_type, created_at '
                'FROM user_patterns '
                'ORDER BY created_at DESC LIMIT ?',
                (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def save_user_pattern(db_path, pattern: str, entity_type: str):
    if not pattern.strip():
        return
    with get_conn(db_path) as conn:
        existing = conn.execute(
            'SELECT id FROM user_patterns WHERE pattern=? AND entity_type=?',
            (pattern, entity_type)
        ).fetchone()
        if not existing:
            conn.execute(
                'INSERT INTO user_patterns (pattern, entity_type) VALUES (?,?)',
                (pattern, entity_type)
            )


def delete_user_pattern(db_path, pattern_id: int):
    with get_conn(db_path) as conn:
        conn.execute('DELETE FROM user_patterns WHERE id=?', (pattern_id,))


def add_exclusion(db_path, session_id: str, original_form: str, entity_type: str):
    """Mark original_form+entity_type as excluded — skip on reprocess."""
    with get_conn(db_path) as conn:
        conn.execute(
            'INSERT OR IGNORE INTO exclusions (session_id, original_form, entity_type) '
            'VALUES (?,?,?)',
            (session_id, original_form, entity_type)
        )


def get_exclusions(db_path, session_id: str) -> set:
    """Return set of (original_form, entity_type) to skip during pipeline."""
    with get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                'SELECT original_form, entity_type FROM exclusions WHERE session_id=?',
                (session_id,)
            ).fetchall()
            return {(r['original_form'], r['entity_type']) for r in rows}
        except Exception:
            return set()


def delete_session_exclusions(db_path, session_id: str):
    with get_conn(db_path) as conn:
        conn.execute('DELETE FROM exclusions WHERE session_id=?', (session_id,))
