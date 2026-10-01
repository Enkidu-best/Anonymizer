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
