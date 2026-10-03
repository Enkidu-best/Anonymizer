import json
import sqlite3
import uuid
from pathlib import Path


class _Conn(sqlite3.Connection):
    """`with get_conn(db) as conn:` commits/rolls back AND closes (no leaked handles)."""

    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


SCHEMA_VERSION = 4


def get_conn(db_path):
    conn = sqlite3.connect(str(db_path), factory=_Conn)
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

            CREATE TABLE IF NOT EXISTS known_entities (
                value         TEXT NOT NULL,
                entity_type   TEXT NOT NULL,
                seen_count    INTEGER NOT NULL DEFAULT 1,
                last_seen_at  TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (value, entity_type)
            );
        ''')
        # Migration: add original_form column to existing databases
        cols = {r[1] for r in conn.execute('PRAGMA table_info(mappings)')}
        if 'original_form' not in cols:
            conn.execute("ALTER TABLE mappings ADD COLUMN original_form TEXT NOT NULL DEFAULT ''")
            conn.execute('UPDATE mappings SET original_form = canonical_form WHERE original_form = ""')
            conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_map_orig ON mappings(session_id, original_form, entity_type)')
        # Migration: canonical_form is restored on deanon only when the user edited it
        cols = {r[1] for r in conn.execute('PRAGMA table_info(mappings)')}
        if 'canonical_edited' not in cols:
            conn.execute('ALTER TABLE mappings ADD COLUMN canonical_edited INTEGER NOT NULL DEFAULT 0')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS occurrences (
                session_id TEXT NOT NULL,
                file_key   TEXT NOT NULL,
                seq        INTEGER NOT NULL,
                token      TEXT NOT NULL,
                original   TEXT NOT NULL
            )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_occ ON occurrences(session_id, file_key)')
        # v4: places — exact forms per anonymized paragraph/cell, keyed by a fingerprint
        # of its anonymized text; restore uses them by place, not by occurrence number
        conn.execute('''
            CREATE TABLE IF NOT EXISTS places (
                session_id TEXT NOT NULL,
                place_hash TEXT NOT NULL,
                items      TEXT NOT NULL,
                PRIMARY KEY (session_id, place_hash)
            )''')
        # v4: mappings are never deleted — status active / excluded (user: «не маскировать») /
        # replaced (user edited it); inactive rows still restore files downloaded earlier
        # identical anonymized places of one file (two signature lines «/ [FIO_1] /») keep
        # their own forms: one row per occurrence, in document order
        conn.execute('''
            CREATE TABLE IF NOT EXISTS place_forms (
                session_id TEXT NOT NULL,
                file_key   TEXT NOT NULL,
                place_hash TEXT NOT NULL,
                seq        INTEGER NOT NULL,
                items      TEXT NOT NULL,
                gen        REAL NOT NULL DEFAULT 0
            )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_place_forms ON place_forms(session_id, place_hash)')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS token_aliases (
                session_id TEXT NOT NULL,
                old_token  TEXT NOT NULL,
                new_token  TEXT NOT NULL,
                PRIMARY KEY (session_id, old_token)
            )''')
        # «Это разные»: pairs of tokens the user said are different entities (task 3, §5.4)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS distinct_pairs (
                session_id TEXT NOT NULL,
                a          TEXT NOT NULL,
                b          TEXT NOT NULL,
                PRIMARY KEY (session_id, a, b)
            )''')
        cols = {r[1] for r in conn.execute('PRAGMA table_info(mappings)')}
        if 'status' not in cols:
            conn.execute("ALTER TABLE mappings ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        if 'opf' not in cols:      # legal form of a company as in the document («ООО»), task 3 §2.2
            conn.execute("ALTER TABLE mappings ADD COLUMN opf TEXT")
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version < SCHEMA_VERSION:
            # v2.2 databases may hold duplicate tokens from the old COUNT(*)+1 bug — renumber
            # them once. From v3 one token may legitimately cover several forms of one entity.
            if version < 3:
                _dedupe_duplicate_tokens(conn)
            conn.execute('DROP INDEX IF EXISTS idx_map_token')
            conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_map_tok ON mappings(session_id, token)')


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


def rename_session(db_path, session_id: str, name: str):
    with get_conn(db_path) as conn:
        conn.execute('UPDATE sessions SET name=? WHERE id=?', (name, session_id))


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
        conn.execute('DELETE FROM occurrences WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM places WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM place_forms WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM token_aliases WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM distinct_pairs WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM mappings    WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM exclusions  WHERE session_id=?', (session_id,))
        conn.execute('DELETE FROM sessions    WHERE id=?',         (session_id,))


def get_session_mappings(db_path, session_id: str, include_inactive: bool = False):
    """Active entries (what the UI table shows); include_inactive adds the archive."""
    with get_conn(db_path) as conn:
        rows = conn.execute('''
            SELECT token, original_form, canonical_form, entity_type, canonical_edited, created_at, status
            FROM mappings
            WHERE session_id=? AND (status='active' OR ?)
            ORDER BY entity_type, token
        ''', (session_id, int(include_inactive))).fetchall()
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
    'КАРТА':    'CARD',
    'IBAN':     'IBAN',
    'КАДАСТР':  'CAD',
    'ГОСНОМЕР': 'CAR',
    'VIN':      'VIN',
    'ОКПО':     'OKPO',
    'ПОЛИС':    'OMS',
    'ВУ':       'DL',
    'НИК':      'NICK',
    'НОТАРИУС': 'NOT',
    'НЕДВИЖ':   'REALTY',
    'ЕГРН':     'EGRN',
    'РЕГНОМЕР': 'REG',
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


def _same_entity_token(conn, session_id, original, etype):
    """Token of an already known entity this value is another form of, else None.

    Persons: same surname (any case/gender form) with compatible name/initials;
    a lone surname joins only if exactly one known person has it.
    Other types: same normalized value (digits for numbers, word lemmas for companies)."""
    from core.entities import Person
    rows = conn.execute("SELECT token, original_form FROM mappings WHERE session_id=? AND entity_type=? "
                        "AND status='active'", (session_id, etype)).fetchall()
    if not rows:
        return None
    if etype in ('ФИО', 'FIO'):
        if any(ch.isdigit() for ch in original):
            return None
        me = Person.parse(original)
        if not (me.surname or me.name):
            return None
        cands = {}
        for r in rows:
            other = Person.parse(r['original_form'])
            if (other.surname or other.name) and me.compatible(other):
                cands.setdefault(r['token'], max(cands.get(r['token'], 0), other.specificity()))
        if len(cands) == 1:
            return next(iter(cands))
        if len(cands) > 1 and me.specificity() >= 2:
            # several namesakes: the most specific compatible one
            return max(cands, key=cands.get)
        return None
    # numbers: digits (+ series letters); companies: without legal form, quotes, case, ё,
    # spaces, or the same word lemmas; addresses: normalized parts (task 3, §1.1)
    from core.entities import merge_key, org_key
    key = merge_key(original, etype)
    if not key or (etype == 'ЮЛ' and len(key) < 4):
        return None
    lemmas = org_key(original) if etype == 'ЮЛ' else None
    for r in rows:
        if merge_key(r['original_form'], etype) == key:
            return r['token']
        if lemmas and org_key(r['original_form']) == lemmas:
            return r['token']
    return None


def find_same_entity(db_path, session_id: str, original: str, entity_type: str):
    """Active token whose entity this value already is (same row or another form), else None —
    the «Такое значение уже есть» check for manual add and edit (task 3, §1.2)."""
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT token FROM mappings WHERE session_id=? AND original_form=? "
                           "AND entity_type=? AND status='active'",
                           (session_id, original, entity_type)).fetchone()
        if row:
            return row['token']
        return _same_entity_token(conn, session_id, original, entity_type)


def _mark_distinct(conn, session_id, a, b):
    a, b = sorted((a, b))
    conn.execute('INSERT OR IGNORE INTO distinct_pairs (session_id, a, b) VALUES (?,?,?)', (session_id, a, b))


def mark_distinct(db_path, session_id: str, a: str, b: str):
    """«Это разные»: never merged automatically, never proposed again."""
    with get_conn(db_path) as conn:
        _mark_distinct(conn, session_id, a, b)


def get_distinct_pairs(db_path, session_id: str) -> set:
    with get_conn(db_path) as conn:
        rows = conn.execute('SELECT a, b FROM distinct_pairs WHERE session_id=?', (session_id,)).fetchall()
    return {frozenset((r['a'], r['b'])) for r in rows}


def consolidate_session(db_path, session_id: str) -> dict:
    """End-of-processing check (task 3, §1.1): tokens that are one entity by the automatic
    rules are merged into the lowest one; returns {old_token: kept_token}. Old tokens stay
    aliases, so files downloaded earlier still restore."""
    from core.entities import auto_groups
    merged = {}
    for group in auto_groups(get_session_mappings(db_path, session_id),
                             get_distinct_pairs(db_path, session_id)):
        for t in group[1:]:
            merge_tokens(db_path, session_id, t, group[0])
            merged[t] = group[0]
    return merged


def get_or_create_token(db_path, session_id: str,
                        original_form: str, canonical_form: str,
                        entity_type: str, canonical_edited: bool = False, manual: bool = False,
                        separate: bool = False) -> str:
    """Token for a value: the same row, another form of a known entity, or a new token.
    separate=True («Всё равно добавить отдельно»): a new token even if the value looks like a
    known entity; the pair is remembered as different so the end-of-processing check keeps it."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            'SELECT token, status FROM mappings '
            'WHERE session_id=? AND original_form=? AND entity_type=?',
            (session_id, original_form, entity_type)
        ).fetchone()
        if row:
            if row['status'] != 'active' and manual:   # added again by hand after «не маскировать»
                conn.execute("UPDATE mappings SET status='active' WHERE session_id=? AND token=?",
                             (session_id, row['token']))
            return row['token']

        token = _same_entity_token(conn, session_id, original_form, entity_type)
        like = token if separate else None
        if token is None or separate:
            prefix = _PREFIX.get(entity_type, entity_type.upper())
            n = _next_token_number(conn, session_id, prefix)
            token = f'{prefix}_{n}'
        if like:
            _mark_distinct(conn, session_id, like, token)

        conn.execute(
            'INSERT OR IGNORE INTO mappings '
            '(session_id, token, original_form, canonical_form, entity_type, canonical_edited) '
            'VALUES (?,?,?,?,?,?)',
            (session_id, token, original_form, canonical_form, entity_type, int(canonical_edited))
        )
    return token


def merge_tokens(db_path, session_id: str, token: str, into: str):
    """«Это одно и то же»: all forms of `token` now belong to `into`. The old token stays
    known as an alias, so files downloaded before the merge still restore."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE mappings SET token=?, status='active' WHERE session_id=? AND token=?",
                     (into, session_id, token))
        conn.execute('INSERT OR REPLACE INTO token_aliases (session_id, old_token, new_token) VALUES (?,?,?)',
                     (session_id, token, into))


def add_alias(db_path, session_id: str, token: str, original: str, entity_type: str):
    """Another written form of an existing entity («NTI» for «Northwind Trading & Investments»)."""
    with get_conn(db_path) as conn:
        row = conn.execute('SELECT token FROM mappings WHERE session_id=? AND original_form=? AND entity_type=?',
                           (session_id, original, entity_type)).fetchone()
        if row:      # the form was already known under another token: it moves here
            conn.execute("UPDATE mappings SET token=?, status='active' WHERE session_id=? AND original_form=? "
                         "AND entity_type=?", (token, session_id, original, entity_type))
            return
        conn.execute('INSERT INTO mappings (session_id, token, original_form, canonical_form, entity_type) '
                     'VALUES (?,?,?,?,?)', (session_id, token, original, original, entity_type))


def delete_mapping(db_path, session_id: str, token: str):
    """«Не маскировать»: the token stops being used, but its values are kept so that
    files downloaded earlier with this token still restore."""
    _set_status(db_path, session_id, token, 'excluded')


def retire_mapping(db_path, session_id: str, token: str):
    """The user edited the entry: the old token is replaced by a new one, kept for old files."""
    _set_status(db_path, session_id, token, 'replaced')


def _set_status(db_path, session_id, token, status):
    with get_conn(db_path) as conn:
        conn.execute('UPDATE mappings SET status=? WHERE session_id=? AND token=?',
                     (status, session_id, token))


def save_places(db_path, session_id: str, places, file_key: str = ''):
    """places: [(fingerprint of the anonymized place, [(token, original form), ...])] in
    document order. Every version of a file is kept: a file downloaded before a mapping
    edit (same name, other tokens) must still restore exactly."""
    import time
    gen = time.time()
    with get_conn(db_path) as conn:
        conn.executemany('INSERT INTO place_forms (session_id, file_key, place_hash, seq, items, gen) '
                         'VALUES (?,?,?,?,?,?)',
                         [(session_id, file_key, h, i, json.dumps(items, ensure_ascii=False), gen)
                          for i, (h, items) in enumerate(places)])


def get_places(db_path, session_id: str, file_key: str = None) -> dict:
    """{fingerprint: [items of 1st occurrence, items of 2nd, ...]} — the given file first,
    then every other file of the session."""
    with get_conn(db_path) as conn:
        rows = conn.execute('SELECT file_key, place_hash, items FROM place_forms WHERE session_id=? '
                            'ORDER BY (file_key = ?) DESC, gen DESC, file_key, seq',
                            (session_id, file_key or '')).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['place_hash'], []).append([tuple(x) for x in json.loads(r['items'])])
    return out


def update_mapping(db_path, session_id: str, token: str, data: dict):
    with get_conn(db_path) as conn:
        if data.get('canonical_form'):
            conn.execute(
                'UPDATE mappings SET canonical_form=?, canonical_edited=1 WHERE session_id=? AND token=?',
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
    """Return {token: text-to-restore} for deanonymization of new text (e.g. an LLM reply).

    One token may cover several forms of one entity («Иванов», «Иванову»); the first
    found form (usually the full one from the preamble) is restored. The user's manual
    «Базовая форма» (canonical_edited=1) wins. Exact per-place forms of the original
    file come from get_occurrences().
    """
    return {t: v for t, (v, _) in get_reverse_info(db_path, session_id).items()}


def get_reverse_info(db_path, session_id: str) -> dict:
    """{token: (value, edited_by_user)}."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT token, original_form, canonical_form, canonical_edited, status "
            "FROM mappings WHERE session_id=? ORDER BY (status='active') DESC, id",
            (session_id,)
        ).fetchall()
    out = {}
    for r in rows:
        canon = (r['canonical_form'] or '').strip()
        orig = (r['original_form'] or '').strip()
        if r['canonical_edited'] and canon:
            out[r['token']] = (canon, True)
        elif r['token'] not in out:
            out[r['token']] = (orig or canon, False)
    with get_conn(db_path) as conn:
        aliases = conn.execute('SELECT old_token, new_token FROM token_aliases WHERE session_id=?',
                               (session_id,)).fetchall()
    for a in aliases:                     # tokens merged away: old files still restore
        if a['old_token'] not in out and a['new_token'] in out:
            out[a['old_token']] = out[a['new_token']]
    return out


def set_org_forms(db_path, session_id: str, forms: dict):
    """{token: «ООО»} — the legal form found next to a company name; the first one is kept."""
    with get_conn(db_path) as conn:
        for tok, opf in forms.items():
            conn.execute("UPDATE mappings SET opf=? WHERE session_id=? AND token=? AND (opf IS NULL OR opf='')",
                         (opf, session_id, tok))


def get_org_forms(db_path, session_id: str) -> dict:
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT token, opf FROM mappings WHERE session_id=? AND opf IS NOT NULL AND opf!=''",
                            (session_id,)).fetchall()
        aliases = conn.execute('SELECT old_token, new_token FROM token_aliases WHERE session_id=?',
                               (session_id,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['token'], r['opf'])
    for a in aliases:
        if a['old_token'] not in out and a['new_token'] in out:
            out[a['old_token']] = out[a['new_token']]
    return out


def save_occurrences(db_path, session_id: str, file_key: str, items):
    """items: [(token, original)] in document order."""
    with get_conn(db_path) as conn:
        conn.execute('DELETE FROM occurrences WHERE session_id=? AND file_key=?', (session_id, file_key))
        conn.executemany('INSERT INTO occurrences (session_id, file_key, seq, token, original) VALUES (?,?,?,?,?)',
                         [(session_id, file_key, i, t, o) for i, (t, o) in enumerate(items)])


def get_occurrences(db_path, session_id: str, file_key: str) -> dict:
    """{token: [original forms in document order]} for one anonymized file."""
    with get_conn(db_path) as conn:
        rows = conn.execute('SELECT token, original FROM occurrences WHERE session_id=? AND file_key=? '
                            'ORDER BY seq', (session_id, file_key)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['token'], []).append(r['original'])
    return out

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
            return ExclusionSet((r['original_form'], r['entity_type']) for r in rows)
        except Exception:
            return ExclusionSet()


class ExclusionSet(set):
    """Values the user marked «не маскировать». `(value, type) in excl` matches every form
    of the same entity — another case of the name, another spelling of the company —
    not only the exact string, so an excluded entity is never re-detected."""

    def __init__(self, items=()):
        super().__init__(items)
        self._keys = None

    def __contains__(self, item):
        if set.__contains__(self, item):
            return True
        try:
            value, etype = item
        except (TypeError, ValueError):
            return False
        if self._keys is None:
            from core.entities import value_key
            self._keys = {(t, value_key(v, t)) for v, t in set.__iter__(self)}
        from core.entities import value_key, Person
        if (etype, value_key(value, etype)) in self._keys:
            return True
        if etype == 'ФИО':
            me = Person.parse(value)
            return any(t == 'ФИО' and (me.surname or me.name) and Person.parse(v).compatible(me)
                       and Person.parse(v).specificity() >= 2 for v, t in set.__iter__(self))
        return False


def delete_session_exclusions(db_path, session_id: str):
    with get_conn(db_path) as conn:
        conn.execute('DELETE FROM exclusions WHERE session_id=?', (session_id,))


# ── Global "known entities" — cross-session learning ─────────────────────────
def remember_entity(db_path, value: str, entity_type: str):
    """Record a (value, type) pair so future sessions auto-detect it."""
    v = (value or '').strip()
    t = (entity_type or '').strip()
    if not v or not t:
        return
    with get_conn(db_path) as conn:
        try:
            conn.execute(
                'INSERT INTO known_entities (value, entity_type) VALUES (?,?) '
                'ON CONFLICT(value, entity_type) DO UPDATE SET '
                'seen_count = seen_count + 1, '
                "last_seen_at = datetime('now','localtime')",
                (v, t)
            )
        except Exception:
            # Fallback for SQLite without ON CONFLICT support (very old)
            existing = conn.execute(
                'SELECT seen_count FROM known_entities WHERE value=? AND entity_type=?',
                (v, t)
            ).fetchone()
            if existing:
                conn.execute(
                    'UPDATE known_entities SET seen_count = seen_count + 1 '
                    'WHERE value=? AND entity_type=?',
                    (v, t)
                )
            else:
                conn.execute(
                    'INSERT INTO known_entities (value, entity_type) VALUES (?,?)',
                    (v, t)
                )


def get_known_entities(db_path, limit: int = 500) -> list:
    """Return list of {value, entity_type, seen_count} ordered by frequency."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            'SELECT value, entity_type, seen_count FROM known_entities '
            'ORDER BY seen_count DESC, last_seen_at DESC LIMIT ?',
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
