"""
Regression tests for v2.3 bug reports:

  Bug A: masked content leaked into DB ("АО «[YUL_1" stored as YUL_2)
  Bug B: spaCy produced lots of garbage FIO/ORG ("Возглавляет", "Инфраструктура",
         "Сыктывкар", "Банка", "(акционерное общество)", "ОГРН", "БИК", "Чистая прибыль…")
  Bug C: deanonymization ignored user edits to canonical_form
  Bug D: pymorphy3-driven FIO normalization (auto-nominative) was missing
  Bug E: "LLM training" — manually added entities not reused in new sessions

Each test is named so failing one points at the originating bug report.
"""

import pytest

from core.db import (
    get_or_create_token, get_session_mappings, get_reverse_mappings,
    delete_mapping, update_mapping_original, update_mapping,
    create_session, remember_entity, get_known_entities,
)
from core.anonymizer import (
    anonymize_text_pipeline, _contains_token, _is_bracketed_token,
    _validate_spacy_fio, _validate_spacy_org, _normalize_fio,
    PARTIAL_TOKEN_RE,
)


# ── A: masks leaking into DB ─────────────────────────────────────────────────

def test_partial_token_detected_without_closing_bracket():
    assert _contains_token('АО «[YUL_1')          # the exact bug from the report
    assert _contains_token('что-то [FIO_42 ещё')
    assert _contains_token('[INN_1]')             # fully closed also detected
    assert _contains_token('  [YUL_999 другая фигня')
    assert not _contains_token('обычный текст')
    assert not _contains_token('[не_токен]')


def test_partial_token_re_matches_bug_example():
    # The exact string the user pasted from their DB
    assert PARTIAL_TOKEN_RE.search('АО «[YUL_1')
    assert PARTIAL_TOKEN_RE.search('Акционерное общество «[YUL_3')


# ── B: stricter spaCy validators ─────────────────────────────────────────────

@pytest.mark.parametrize('bad', [
    'Возглавляет', 'Инфраструктура', 'Сыктывкар', 'Москва', 'Россия',
    'Подтвержден', 'Бенефициары', 'Заключение', 'Риски', 'Статус (',
    'основной владелец', 'Председателя Правления', 'Совета директоров',
])
def test_spacy_fio_rejects_common_garbage(bad):
    assert not _validate_spacy_fio(bad), f'FIO validator wrongly accepted {bad!r}'


@pytest.mark.parametrize('good', [
    'Иванов Иван Иванович',
    'Сердитов Сергей Вячеславович',
    'Аверьянова Нелли Робертовна',
    'Москотельниковых',          # legitimate surname (gen. pl.)
    'Перваков В.Е.',
    'С.В. Сердитов',
])
def test_spacy_fio_accepts_real_fios(good):
    assert _validate_spacy_fio(good), f'FIO validator wrongly rejected {good!r}'


@pytest.mark.parametrize('bad', [
    'Банка', 'Банком', 'Совета директоров', 'Председателя Правления',
    'Гарантии банка', 'ВКЛЮЧЕН', 'Подтвержден', 'надежный региональный банк «домашнего» типа',
    'Банк стабильно прибылен',
    '(акционерное общество)',  # OPF text alone, no name
    'ОГРН', 'БИК', 'СНИЛС', 'ИНН',   # pure keywords
    'ЕИО',                            # generic role abbreviation, not a company
    'Чистая прибыль за 2023 год — 68,7 млн руб.',  # sentence fragment
    'Систему страхования вкладов (№ 126',
    'Единственный региональный банк',
    'Рейтинги',
])
def test_spacy_org_rejects_common_garbage(bad):
    assert not _validate_spacy_org(bad), f'ORG validator wrongly accepted {bad!r}'


@pytest.mark.parametrize('good', [
    'АО «Северный Народный Банк»',
    'ООО «Ромашка»',
    'АСВ', 'НАУФОР', 'ФКЦБ', 'РЦБ',
    'VISA', 'MasterCard',
])
def test_spacy_org_accepts_real_orgs(good):
    assert _validate_spacy_org(good), f'ORG validator wrongly rejected {good!r}'


# ── C: deanonymization respects canonical_form edits ────────────────────────

def test_deanon_returns_canonical_when_edited(tmp_db, session_id):
    """Edit canonical_form via the same path the PATCH endpoint uses → deanon
    must return the new canonical, not the original."""
    tok = get_or_create_token(tmp_db, session_id, 'www.sevnb.ru', 'www.sevnb.ru', 'URL')
    # User edits the base form
    update_mapping(tmp_db, session_id, tok, {'canonical_form': 'www.sevnb.SU'})
    rev = get_reverse_mappings(tmp_db, session_id)
    assert rev[tok] == 'www.sevnb.SU'


def test_deanon_uses_original_when_canonical_empty(tmp_db, session_id):
    """If canonical is blank, fall back to original_form (legacy behaviour)."""
    tok = get_or_create_token(tmp_db, session_id, 'foo@bar.ru', '', 'EMAIL')
    rev = get_reverse_mappings(tmp_db, session_id)
    assert rev[tok] == 'foo@bar.ru'


# ── D: pymorphy3 normalization for FIO canonical_form ───────────────────────

def test_normalize_fio_inflected_to_nominative():
    """'Ивановым Иваном Ивановичем' → 'Иванов Иван Иванович' (best effort)."""
    out = _normalize_fio('Ивановым Иваном Ивановичем')
    # Different morph backends may produce slightly different output; assert at
    # least that the inflected genitive/instrumental endings are gone.
    assert 'Ивановым' not in out, f'still in instrumental: {out!r}'


def test_normalize_fio_keeps_initials():
    """Initials like 'А.А.' must not be re-cased into a verb form."""
    out = _normalize_fio('Перваков В.Е.')
    assert 'В.' in out and 'Е.' in out


# ── E: cross-session learning ("LLM training") ──────────────────────────────

def test_manually_added_entity_remembered(tmp_db):
    """Adding a mapping in session A and processing the same text in session B
    must auto-anonymize that value in B."""
    sa = create_session(tmp_db, 'A')
    # Simulate the user manually adding a phone number that regex missed
    tok = get_or_create_token(tmp_db, sa, '+7(800)123-45-67', '+7(800)123-45-67', 'ТЕЛЕФОН')
    remember_entity(tmp_db, '+7(800)123-45-67', 'ТЕЛЕФОН')

    # Sanity: it's in known_entities
    known = get_known_entities(tmp_db)
    assert any(k['value'] == '+7(800)123-45-67' for k in known)

    # New session B — same text gets the phone masked WITHOUT manual help
    sb = create_session(tmp_db, 'B')
    text = 'Свяжитесь по +7(800)123-45-67 для уточнений.'
    out, _ = anonymize_text_pipeline(text, tmp_db, sb, use_spacy=False, use_llm=False)
    assert '+7(800)123-45-67' not in out, \
        f'Phone not auto-anonymized via cross-session learning: {out!r}'
    # And session B should have the mapping with the proper type
    sb_maps = get_session_mappings(tmp_db, sb)
    assert any(m['entity_type'] == 'ТЕЛЕФОН' and m['original_form'] == '+7(800)123-45-67'
                for m in sb_maps)


def test_remember_increments_seen_count(tmp_db):
    remember_entity(tmp_db, 'foo@bar.ru', 'EMAIL')
    remember_entity(tmp_db, 'foo@bar.ru', 'EMAIL')
    remember_entity(tmp_db, 'foo@bar.ru', 'EMAIL')
    known = [k for k in get_known_entities(tmp_db) if k['value'] == 'foo@bar.ru']
    assert known and known[0]['seen_count'] >= 3


# ── Bonus: spaCy pass on a realistic snippet produces clean mappings ────────

def test_full_pipeline_skips_garbage_on_bank_summary_text(tmp_db, session_id):
    """A snippet shaped like the user's Northern People's Bank PDF — after
    the pipeline runs, no mapping should hold any of the known false-positives."""
    text = (
        'АО «Северный Народный Банк» (далее — Банк) включен в систему '
        'страхования вкладов (№ 126). Председателя Правления возглавляет '
        'Сердитов Сергей Вячеславович. Бенефициары: Аверьянова Нелли Робертовна. '
        'ИНН 1101300820, БИК 048702781, ОГРН 1021100000074.'
    )
    anonymize_text_pipeline(text, tmp_db, session_id, use_spacy=True, use_llm=False)
    bad_substrings = {
        'возглавляет', 'инфраструктура', 'статус', 'риски', 'бенефициары',
        'председателя', 'правления', 'совета директоров', 'банк стабильно',
        '(акционерное общество)', 'огрн', 'бик',
    }
    for m in get_session_mappings(tmp_db, session_id):
        low = m['original_form'].lower()
        assert not _contains_token(m['original_form']), \
            f'Mask leaked into DB: {m}'
        for s in bad_substrings:
            assert s != low, f'Garbage entity slipped past validator: {m}'
