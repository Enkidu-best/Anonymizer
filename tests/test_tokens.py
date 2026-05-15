"""
Regression tests for token bookkeeping in core.db.

Covers:
- Idempotency: same (original, type) -> same token
- Uniqueness: every token in a session is unique even after deletions
- Re-add after delete: must NOT reuse a freed number (would collide if logs reference it)
- Token suffix monotonically increases (uses MAX, not COUNT)
"""

import pytest
from core.db import (
    get_or_create_token,
    delete_mapping,
    get_session_mappings,
)


def test_token_idempotent(tmp_db, session_id):
    t1 = get_or_create_token(tmp_db, session_id, 'Иванов И.И.', 'Иванов И.И.', 'ФИО')
    t2 = get_or_create_token(tmp_db, session_id, 'Иванов И.И.', 'Иванов И.И.', 'ФИО')
    assert t1 == t2
    assert t1 == 'FIO_1'


def test_tokens_unique_after_delete(tmp_db, session_id):
    """Bug #3: delete + add must not reuse the deleted number."""
    a = get_or_create_token(tmp_db, session_id, 'A', 'A', 'ФИО')
    b = get_or_create_token(tmp_db, session_id, 'B', 'B', 'ФИО')
    c = get_or_create_token(tmp_db, session_id, 'C', 'C', 'ФИО')
    assert (a, b, c) == ('FIO_1', 'FIO_2', 'FIO_3')

    # delete middle one
    delete_mapping(tmp_db, session_id, 'FIO_2')

    # add another — must be FIO_4, not FIO_3 (collision with 'C')
    d = get_or_create_token(tmp_db, session_id, 'D', 'D', 'ФИО')
    assert d == 'FIO_4'

    tokens = [m['token'] for m in get_session_mappings(tmp_db, session_id)]
    assert len(tokens) == len(set(tokens)), f'Duplicate tokens: {tokens}'


def test_tokens_unique_after_many_deletes(tmp_db, session_id):
    """Stress: delete several, re-add several, then check uniqueness."""
    for i in range(10):
        get_or_create_token(tmp_db, session_id, f'name-{i}', f'name-{i}', 'ФИО')
    # delete 3, 5, 7
    for n in (3, 5, 7):
        delete_mapping(tmp_db, session_id, f'FIO_{n}')
    # add 3 more — should be FIO_11, FIO_12, FIO_13 (NOT 3, 5, 7)
    new = [get_or_create_token(tmp_db, session_id, f'new-{i}', f'new-{i}', 'ФИО')
           for i in range(3)]
    assert new == ['FIO_11', 'FIO_12', 'FIO_13']

    tokens = [m['token'] for m in get_session_mappings(tmp_db, session_id)]
    assert len(tokens) == len(set(tokens))


def test_different_types_independent_counters(tmp_db, session_id):
    f1 = get_or_create_token(tmp_db, session_id, 'Иван', 'Иван', 'ФИО')
    y1 = get_or_create_token(tmp_db, session_id, 'ООО Х', 'ООО Х', 'ЮЛ')
    i1 = get_or_create_token(tmp_db, session_id, '7700000001', '7700000001', 'ИНН')
    assert f1 == 'FIO_1'
    assert y1 == 'YUL_1'
    assert i1 == 'INN_1'


def test_session_isolation(tmp_db):
    from core.db import create_session
    s1 = create_session(tmp_db, 'one')
    s2 = create_session(tmp_db, 'two')
    a = get_or_create_token(tmp_db, s1, 'X', 'X', 'ФИО')
    b = get_or_create_token(tmp_db, s2, 'X', 'X', 'ФИО')
    assert a == 'FIO_1'
    assert b == 'FIO_1'
    assert a == b  # numbering resets per session
