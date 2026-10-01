"""Replacements respect word boundaries (CLAUDE_CODE_TASK.md item 17)."""
from docx import Document

from core.anonymizer import anonymize_text_pipeline, replace_bounded
from core.handlers import _replace_para


def test_replace_bounded_keeps_longer_words_and_numbers():
    reps = {'123456': '[PASSPORT_1]', 'Иванов': '[FIO_1]'}
    s = 'паспорт 123456; номер дела 1234567; Иванов; ул. Ивановская; Ивановский завод'
    assert replace_bounded(s, reps) == (
        'паспорт [PASSPORT_1]; номер дела 1234567; [FIO_1]; ул. Ивановская; Ивановский завод')


def test_known_entities_pass_does_not_cut_numbers(tmp_db, session_id):
    text = 'паспорт серии 45 07 № 123456. Сумма 1234567 рублей.'
    out, _ = anonymize_text_pipeline(text, tmp_db, session_id, use_spacy=False)
    assert 'Сумма 1234567 рублей' in out
    assert '123456.' not in out


def test_docx_paragraph_respects_boundaries():
    doc = Document()
    para = doc.add_paragraph('Иванов работает в Ивановский завод')
    _replace_para(para, {'Иванов': '[FIO_1]'})
    assert para.text == '[FIO_1] работает в Ивановский завод'
