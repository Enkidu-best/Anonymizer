"""One value = one token (task 2, §1.4): forms of the same company / address merge."""
from core.db import get_or_create_token


def test_ocr_space_inside_company_name_merges(tmp_db, session_id):
    a = get_or_create_token(tmp_db, session_id, 'ВекторФуд', 'ВекторФуд', 'ЮЛ')
    b = get_or_create_token(tmp_db, session_id, 'ВекторФ уд', 'ВекторФ уд', 'ЮЛ')
    c = get_or_create_token(tmp_db, session_id, 'ВЕКТОРФУД', 'ВЕКТОРФУД', 'ЮЛ')
    d = get_or_create_token(tmp_db, session_id, 'Вектор\xadФуд', 'Вектор\xadФуд', 'ЮЛ')
    assert a == b == c == d


def test_address_abbreviations_merge(tmp_db, session_id):
    a = get_or_create_token(tmp_db, session_id, 'г. Тверь, ул. Озёрная, д. 17, кв. 41', 'x', 'АДРЕС')
    b = get_or_create_token(tmp_db, session_id, 'город Тверь, улица Озерная, дом 17, квартира 41', 'x', 'АДРЕС')
    c = get_or_create_token(tmp_db, session_id, 'г. Тверь, ул. Озёрная, д. 19', 'x', 'АДРЕС')
    assert a == b and a != c


def test_different_companies_stay_apart(tmp_db, session_id):
    a = get_or_create_token(tmp_db, session_id, 'Вектор', 'Вектор', 'ЮЛ')
    b = get_or_create_token(tmp_db, session_id, 'Вектор-Плюс', 'Вектор-Плюс', 'ЮЛ')
    assert a != b
