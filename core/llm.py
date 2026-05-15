"""
LLM entity extraction via Ollama (local, no internet).
Optional layer — used only when Ollama is running and user enables it.

Recommended models (install with: ollama pull <name>):
  qwen2.5:7b      — 7B, ~5 GB RAM, fastest, good multilingual (primary)
  mistral-nemo    — 12B, ~7 GB RAM, good Russian, balanced speed (fallback)
"""

import json
import re
import threading
import urllib.request
import urllib.error
from typing import List, Tuple

OLLAMA_URL   = 'http://localhost:11434'
DEFAULT_MODEL = 'qwen2.5:7b'

_lock        = threading.Lock()
_llm_model   = None
_llm_checked = False
_llm_available = False
_available_models: List[str] = []

_llm_progress = {'active': False, 'chunk': 0, 'total': 0, 'found': 0}

def get_llm_progress() -> dict:
    return dict(_llm_progress)


# ─────────────────────────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────────────────────────

def check_ollama() -> dict:
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
        if not _spawn('open', '-a', 'Ollama'):
            started = _spawn('ollama', 'serve')
        else:
            started = True

    else:
        started = _spawn('ollama', 'serve')

    if started:
        print('[LLM] Ollama start command sent, waiting 3 s...')
        time.sleep(3)
        result = check_ollama()
        print(f'[LLM] Ollama available: {result.get("available")}')
        return result.get('available', False)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Prompt
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — система поиска персональных данных в юридических документах.

В тексте уже заменены некоторые данные токенами вида [FIO_1], [INN_2] и т.д.
Найди ТОЛЬКО то, что ещё НЕ заменено токенами.

Ищи:
FIO — ФИО физлиц в любом падеже и форме:
  полное (Иванов Иван Иванович), сокращённое (Иванов И.И.),
  в подписях (/Иванов И.И./)

YUL — названия организаций БЕЗ ООО/ПАО/АО/ЗАО (их не трогать):
  только само название в кавычках

ADDR_PHYS — адреса физлиц:
  контекст: "зарегистрирован по адресу", "проживает", "место рождения"

ADDR_CORP — адреса юрлиц:
  контекст: "юридический адрес", "местонахождение"

PASSPORT — паспортные данные: серия, номер, кем выдан, код подразделения

DOB — даты рождения: "15 марта 1986 г.р.", "15.03.1986"

НЕ трогай: ИНН, ОГРН, КПП, р/с, к/с, БИК — уже обработаны.
НЕ трогай токены в квадратных скобках [TOKEN_N].

{patterns_section}

Верни ТОЛЬКО валидный JSON:
{{"entities": [{{"text": "точный текст из документа", "type": "FIO|YUL|ADDR_PHYS|ADDR_CORP|PASSPORT|DOB"}}]}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_patterns_section(user_patterns: list) -> str:
    if not user_patterns:
        return ""
    top = user_patterns[:20]
    lines = [f'- "{p["pattern"]}" → {p["entity_type"]}' for p in top]
    return "Дополнительные паттерны из документов пользователя:\n" + "\n".join(lines)


CHUNK_SIZE = 1500
OVERLAP    = 100

def _chunk_text(text):
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += CHUNK_SIZE - OVERLAP
    return chunks


def _ollama_generate(prompt: str, system: str,
                     timeout: int = 180) -> str:
    model = _llm_model or DEFAULT_MODEL
    body = json.dumps({
        'model':  model,
        'prompt': prompt,
        'system': system,
        'stream': False,
        'options': {'temperature': 0.0, 'num_predict': 1200},
    }).encode()

    req = urllib.request.Request(
        f'{OLLAMA_URL}/api/generate',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data.get('response', '')


# ─────────────────────────────────────────────────────────────────────────────
# Main LLM pass
# ─────────────────────────────────────────────────────────────────────────────

def apply_llm_pass(text: str, db_path, session_id: str,
                   user_patterns=None,
                   exclusions: set = None) -> Tuple[str, dict]:
    """
    CRITICAL: this function applies found entities to the text and returns
    the modified text + replacements dict.
    """
    from core.anonymizer import TOKEN_INNER_RE
    from core.db import get_or_create_token

    if not _llm_available:
        print('[LLM] Ollama not available — skipping LLM pass')
        return text, {}

    model = _llm_model or DEFAULT_MODEL
    print(f'[LLM] Starting LLM pass, model={model}')

    all_replacements = {}
    patterns_section = _build_patterns_section(user_patterns or [])

    chunks = _chunk_text(text)
    _llm_progress.update({'active': True, 'chunk': 0, 'total': len(chunks), 'found': 0})

    system_prompt = SYSTEM_PROMPT.format(patterns_section=patterns_section)

    for i, chunk in enumerate(chunks, 1):
        _llm_progress['chunk'] = i
        print(f'[LLM] Chunk {i}/{len(chunks)}...')

        token_chars = sum(len(m.group()) for m in TOKEN_INNER_RE.finditer(chunk))
        if len(chunk) > 0 and token_chars / len(chunk) > 0.80:
            print(f'[LLM] Chunk {i} mostly tokenized, skipping')
            continue

        try:
            raw_response = _ollama_generate(chunk, system_prompt, timeout=180)
            raw_response = re.sub(r'```(?:json)?|```', '', raw_response).strip()
            data = json.loads(raw_response)
        except Exception as e:
            print(f'[LLM] Parse error: {e}')
            continue

        for entity in data.get('entities', []):
            original = entity.get('text', '').strip()
            etype    = entity.get('type', 'FIO')

            if not original or len(original) < 2:
                continue
            if re.search(r'\[[A-Z_]+\d+\]', original):
                continue
            if original not in text:
                continue

            type_map = {
                'FIO': 'ФИО', 'YUL': 'ЮЛ',
                'ADDR_PHYS': 'АДРЕС', 'ADDR_CORP': 'АДРЕС',
                'PASSPORT': 'ПАСПОРТ', 'DOB': 'ДАТАРОЖД',
            }
            internal_type = type_map.get(etype, etype)

            if exclusions and (original, internal_type) in exclusions:
                continue

            token     = get_or_create_token(db_path, session_id,
                                             original, original, internal_type)
            bracketed = f'[{token}]'

            text = text.replace(original, bracketed)
            all_replacements[original] = bracketed
            print(f'[LLM] Entity: {repr(original[:60])} -> {bracketed}')

    _llm_progress.update({'active': False, 'found': len(all_replacements)})
    print(f'[LLM] Pass complete — {len(all_replacements)} new entities')
    return text, all_replacements


# ─────────────────────────────────────────────────────────────────────────────
# Pattern extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_and_save_patterns(db_path, session_id, original_text):
    from core.db import get_session_mappings, save_user_pattern

    CONTEXT_BEFORE = {
        'ФИО':     ['гражданка рф', 'гражданин рф', 'в лице', 'директора',
                     'подписал', 'именуемый', 'действующий', '/'],
        'ЮЛ':      ['между', 'с одной стороны', 'именуемое', 'цедент',
                     'цессионарий', 'заказчик', 'исполнитель'],
        'АДРЕС':   ['зарегистрирован', 'проживает', 'место рождения',
                     'по адресу', 'место жительства'],
        'ПАСПОРТ': ['паспорт', 'серия', 'выдан', 'код подразделения'],
    }
    mappings = get_session_mappings(db_path, session_id)
    saved = set()
    for m in mappings:
        etype = m['entity_type']
        if etype in CONTEXT_BEFORE:
            for keyword in CONTEXT_BEFORE[etype]:
                pattern = f'{keyword} [{etype}]'
                if pattern not in saved:
                    save_user_pattern(db_path, pattern, etype)
                    saved.add(pattern)


# ─────────────────────────────────────────────────────────────────────────────
# Chat command processing
# ─────────────────────────────────────────────────────────────────────────────

def process_chat_command(message: str, db_path, session_id: str) -> dict:
    from core.db import (get_session_mappings, get_or_create_token,
                         delete_mapping, update_mapping)

    if not _llm_available:
        return {'ok': False, 'message': 'Ollama недоступен'}

    model = _llm_model or DEFAULT_MODEL
    mappings = get_session_mappings(db_path, session_id)

    ctx_lines = [f'{m["token"]} = {m["original_form"]} ({m["entity_type"]})'
                 for m in (mappings or [])[:30]]
    ctx = '\n'.join(ctx_lines) if ctx_lines else '(маппинг пуст)'

    system = (
        'Ты — помощник системы анонимизации. Пользователь описывает действие с базой маппингов. '
        'Распознай намерение и верни ТОЛЬКО JSON без пояснений: '
        '{"action": "add|delete|update", "text": "...", "type": "ФИО|ЮЛ|ИНН|...", '
        '"token": "FIO_1", "canonical_form": "..."} '
        'Поля заполняй только те, что нужны для данного action. '
        'Если не можешь понять запрос — верни {"error": "не понял запрос"}.'
    )

    prompt = (
        f'СУЩЕСТВУЮЩИЙ МАППИНГ:\n{ctx}\n\n'
        f'ЗАПРОС ПОЛЬЗОВАТЕЛЯ: {message}'
    )

    try:
        raw = _ollama_generate(prompt, system, timeout=180)
        raw = re.sub(r'```(?:json)?|```', '', raw).strip()
        result = json.loads(raw)
    except Exception as ex:
        print(f'[LLM Chat] Parse error: {ex}')
        return {'ok': False, 'message': f'Ошибка разбора ответа LLM: {ex}'}

    if 'error' in result:
        return {'ok': False, 'message': result['error']}

    action = result.get('action')
    try:
        if action == 'add':
            canonical = (result.get('text') or result.get('canonical_form') or '').strip()
            entity_type = (result.get('type') or result.get('entity_type') or 'ЮЛ').strip()
            if not canonical:
                return {'ok': False, 'message': 'LLM не распознал значение'}
            token = get_or_create_token(db_path, session_id, canonical, canonical, entity_type)
            return {'ok': True, 'action': 'add', 'token': token,
                    'canonical_form': canonical, 'entity_type': entity_type,
                    'mappings': get_session_mappings(db_path, session_id)}

        elif action == 'delete':
            token = (result.get('token') or '').strip()
            if not token:
                return {'ok': False, 'message': 'LLM не распознал токен для удаления'}
            delete_mapping(db_path, session_id, token)
            return {'ok': True, 'action': 'delete', 'token': token,
                    'mappings': get_session_mappings(db_path, session_id)}

        elif action == 'update':
            token = (result.get('token') or '').strip()
            canonical = (result.get('canonical_form') or result.get('text') or '').strip()
            entity_type = (result.get('type') or result.get('entity_type') or '').strip()
            if not token:
                return {'ok': False, 'message': 'LLM не распознал токен'}
            update_mapping(db_path, session_id, token,
                           {'canonical_form': canonical, 'entity_type': entity_type})
            return {'ok': True, 'action': 'update', 'token': token,
                    'mappings': get_session_mappings(db_path, session_id)}

        else:
            return {'ok': False, 'message': f'Неизвестное действие: {action}'}

    except Exception as ex:
        return {'ok': False, 'message': str(ex)}
