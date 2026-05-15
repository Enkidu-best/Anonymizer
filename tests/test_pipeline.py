"""
Pipeline tests: regex+spaCy passes must not match already-masked content
and must produce a sound bidirectional mapping (anonymize -> deanonymize round-trips).
"""

import re
import pytest

from core.anonymizer import (
    anonymize_text_pipeline,
    apply_reverse,
    ANY_TOKEN_RE,
    _contains_token,
)
from core.db import get_reverse_mappings, get_session_mappings, delete_mapping


def test_contains_token_helper():
    assert _contains_token('[FIO_1]')
    assert _contains_token('что-то [INN_2] ещё')
    assert not _contains_token('просто текст')
    assert not _contains_token('[не_токен]')


def test_basic_anonymize_inn_email(tmp_db, session_id):
    text = ('Договор между ООО "Ромашка" (ИНН 7701234567) и '
            'Ивановым Иваном Ивановичем, e-mail: ivan@test.ru.')
    out, reps = anonymize_text_pipeline(text, tmp_db, session_id, use_spacy=False, use_llm=False)

    assert '7701234567' not in out
    assert 'ivan@test.ru' not in out
    assert '[INN_1]' in out
    assert '[EMAIL_1]' in out


def test_regex_does_not_match_existing_token(tmp_db, session_id):
    """Bug #4: regex (and spaCy when on) must not pick up [TOKEN_N] as new entities."""
    # 1st pass produces masks
    text1 = 'ИНН 7701234567, тел: ivan@x.ru'
    out1, _ = anonymize_text_pipeline(text1, tmp_db, session_id, use_spacy=False, use_llm=False)
    assert '[INN_1]' in out1

    # 2nd pass on the already-masked text — must not create new mappings
    out2, reps2 = anonymize_text_pipeline(out1, tmp_db, session_id, use_spacy=False, use_llm=False)
    assert out1 == out2
    # No mapping should reference content that contains a token mask
    mappings = get_session_mappings(tmp_db, session_id)
    for m in mappings:
        assert not _contains_token(m['original_form']), \
            f"Mapping captured masked text: {m['original_form']!r}"
        assert not _contains_token(m['canonical_form']), \
            f"Mapping canonical contains mask: {m['canonical_form']!r}"


def test_roundtrip_anon_then_deanon(tmp_db, session_id):
    text = 'ИНН 7701234567 e-mail aa@bb.ru СНИЛС 123-456-789 00'
    masked, _ = anonymize_text_pipeline(text, tmp_db, session_id, use_spacy=False, use_llm=False)
    rev = get_reverse_mappings(tmp_db, session_id)
    restored = apply_reverse(masked, rev)
    # Original PII strings should re-appear after deanonymization
    assert '7701234567' in restored
    assert 'aa@bb.ru' in restored


def test_exclusion_persists(tmp_db, session_id):
    """Deleted mapping must NOT reappear on reprocess of the same text."""
    from core.db import add_exclusion
    text = 'ИНН 7701234567'
    anonymize_text_pipeline(text, tmp_db, session_id, use_spacy=False, use_llm=False)
    # Mapping exists for the INN
    mp = get_session_mappings(tmp_db, session_id)
    inn = [m for m in mp if m['entity_type'] == 'ИНН']
    assert len(inn) == 1
    tok = inn[0]['token']
    # User deletes it (and adds to exclusions like the API does)
    add_exclusion(tmp_db, session_id, inn[0]['original_form'], 'ИНН')
    delete_mapping(tmp_db, session_id, tok)
    # Reprocess
    anonymize_text_pipeline(text, tmp_db, session_id, use_spacy=False, use_llm=False)
    # Still no INN mapping
    mp2 = get_session_mappings(tmp_db, session_id)
    assert not [m for m in mp2 if m['entity_type'] == 'ИНН'], \
        'Excluded INN reappeared after reprocess'


def test_all_session_tokens_unique_after_pipeline(tmp_db, session_id):
    """After running a realistic pipeline + manual edits, no two mappings share a token."""
    long_text = (
        'Договор от 01.01.2025 между ООО "Ромашка" (ИНН 7701234567, ОГРН 1027700123456, '
        'КПП 770101001) и Ивановым Петром Сидоровичем, паспорт 4510 123456, '
        'СНИЛС 123-456-789 00, тел: +7 (916) 1234567, e-mail: petya@mail.ru.'
    )
    anonymize_text_pipeline(long_text, tmp_db, session_id, use_spacy=False, use_llm=False)
    mp = get_session_mappings(tmp_db, session_id)
    tokens = [m['token'] for m in mp]
    assert len(tokens) == len(set(tokens)), f'Duplicate tokens after pipeline: {tokens}'

    # Now simulate user edits: delete every other, then add new ones
    for i, m in enumerate(mp):
        if i % 2 == 0:
            delete_mapping(tmp_db, session_id, m['token'])
    from core.db import get_or_create_token
    for i in range(5):
        get_or_create_token(tmp_db, session_id, f'NewVal{i}', f'NewVal{i}', 'ФИО')

    mp2 = get_session_mappings(tmp_db, session_id)
    tokens2 = [m['token'] for m in mp2]
    assert len(tokens2) == len(set(tokens2)), f'Duplicate tokens after edits: {tokens2}'
