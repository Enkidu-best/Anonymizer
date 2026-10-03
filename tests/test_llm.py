"""LLM layer with a mocked Ollama: request shape (no thinking, JSON schema) and bounded replace."""
import io
import json

import core.llm as llm
from core.anonymizer import anonymize_text_pipeline


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_llm_request_and_replace(monkeypatch, tmp_db, session_id):
    seen = {}

    def fake_urlopen(req, timeout=0):
        if not req.full_url.endswith('/api/generate'):
            return _Resp(json.dumps({'models': []}).encode())
        body = json.loads(req.data)
        seen.update(body)
        reply = {'entities': [{'text': 'Тарханов', 'type': 'FIO'}]}
        return _Resp(json.dumps({'response': json.dumps(reply, ensure_ascii=False)}).encode())

    monkeypatch.setattr(llm.urllib.request, 'urlopen', fake_urlopen)
    monkeypatch.setattr(llm, '_llm_available', True)
    text = 'Свидетель Тарханов, а также Тархановский завод.'
    out, _ = anonymize_text_pipeline(text, tmp_db, session_id, use_spacy=False, use_llm=True)
    assert seen['think'] is False
    assert seen['format']['required'] == ['entities']
    assert seen['options']['num_predict'] >= 2048
    assert 'Тарханов,' not in out and 'Тархановский завод' in out


def test_review_never_hangs_on_a_stuck_ollama(monkeypatch):
    """A stuck Ollama (it happened: 0.5 tok/s while swapping) must not hang the review:
    the hard budget returns a partial result in time, the file stays ready."""
    import time as _t
    from core import llm_review
    monkeypatch.setattr(llm, 'check_ollama', lambda: {'available': True})
    monkeypatch.setattr(llm, '_ollama_generate', lambda *a, **k: (_t.sleep(5), '{"drop": [], "merge": [], "add": []}')[1])
    maps = [{'token': 'FIO_1', 'entity_type': 'ФИО', 'original_form': 'Тарханов Глеб'}]
    t0 = _t.time()
    out = llm_review.review('Тарханов Глеб подписал.', '[FIO_1] подписал.', maps, budget_s=1)
    assert _t.time() - t0 < 2.5
    assert out['partial']


def test_review_short_answer_is_filtered(monkeypatch):
    """Task 3, §3: the LLM answers only changes by number; unsafe ones are dropped:
    a name is never unmasked, two different people are never merged."""
    import json as _j
    from core import llm_review
    seen = {}

    def fake(prompt, system, timeout=90, schema=None, num_ctx=None, num_predict=2048):
        seen['num_predict'] = num_predict
        seen['schema'] = schema
        return _j.dumps({'drop': [1, 2], 'merge': [[2, 3], [2, 4]], 'add': [{'text': 'Агафонов', 'type': 'FIO'}]})
    monkeypatch.setattr(llm, 'check_ollama', lambda: {'available': True})
    monkeypatch.setattr(llm, '_ollama_generate', fake)
    maps = [{'token': 'FIO_1', 'entity_type': 'ФИО', 'original_form': 'Арендатор'},
            {'token': 'FIO_2', 'entity_type': 'ФИО', 'original_form': 'Тарханов Глеб Игоревич'},
            {'token': 'FIO_3', 'entity_type': 'ФИО', 'original_form': 'Тарханову Г.И.'},
            {'token': 'FIO_4', 'entity_type': 'ФИО', 'original_form': 'Ракитина Валентина'}]
    orig = 'Арендатор Тарханов Глеб Игоревич; Тарханову Г.И.; Ракитина Валентина; Агафонов «Соседи» тоже.'
    masked = '[FIO_1] [FIO_2]; [FIO_3]; [FIO_4]; Агафонов «Соседи» тоже.'
    out = llm_review.review(orig, masked, maps, budget_s=10)
    assert [r['token'] for r in out['remove']] == ['FIO_1']
    assert out['merge'] == [{'token': 'FIO_2', 'into': 'FIO_3'}]
    assert out['add'] == [{'value': 'Агафонов', 'type': 'ФИО'}]
    assert seen['num_predict'] <= 700 and 'drop' in seen['schema']['properties']


def test_llm_never_unmasks_a_real_address_part():
    from core.llm_review import _safe_to_unmask
    assert _safe_to_unmask('Общая площадь кв.м', 'АДРЕС')
    assert not _safe_to_unmask('Тверская область, Конаковский район', 'АДРЕС')
    assert not _safe_to_unmask('г. Тверь, ул. Озёрная', 'АДРЕС')


def test_llm_additions_of_public_bodies_typed_as_persons_dropped(monkeypatch):
    import json as _j
    from core import llm_review
    monkeypatch.setattr(llm, 'check_ollama', lambda: {'available': True})
    monkeypatch.setattr(llm, '_ollama_generate', lambda *a, **k: _j.dumps({'drop': [], 'merge': [], 'add': [
        {'text': 'Федеральная служба безопасности Российской Федерации', 'type': 'FIO'},
        {'text': 'Государственная фельдъегерская служба Российской Федерации', 'type': 'FIO'},
        {'text': 'Агафонов', 'type': 'FIO'}]}))
    masked = ('Федеральная служба безопасности Российской Федерации и Государственная фельдъегерская служба '
              'Российской Федерации; Агафонов «Соседи» тоже.')
    out = llm_review.review(masked, masked, [], budget_s=10)
    assert [a['value'] for a in out['add']] == ['Агафонов']
