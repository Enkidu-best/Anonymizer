"""End-to-end on generated documents (shared set, runs in pytest).

The same scenario as scripts/e2e_private.py runs on the owner's real documents.
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

from tests.fixtures.generator import make_all, MUST_MASK, MUST_KEEP
from tests.e2e.runner import Client, run_document, _present
from tests.e2e.independent import places, norm

_WORK = Path(tempfile.mkdtemp(prefix='anon_e2e_'))
_LOGS = _WORK / 'logs'
FILES = make_all(_WORK / 'docs')


@pytest.fixture(scope='module')
def client():
    os.environ['ANONYMIZER_DATA_DIR'] = str(_WORK / 'data')
    os.environ['ANONYMIZER_LOG_DIR'] = str(_LOGS)
    sys.modules.pop('app', None)
    from core import log
    log.LOG_DIR = None
    for h in list(log._log.handlers):
        log._log.removeHandler(h)
    app = importlib.import_module('app')
    return Client(app.app)


@pytest.mark.parametrize('src', FILES, ids=lambda p: p.suffix)
def test_document_end_to_end(client, src):
    rep = run_document(client, src, _WORK / 'out' / src.stem, spacy=True, log_dir=_LOGS)
    errors = rep['errors'] + rep.get('errors_llm', []) + rep.get('errors_edits', [])
    assert not errors, f'{src.suffix}: {errors} (found {rep["found"]})'
    assert rep['found'], 'nothing found'


@pytest.mark.parametrize('src', FILES, ids=lambda p: p.suffix)
def test_ground_truth(client, src):
    """Against the known answer: every fictional value is gone, roles and term dates stay."""
    sid = client.session('truth')
    r = client.process(sid, src, spacy=True)
    out = client.download(sid, r['output'], _WORK / 'truth' / (src.stem + Path(r['output']).suffix))
    got = places(out)
    text = norm('\n'.join(got['body'] + got['meta'] + got.get('ocr', []) + [r['output']]))
    plain = text.replace('\xad', '')
    leaked = [i for i, v in enumerate(MUST_MASK) if _present(plain, v) or _present(text, v)]
    source = norm('\n'.join(places(src)['body']))
    lost = [k for k in MUST_KEEP if k in source and k not in text]
    assert not leaked, f'{src.suffix}: leaked MUST_MASK items #{leaked}'
    assert not lost, f'{src.suffix}: over-masked {lost}'
