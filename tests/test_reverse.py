"""Deanonymization restores the exact original form (CLAUDE_CODE_TASK.md §5, item 18)."""
from core.anonymizer import apply_reverse
from core.db import get_or_create_token, get_reverse_mappings, update_mapping


def test_reverse_restores_original_case_not_nominative(tmp_db, session_id):
    tok = get_or_create_token(tmp_db, session_id, 'Кузнецову Игорю', 'Кузнецов Игорь', 'ФИО')
    assert apply_reverse(f'передать [{tok}]', get_reverse_mappings(tmp_db, session_id)) == 'передать Кузнецову Игорю'


def test_reverse_uses_canonical_after_manual_edit(tmp_db, session_id):
    tok = get_or_create_token(tmp_db, session_id, 'Кузнецову Игорю', 'Кузнецов Игорь', 'ФИО')
    update_mapping(tmp_db, session_id, tok, {'canonical_form': 'Кузнецов И.'})
    assert get_reverse_mappings(tmp_db, session_id)[tok] == 'Кузнецов И.'


def test_reverse_tolerates_llm_token_variants(tmp_db, session_id):
    tok = get_or_create_token(tmp_db, session_id, 'Иванов', 'Иванов', 'ФИО')
    rev = get_reverse_mappings(tmp_db, session_id)
    n = tok.split('_')[1]
    for variant in (f'[{tok}]', tok, f'[FIO {n}]', f'[FIO-{n}]', f'[fio_{n}]', f'[ФИО_{n}]'):
        assert apply_reverse(f'Ответчик {variant} явился', rev) == 'Ответчик Иванов явился', variant
    assert apply_reverse(f'**[{tok}]**', rev) == '**Иванов**'
    # FIO_1 must not eat FIO_10
    assert apply_reverse('FIO_10', rev) == 'FIO_10'
