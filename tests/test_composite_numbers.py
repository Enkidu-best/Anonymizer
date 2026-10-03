"""Composite numbers are masked whole by one token (task 3, §1.3). Fictional values.

An EGRN registration record: new format «кадастровый номер-NN/NNN/YYYY-N», old format
«NN-NN/NN-NNN/YYYY-NNNN»; a found number continues through «-», «/», «.», «:» and digits
(no space, or one space after the hyphen) — never «[CAD_3]- 71/022/2020-18».
"""
import re

import pytest

from core.anonymizer import anonymize_text, restore_text

CASES = [
    ('Объект: 50:21:0110105:123-50/021/2019-4, площадь 40 кв.м', '50:21:0110105:123-50/021/2019-4'),
    ('запись от 12.03.2020 № 71:22:030301:1234- 71/022/2020-18 в ЕГРН', '71:22:030301:1234- 71/022/2020-18'),
    ('номер записи 47:14:1203001:814 -47/017/2021-2.', '47:14:1203001:814 -47/017/2021-2'),
    ('право зарегистрировано 15.05.2004 за № 50-01/21-07/2004-0123', '50-01/21-07/2004-0123'),
    ('рег. № 77-77-12/005/2006-123 от 01.02.2006', '77-77-12/005/2006-123'),
    ('запись 77-01/01-014/2000- 3143 погашена', '77-01/01-014/2000- 3143'),
]


@pytest.mark.parametrize('text,value', CASES)
def test_whole_record_one_token(tmp_db, session_id, text, value):
    out, occ = anonymize_text(text, tmp_db, session_id, use_spacy=False)
    toks = re.findall(r'\[[A-Z_]+_\d+\]', out)
    assert len(toks) == 1, out
    assert [v for vals in occ.values() for v in vals] == [value], (out, occ)
    assert not re.search(r'\]\s*-\s*\d', out), out              # nothing left after the token
    assert restore_text(out, tmp_db, session_id, occ) == text


def test_two_cadastral_numbers_stay_two(tmp_db, session_id):
    text = 'участки 50:21:0110105:123-50:21:0110105:124 и 50:21:0110105:125'
    out, occ = anonymize_text(text, tmp_db, session_id, use_spacy=False)
    assert len(occ) == 3, (out, occ)


def test_inn_kpp_pair_not_glued(tmp_db, session_id):
    out, occ = anonymize_text('ИНН/КПП 7707083893/770701001', tmp_db, session_id, use_spacy=False)
    assert not any('/' in v for vals in occ.values() for v in vals), occ


def test_court_case_number_untouched(tmp_db, session_id):
    text = 'по делу А40-227812/2024 Арбитражного суда'
    out, _ = anonymize_text(text, tmp_db, session_id, use_spacy=False)
    assert out == text
