"""
LLM entity extraction via Ollama (local, no internet).
Optional layer — used only when Ollama is running and user enables it.

Recommended models (install with: ollama pull <name>):
  mistral-nemo    — 12B, ~7 GB RAM, good Russian, balanced speed
  mistral-small3.2 — 24B, ~15 GB RAM, best quality
  qwen2.5:7b      — 7B, ~5 GB RAM, fastest, good multilingual
"""

import json
import re
import threading
import urllib.request
import urllib.error
from typing import List, Tuple

OLLAMA_URL   = 'http://localhost:11434'
DEFAULT_MODEL = 'mistral-nemo'

_lock        = threading.Lock()
_llm_model   = None   # currently selected model
_llm_checked = False
_llm_available = False
_available_models: List[str] = []

# ── Progress state (polled by /api/llm-progress) ──────────────────────────────
_llm_progress = {'active': False, 'chunk': 0, 'total': 0, 'found': 0}

def get_llm_progress() -> dict:
    return dict(_llm_progress)


# ─────────────────────────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────────────────────────

def check_ollama() -> dict:
    """Check if Ollama is running and return available models."""
    global _llm_checked, _llm_available, _available_models

    try:
        req = urllib.request.Request(f'{OLLAMA_URL}/api/tags', method='GET')
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        models = [m['name'] for m in data.get('models', [])]
        _llm_available  = True
        _available_models = models
    except Exception:
        _llm_available  = False
        _available_models = []

    _llm_checked = True
    return {
        'available': _llm_available,
        'models':    _available_models,
        'model':     _llm_model,
    }


def get_llm_status() -> dict:
    return {
        'available': _llm_available,
        'models':    _available_models,
        'model':     _llm_model,
        'checked':   _llm_checked,
    }


def set_model(model: str):
    global _llm_model
    _llm_model = model


def try_start_ollama() -> bool:
    """
    Try to start the Ollama daemon if it is not running.
    Works on Windows, macOS, and Linux.
    Returns True if Ollama becomes available after the attempt.
    """
    import subprocess
    import platform
    import time
    import os

    system = platform.system()
    creationflags = 0
    if system == 'Windows':
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)

    def _spawn(*args):
        try:
            subprocess.Popen(
                list(args),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return True
        except FileNotFoundError:
            return False
        except Exception:
            return False

    started = False
    if system == 'Windows':
        local_app = os.environ.get('LOCALAPPDATA', '')
        candidates = [
            os.path.join(local_app, 'Programs', 'Ollama', 'ollama.exe'),
            r'C:\Program Files\Ollama\ollama.exe',
        ]
        for path in candidates:
            if os.path.exists(path):
                started = _spawn(path, 'serve')
                break
        if not started:
            started = _spawn('ollama', 'serve')

    elif system == 'Darwin':
        # Try GUI app first (it starts the server automatically)
        if not _spawn('open', '-a', 'Ollama'):
            started = _spawn('ollama', 'serve')
        else:
            started = True

    else:  # Linux / other
        started = _spawn('ollama', 'serve')

    if started:
        print('[LLM] Ollama start command sent, waiting 3 s...')
        time.sleep(3)
        result = check_ollama()
        print(f'[LLM] Ollama available: {result.get("available")}')
        return result.get('available', False)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Entity extraction via LLM
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM = (
    'Ты — система анонимизации персональных данных. '
    'Твоя задача — найти в юридическом тексте персональные данные и реквизиты, '
    'которые НЕ являются ИНН, ОГРН, КПП, расчётными/корреспондентскими счетами, '
    'БИК, СНИЛС, телефонами и email (они обрабатываются отдельно). '
    'Найди: ФИО физических лиц (все падежи), названия организаций без ОПФ, '
    'даты рождения, адреса проживания/регистрации физических лиц, '
    'паспортные данные. '
    'Верни ТОЛЬКО JSON без пояснений: '
    '{"entities": [{"text": "<точный текст из документа>", "type": "ФИО|ЮЛ|АДРЕС|ДАТАРОЖД|ПАСПОРТ"}]}'
)


def _ollama_generate(prompt: str, model: str) -> str:
    body = json.dumps({
        'model':  model,
        'prompt': prompt,
        'system': _SYSTEM,
        'stream': False,
        'options': {'temperature': 0.0, 'num_predict': 800},
    }).encode()

    req = urllib.request.Request(
        f'{OLLAMA_URL}/api/generate',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data.get('response', '')


def extract_entities_llm(text: str, existing_spans: List[Tuple[int, int]]) -> list:
    """
    Call Ollama to extract entities not caught by regex/Natasha.
    Returns list of Entity objects with approximate span positions.
    """
    from core.anonymizer import Entity

    model = _llm_model or DEFAULT_MODEL
    if not _llm_available:
        return []

    # Chunk long texts (LLM context window)
    CHUNK = 3000
    chunks = [text[i:i+CHUNK] for i in range(0, len(text), CHUNK)]

    all_entities = []
    for chunk in chunks:
        try:
            raw = _ollama_generate(chunk, model)
            # Strip markdown fences if model adds them
            raw = re.sub(r'```(?:json)?|```', '', raw).strip()
            data = json.loads(raw)
        except Exception:
            continue

        for item in data.get('entities', []):
            ent_text = item.get('text', '').strip()
            ent_type = item.get('type', 'ФИО')
            if not ent_text or len(ent_text) < 2:
                continue

            # Find actual position in the full text
            idx = text.find(ent_text)
            if idx == -1:
                continue
            s, e = idx, idx + len(ent_text)

            # Skip if overlaps with existing span
            if any(not (e <= us or s >= ue) for us, ue in existing_spans):
                continue

            all_entities.append(Entity(ent_text, ent_text, ent_type, s, e))
            existing_spans.append((s, e))

    return all_entities


# ─────────────────────────────────────────────────────────────────────────────
# Public API called from anonymizer.py
# ─────────────────────────────────────────────────────────────────────────────

def apply_llm_pass(text: str, db_path, session_id: str):
    """
    Run LLM entity extraction as final anonymization pass.
    Returns (anonymized_text, replacements_dict).
    Called once per document from handlers (NOT per paragraph).
    Progress is tracked in _llm_progress for the /api/llm-progress endpoint.
    """
    from core.anonymizer import TOKEN_INNER_RE, _is_bracketed_token, _wrap
    from core.db import get_or_create_token

    if not _llm_available:
        print('[LLM] Ollama not available — skipping LLM pass')
        return text, {}

    model = _llm_model or DEFAULT_MODEL
    print(f'[LLM] Starting LLM pass, model={model}')

    # Larger chunks = fewer API calls = faster processing
    CHUNK = 4000
    chunks = [text[i:i+CHUNK] for i in range(0, len(text), CHUNK)]

    _llm_progress.update({'active': True, 'chunk': 0, 'total': len(chunks), 'found': 0})

    existing_spans = [(m.start(), m.end()) for m in TOKEN_INNER_RE.finditer(text)]
    entities = []

    for i, chunk in enumerate(chunks, 1):
        _llm_progress['chunk'] = i
        print(f'[LLM] Chunk {i}/{len(chunks)}…')

        # Skip chunks that are already almost entirely tokenized (>80% tokens)
        token_chars = sum(len(m.group()) for m in TOKEN_INNER_RE.finditer(chunk))
        if len(chunk) > 0 and token_chars / len(chunk) > 0.80:
            print(f'[LLM] Chunk {i} mostly tokenized, skipping')
            continue

        try:
            raw = _ollama_generate(chunk, model)
            raw = re.sub(r'```(?:json)?|```', '', raw).strip()
            data = json.loads(raw)
        except Exception as ex:
            print(f'[LLM] Chunk {i} parse error: {ex}')
            continue

        for item in data.get('entities', []):
            ent_text = item.get('text', '').strip()
            ent_type = item.get('type', 'ФИО')
            if not ent_text or len(ent_text) < 2:
                continue
            idx = text.find(ent_text)
            if idx == -1:
                continue
            s, e = idx, idx + len(ent_text)
            if any(not (e <= us or s >= ue) for us, ue in existing_spans):
                continue
            from core.anonymizer import Entity
            entities.append(Entity(ent_text, ent_text, ent_type, s, e))
            existing_spans.append((s, e))

    _llm_progress.update({'active': False, 'found': len(entities)})

    if not entities:
        print('[LLM] No additional entities found')
        return text, {}

    replacements = {}
    for ent in entities:
        if ent.original not in replacements and not _is_bracketed_token(ent.original):
            token = _wrap(get_or_create_token(db_path, session_id, ent.canonical, ent.entity_type))
            replacements[ent.original] = token
            print(f'[LLM] Entity: {repr(ent.original[:60])} → {token}')

    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)

    print(f'[LLM] Pass complete — {len(replacements)} new entities')
    return text, replacements
