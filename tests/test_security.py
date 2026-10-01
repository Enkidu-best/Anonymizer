"""Local server accepts requests only from the app itself (CLAUDE_CODE_TASK.md item 6)."""
import importlib
import os
import sys


def _client(tmp_path):
    os.environ['ANONYMIZER_DATA_DIR'] = str(tmp_path)
    sys.modules.pop('app', None)
    app_mod = importlib.import_module('app')
    return app_mod.app.test_client()


def test_foreign_origin_and_host_rejected(tmp_path):
    c = _client(tmp_path)
    assert c.post('/api/sessions', json={'name': 'x'},
                  headers={'Origin': 'https://evil.example'}).status_code == 403
    assert c.post('/api/sessions', json={'name': 'x'},
                  headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert c.get('/api/sessions', headers={'Host': 'evil.example'}).status_code == 403


def test_own_requests_allowed(tmp_path):
    c = _client(tmp_path)
    r = c.post('/api/sessions', json={'name': 'x'},
               headers={'Origin': 'http://localhost', 'Sec-Fetch-Site': 'same-origin'})
    assert r.status_code in (200, 201)
    assert c.get('/api/status').status_code == 200
