"""One entity — one token; merge rules by type (task 3, §1.1). Data is fictional.

automatic: numbers by digits (+ series letters), persons by lemmas + compatible initials,
companies after normalization (legal form, quotes, case, ё, spaces), addresses after
normalization (abbreviations, punctuation, spaces, line breaks, word order in a part);
proposed to a human: namesakes with other initials, companies 1 char per 8 apart,
an address that is another one plus a room / flat; never: different numbers, houses,
корпуса, rooms.
"""
import pytest

from core.db import consolidate_session, get_or_create_token, get_session_mappings
from core.entities import merge_key, review_pairs


def _tokens(db, sid, etype):
    return {m['original_form']: m['token'] for m in get_session_mappings(db, sid) if m['entity_type'] == etype}


def _add(db, sid, etype, values):
    return [get_or_create_token(db, sid, v, v, etype) for v in values]


SAME = {
    'ЮЛ': ['ОсОО «Вектор Трейд', 'Вектор Трейд', 'ВЕКТОР ТРЕЙД', 'ООО "Вектор Трейд"', 'Вектор  Трёйд'],
    'АДРЕС': ['г.Москва, ул. Лесная, д.5', 'город Москва, улица Лесная, дом 5', 'г. Москва, ул. Лесная, д. 5.',
              'г. Москва,\nул. Лесная, д. 5', 'Москва г., Лесная ул., д. 5', 'г. Москва ул. Лесная д. 5'],
    'КАДАСТР': ['50:21:0110105:123', '50:21:0110105 :123'],
    'ТЕЛЕФОН': ['+7 (912) 345-67-89', '8 912 345 67 89', '89123456789'],
    'ПАСПОРТ': ['45 10 123456', '4510 123456', '4510№123456'],
    'ИНН': ['7707083893', '77 07 083893'],
    'EMAIL': ['Info@Vector-Trade.example', 'info@vector-trade.example'],
    'ФИО': ['Белозёров Аркадий Львович', 'Белозерова Аркадия Львовича', 'А.Л. Белозёров', 'БЕЛОЗЁРОВ А. Л.'],
}

SETTLEMENT = ['Тверская обл., Конаковский район, пос. Озёрный, ул. Лесная, д. 5',
              'Тверская область, р-н Конаковский, пгт Озерный, ул. Лесная, д.5',
              'Тверская область, Конаковский р-н, поселок Озерный, улица Лесная, дом 5']

DIFFERENT = {
    'КАДАСТР': ['50:21:0110105:123', '50:21:0110105:124'],
    'ИНН': ['7707083893', '7736207543'],
    'АДРЕС': ['г. Москва, ул. Лесная, д. 5', 'г. Москва, ул. Лесная, д. 7', 'г. Москва, ул. Лесная, д. 5, корп. 2',
              'г. Москва, ул. Лесная, д. 5, кв. 1', 'г. Москва, ул. Лесная, д. 5, кв. 2'],
    'ЮЛ': ['Вектор Трейд', 'Вектор Трейд Плюс'],
    'ФИО': ['Белозёров Аркадий Львович', 'Белозёров Павел Сергеевич'],
}


@pytest.mark.parametrize('etype', list(SAME))
def test_spellings_of_one_entity_get_one_token(tmp_db, session_id, etype):
    toks = _add(tmp_db, session_id, etype, SAME[etype])
    assert len(set(toks)) == 1, dict(zip(SAME[etype], toks))


def test_settlement_and_district_spellings(tmp_db, session_id):
    toks = _add(tmp_db, session_id, 'АДРЕС', SETTLEMENT)
    assert len(set(toks)) == 1


@pytest.mark.parametrize('etype', list(DIFFERENT))
def test_different_entities_stay_apart(tmp_db, session_id, etype):
    toks = _add(tmp_db, session_id, etype, DIFFERENT[etype])
    assert len(set(toks)) == len(toks), dict(zip(DIFFERENT[etype], toks))


def test_merge_key_numbers_never_by_similarity():
    assert merge_key('50:21:0110105:123', 'КАДАСТР') != merge_key('50:21:0110105:128', 'КАДАСТР')
    assert merge_key('77АМ 335131', 'РЕГНОМЕР') == merge_key('77 АМ №335131', 'РЕГНОМЕР')
    assert merge_key('77АМ 335131', 'РЕГНОМЕР') != merge_key('77АН 335131', 'РЕГНОМЕР')


def test_consolidation_merges_rows_created_before_the_rules(tmp_db, session_id):
    """Rows written by an older version (separate tokens) are merged at the end of processing;
    the old token stays an alias so files downloaded earlier still restore."""
    from core.db import get_conn, get_reverse_info
    with get_conn(tmp_db) as c:
        for i, v in enumerate(['Вектор Трейд', 'ОсОО «Вектор Трейд'], 1):
            c.execute('INSERT INTO mappings (session_id, token, original_form, canonical_form, entity_type) '
                      'VALUES (?,?,?,?,?)', (session_id, f'[YUL_{i}]', v, v, 'ЮЛ'))
        for i, v in enumerate(['г. Москва, ул. Лесная, д. 5', 'г.Москва, ул.Лесная, д.5.'], 1):
            c.execute('INSERT INTO mappings (session_id, token, original_form, canonical_form, entity_type) '
                      'VALUES (?,?,?,?,?)', (session_id, f'[ADR_{i}]', v, v, 'АДРЕС'))
    merged = consolidate_session(tmp_db, session_id)
    assert merged == {'[YUL_2]': '[YUL_1]', '[ADR_2]': '[ADR_1]'}
    assert set(_tokens(tmp_db, session_id, 'ЮЛ').values()) == {'[YUL_1]'}
    assert set(_tokens(tmp_db, session_id, 'АДРЕС').values()) == {'[ADR_1]'}
    info = get_reverse_info(tmp_db, session_id)
    assert info  # old token still known (alias)
    assert consolidate_session(tmp_db, session_id) == {}


def _pairs(db, sid, rejected=()):
    return {(p['type'], frozenset((p['a_value'], p['b_value'])))
            for p in review_pairs(get_session_mappings(db, sid), rejected)}


def test_proposals_for_a_human(tmp_db, session_id):
    _add(tmp_db, session_id, 'ФИО', ['Белозёров Аркадий Львович', 'Белозёров П.С.'])
    _add(tmp_db, session_id, 'ЮЛ', ['Вектор Трейд', 'Вектор Тренд'])
    _add(tmp_db, session_id, 'АДРЕС', ['г. Москва, ул. Лесная, д. 5', 'г. Москва, ул. Лесная, д. 5, пом. 3',
                                       'г. Москва, ул. Лесная, д. 7', 'Тверская обл., Конаковский р-н'])
    _add(tmp_db, session_id, 'КАДАСТР', ['50:21:0110105:123', '50:21:0110105:124'])
    p = _pairs(tmp_db, session_id)
    assert ('ФИО', frozenset(('Белозёров Аркадий Львович', 'Белозёров П.С.'))) in p
    assert ('ЮЛ', frozenset(('Вектор Трейд', 'Вектор Тренд'))) in p
    assert ('АДРЕС', frozenset(('г. Москва, ул. Лесная, д. 5', 'г. Москва, ул. Лесная, д. 5, пом. 3'))) in p
    # never: other house, a region without a house, numbers
    assert not any(t == 'АДРЕС' and 'г. Москва, ул. Лесная, д. 7' in v for t, v in p)
    assert not any(t == 'АДРЕС' and 'Тверская обл., Конаковский р-н' in v for t, v in p)
    assert not any(t == 'КАДАСТР' for t, v in p)


def test_rejected_pairs_are_not_proposed_again(tmp_db, session_id):
    a, b = _add(tmp_db, session_id, 'ЮЛ', ['Вектор Трейд', 'Вектор Тренд'])
    assert review_pairs(get_session_mappings(tmp_db, session_id))
    assert not review_pairs(get_session_mappings(tmp_db, session_id), {frozenset((a, b))})


def _client(tmp_path):
    import importlib
    import os
    import sys
    os.environ['ANONYMIZER_DATA_DIR'] = str(tmp_path / 'data')
    sys.modules.pop('app', None)
    app_mod = importlib.import_module('app')
    c = app_mod.app.test_client()
    sid = c.post('/api/sessions', json={'name': 'x'}).get_json()['id']
    return app_mod, c, sid


def test_manual_add_warns_about_existing_value(tmp_path):
    """§1.2: «Такое значение уже есть: [ADR_1] … Объединить / Всё равно добавить отдельно / Отмена»."""
    app_mod, c, sid = _client(tmp_path)
    url = f'/api/sessions/{sid}/mappings'
    r = c.post(url, json={'original_form': 'г. Москва, ул. Лесная, д. 5', 'entity_type': 'АДРЕС'})
    tok = r.get_json()['token']
    r = c.post(url, json={'original_form': 'г.Москва, ул.Лесная, д.5', 'entity_type': 'АДРЕС'})
    assert r.status_code == 409 and r.get_json()['duplicate']['token'] == tok
    assert len(c.get(url).get_json()) == 1                       # «Отмена»: nothing added
    r = c.post(url, json={'original_form': 'г.Москва, ул.Лесная, д.5', 'entity_type': 'АДРЕС',
                          'on_duplicate': 'merge'})
    assert r.get_json()['token'] == tok
    r = c.post(url, json={'original_form': 'Москва, Лесная ул., дом 5', 'entity_type': 'АДРЕС',
                          'on_duplicate': 'separate'})
    other = r.get_json()['token']
    assert other != tok
    # the end-of-processing check must not merge what the user kept apart
    from core.db import consolidate_session
    assert consolidate_session(app_mod.DB_PATH, sid) == {}
    # a different house is not a duplicate
    r = c.post(url, json={'original_form': 'г. Москва, ул. Лесная, д. 7', 'entity_type': 'АДРЕС'})
    assert r.status_code == 201


def test_edit_warns_about_existing_value(tmp_path):
    app_mod, c, sid = _client(tmp_path)
    url = f'/api/sessions/{sid}/mappings'
    a = c.post(url, json={'original_form': 'Вектор Трейд', 'entity_type': 'ЮЛ'}).get_json()['token']
    b = c.post(url, json={'original_form': 'Северный Ветер', 'entity_type': 'ЮЛ'}).get_json()['token']
    r = c.patch(f'{url}/{b}', json={'original_form': 'ООО «ВЕКТОР ТРЕЙД»'})
    assert r.status_code == 409 and r.get_json()['duplicate']['token'] == a
    r = c.patch(f'{url}/{b}', json={'original_form': 'ООО «ВЕКТОР ТРЕЙД»', 'on_duplicate': 'merge'})
    assert r.get_json()['new_token'] == a


def test_distinct_pairs_api(tmp_path):
    app_mod, c, sid = _client(tmp_path)
    url = f'/api/sessions/{sid}/mappings'
    a = c.post(url, json={'original_form': 'Вектор Трейд', 'entity_type': 'ЮЛ'}).get_json()['token']
    b = c.post(url, json={'original_form': 'Вектор Тренд', 'entity_type': 'ЮЛ'}).get_json()['token']
    assert len(c.get(f'/api/sessions/{sid}/duplicates').get_json()) == 1
    c.post(f'/api/sessions/{sid}/distinct', json={'a': a, 'b': b})
    assert c.get(f'/api/sessions/{sid}/duplicates').get_json() == []
