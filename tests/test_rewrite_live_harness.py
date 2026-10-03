"""The live «memo» check (tests/e2e/rewrite.py) with a fake LLM: the harness itself works
and catches a name that is not a form of a session person."""
import importlib
import os
import sys

from tests.fixtures import generator


def _client(tmp_path):
    os.environ['ANONYMIZER_DATA_DIR'] = str(tmp_path / 'data')
    sys.modules.pop('app', None)
    app_mod = importlib.import_module('app')
    import core.anonymizer as A
    A._ner_ready = False
    from tests.e2e.runner import Client
    return Client(app_mod.app)


def test_memo_harness(tmp_path, monkeypatch):
    import core.anonymizer as A
    monkeypatch.setattr(A, '_ner_ready', False)
    client = _client(tmp_path)
    src = tmp_path / 'c.docx'
    generator.make_docx(src)
    from tests.e2e.rewrite import run_rewrite

    def write(text):
        import re
        toks = re.findall(r'\[(FIO_\d+|YUL_\d+)\]', text)
        fio = next(t for t in toks if t.startswith('FIO'))
        yul = next(t for t in toks if t.startswith('YUL'))
        return f'Служебная записка\nУведомление направить [{fio}].\nРиск: [{yul}] не оплатит.\nСм. [FIO_77].'
    rep = run_rewrite(client, src, tmp_path / 'w', write)
    assert rep['errors'] == [], rep
    assert rep['names'] == 1 and rep['orgs'] == 1 and rep['invented'] == 1
