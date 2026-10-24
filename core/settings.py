"""User settings («Настройки»), a small JSON file in the data folder.

org_quotes: 'always' — a company name restored into new text gets «» if it has none;
            'keep' — as stored.
org_opf:    'context' — the legal form is added when the name stands alone (no «ООО»,
            «компания»… on the left); 'always'; 'never'.
highlight_case: True — places where the case of a name was chosen automatically are
            highlighted yellow in the restored Word file.
pdf_to_word: True — a PDF with a text layer is turned into Word and processed as Word;
keep_pdf:   False — also save the anonymized PDF next to it.
retention_days: 30 — a session is deleted whole after 7 / 30 / 90 days (description 3.9, 3.12).
use_llm:    False — the local neural network checks what was found and chooses the case in doubtful places
            on restore (one switch for both, description 3.12 and question 2).
llm_model:  the Ollama model chosen in «Настройки» (kept between runs, was Р-43); '' - the default one.
Internal keys (not shown in «Настройки»): dict_migrated - values moved from the memory before 4.0,
dict_note_hidden - the line «Перенесено N значений…» was closed.
"""
import json
import os
import threading
from pathlib import Path

DEFAULTS = {'org_quotes': 'always', 'org_opf': 'context', 'highlight_case': True,
            'pdf_to_word': True, 'keep_pdf': False, 'retention_days': 30, 'use_llm': False, 'llm_model': '',
            'dict_migrated': 0, 'dict_note_hidden': False}
CHOICES = {'org_quotes': ('always', 'keep'), 'org_opf': ('context', 'always', 'never'),
           'highlight_case': (True, False), 'pdf_to_word': (True, False), 'keep_pdf': (True, False),
           'retention_days': (7, 30, 90), 'use_llm': (True, False), 'dict_note_hidden': (True, False)}


def _valid(k, v) -> bool:
    if k == 'llm_model':
        return isinstance(v, str) and len(v) <= 100
    if k == 'dict_migrated':
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0
    return k in CHOICES and v in CHOICES[k] and type(v) is type(CHOICES[k][0])
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
                        if k in DEFAULTS and _valid(k, v)})
        except Exception:
            pass
    return out


def update(values: dict) -> dict:
    cur = get()
    cur.update({k: v for k, v in (values or {}).items() if k in DEFAULTS and _valid(k, v)})
    if _path:
        with _lock:
            tmp = _path.with_suffix('.tmp')
            tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, _path)
    return cur
