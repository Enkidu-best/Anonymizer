"""Duplicates in the app database: one entity under several tokens (task 3, §1.1).

    python scripts/audit_db.py [path/to/anon.db] [--values]

Opens the database READ-ONLY (default: the app's working base in Application Support).
For every session groups active entries by the entity key and prints groups where one
key has more than one token. Without --values only counts per type are printed (safe
to paste into a report); --values shows the values on the local screen.
"""
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DB = Path.home() / 'Library' / 'Application Support' / 'Anonymizer' / 'anon.db'


def groups(rows):
    """Groups the app would merge AUTOMATICALLY (same rules as the end-of-processing check):
    {(session, type, n): {token: [values]}}; rows = (session, token, type, value)."""
    from core.entities import auto_groups
    by_sid = defaultdict(list)
    for sid, token, etype, value in rows:
        by_sid[sid].append({'token': token, 'entity_type': etype, 'original_form': value})
    out = {}
    for sid, maps in by_sid.items():
        for n, g in enumerate(auto_groups(maps)):
            etype = next(m['entity_type'] for m in maps if m['token'] == g[0])
            out[(sid, etype, n)] = {t: [m['original_form'] for m in maps if m['token'] == t] for t in g}
    return out


def proposals(rows):
    """Pairs that go to a human («Проверка дублей»), per session."""
    from core.entities import review_pairs
    by_sid = defaultdict(list)
    for sid, token, etype, value in rows:
        by_sid[sid].append({'token': token, 'entity_type': etype, 'original_form': value})
    return {sid: review_pairs(maps) for sid, maps in by_sid.items()}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    show = '--values' in sys.argv
    db = Path(args[0]) if args else DEFAULT_DB
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    cols = {r[1] for r in con.execute('PRAGMA table_info(mappings)')}
    where = "WHERE status='active'" if 'status' in cols else ''
    rows = con.execute(f'SELECT session_id, token, entity_type, original_form FROM mappings {where}').fetchall()
    sessions = {r[0] for r in rows}
    dup = groups(rows)
    by_type = defaultdict(int)
    for (sid, etype, key), toks in dup.items():
        by_type[etype] += 1
    prop = proposals(rows)
    by_type_p = defaultdict(int)
    for pairs in prop.values():
        for p in pairs:
            by_type_p[p['type']] += 1
    print(f'База: {db}\nСессий: {len(sessions)}, записей: {len(rows)}, '
          f'групп «автоматически»: {len(dup)}, пар «предложить человеку»: {sum(by_type_p.values())}')
    for t in sorted(set(by_type) | set(by_type_p)):
        print(f'  {t:10} авто {by_type.get(t, 0):<4} человеку {by_type_p.get(t, 0)}')
    if show:
        for (sid, etype, key), toks in sorted(dup.items(), key=lambda x: x[0][1]):
            print(f'\n[{etype}] сессия {sid[:8]}')
            for tok, vals in toks.items():
                for v in vals:
                    print(f'   {tok:10} {v!r}')
        for sid, pairs in prop.items():
            for p in pairs:
                print(f'\n? [{p["type"]}] {sid[:8]} {p["a"]} {p["a_value"]!r}  ~  {p["b"]} {p["b_value"]!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
