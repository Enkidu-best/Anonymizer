"""User settings («Настройки»), a small JSON file in the data folder.

org_quotes: 'always' — a company name restored into new text gets «» if it has none;
            'keep' — as stored.
org_opf:    'context' — the legal form is added when the name stands alone (no «ООО»,
            «компания»… on the left); 'always'; 'never'.
highlight_case: True — places where the case of a name was chosen automatically are
            highlighted yellow in the restored Word file.
pdf_to_word: True — a PDF with a text layer is turned into Word and processed as Word;
keep_pdf:   False — also save the anonymized PDF next to it.
"""
import json
import os
import threading
from pathlib import Path

DEFAULTS = {'org_quotes': 'always', 'org_opf': 'context', 'highlight_case': True,
            'pdf_to_word': True, 'keep_pdf': False}
CHOICES = {'org_quotes': ('always', 'keep'), 'org_opf': ('context', 'always', 'never'),
           'highlight_case': (True, False), 'pdf_to_word': (True, False), 'keep_pdf': (True, False)}
_lock = threading.Lock()
_path = None


def configure(data_dir):
    global _path
    _path = Path(data_dir) / 'settings.json'


def get() -> dict:
    out = dict(DEFAULTS)
    if _path and _path.exists():
        try:
            out.update({k: v for k, v in json.loads(_path.read_text(encoding='utf-8')).items()
                        if k in DEFAULTS and v in CHOICES[k]})
        except Exception:
            pass
    return out


def update(values: dict) -> dict:
    cur = get()
    cur.update({k: v for k, v in (values or {}).items() if k in DEFAULTS and v in CHOICES[k]})
    if _path:
        with _lock:
            tmp = _path.with_suffix('.tmp')
            tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, _path)
    return cur
