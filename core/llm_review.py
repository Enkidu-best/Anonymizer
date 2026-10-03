"""The local LLM as a reviewer of the rule layers (task 2, §1.5, §3).

Instead of reading the whole document in 3 000-char chunks (minutes), short calls:

review_list — every found person / company / address with one line of context, plus only
«suspicious» lines (not masked, capitalized words that are not roles/positions, quotes,
long digit groups — 5-15 % of a contract) in ONE request. The answer is only the changes
by item number {"drop": [3], "merge": [[1, 4]], "add": [...]} — tens of tokens, not one
object per item (task 3, §3). The LLM may only propose to unmask persons, companies,
addresses; numbers with a checksum are never touched.

The result is a proposal {add: [...], remove: [...], merge: [...]}; the user applies it.
"""
import json
import re
import time

from core import llm, log
from core.lexicon import is_role_or_position, is_public_body

REVIEW_TYPES = ('ФИО', 'ЮЛ', 'АДРЕС')

REVIEW_SYSTEM = """Ты проверяешь российский юридический документ после автоматического поиска персональных данных.
Раздел НАЙДЕНО — пронумерованные значения с контекстом. Раздел СТРОКИ — строки, где могли что-то пропустить
(уже заменённые метки вида [FIO_1], [YUL_2] не трогай).
Верни ТОЛЬКО изменения, без повторения текста:
drop — номера пунктов НАЙДЕНО, которые не являются ФИО конкретного человека, названием конкретной организации
или адресом конкретного объекта: роль стороны (Арендатор, Покупатель, Сторона), должность, заголовок, обычные
слова, закон, госорган, суд, публичный реестр;
merge — пары номеров [a, b], если это одно и то же лицо, компания или адрес в другой форме;
add — пропущенные в разделе СТРОКИ значения: ФИО людей (в любом падеже, с инициалами), названия конкретных
организаций (без ООО и кавычек), адреса, номера документов и реквизиты; текст копируй точно как в строке.
Если менять нечего — пустые списки."""

ADD_TYPES = ['FIO', 'YUL', 'ADDR', 'PASSPORT', 'PHONE', 'EMAIL', 'INN', 'OGRN', 'ACCOUNT', 'DOB']
# the answer is only the changes by item number: 10-40 tokens instead of one object per item
REVIEW_SCHEMA = {
    'type': 'object',
    'properties': {
        'drop': {'type': 'array', 'items': {'type': 'integer'}},
        'merge': {'type': 'array', 'items': {'type': 'array', 'items': {'type': 'integer'},
                                             'minItems': 2, 'maxItems': 2}},
        'add': {'type': 'array', 'items': {'type': 'object',
                                           'properties': {'text': {'type': 'string'},
                                                          'type': {'type': 'string', 'enum': ADD_TYPES}},
                                           'required': ['text', 'type']}},
    },
    'required': ['drop', 'merge', 'add'],
}
NOT_PII = {'ROLE', 'POSITION', 'HEADING', 'LAW', 'PUBLIC'}


def _safe_to_unmask(value: str, etype: str) -> bool:
    """The LLM may be wrong; never propose to unmask something that looks like a name
    by morphology, or an address with a house number."""
    from core.lexicon import name_like, _WORD
    if etype == 'АДРЕС':
        return not re.search(r'\d', value)
    words = [w for w in _WORD.findall(value) if len(w) > 1]
    if etype == 'ФИО':
        return bool(words) and not any(name_like(w) for w in words)
    if etype == 'ЮЛ':
        return is_role_or_position(value) or is_public_body(value) or \
            (bool(words) and all(not name_like(w) for w in words) and value.islower())
    return False


def _context_line(text: str, value: str, width: int = 160) -> str:
    i = text.find(value)
    if i < 0:
        return ''
    a = text.rfind('\n', 0, i) + 1
    b = text.find('\n', i)
    b = len(text) if b < 0 else b
    line = text[a:b]
    if len(line) > width:
        k = i - a
        line = line[max(0, k - width // 2):k + len(value) + width // 2]
    return re.sub(r'\s+', ' ', line).strip()


def _category(value: str) -> str:
    """Why a value is not personal data — for the user, from our own lexicon."""
    if is_role_or_position(value):
        return 'ROLE'
    if is_public_body(value):
        return 'PUBLIC'
    return 'HEADING'


def _entries(original_text: str, mappings: list) -> list:
    entries, seen = [], set()
    for m in mappings:
        if m['entity_type'] not in REVIEW_TYPES or m['token'] in seen:
            continue
        seen.add(m['token'])
        entries.append({'token': m['token'], 'type': m['entity_type'], 'value': m['original_form'],
                        'context': _context_line(original_text, m['original_form'])})
    return entries


def _blocks(masked_text: str, block_chars: int) -> list:
    blocks, cur = [], ''
    for l in suspicious_lines(masked_text):
        if len(cur) + len(l) > block_chars and cur:
            blocks.append(cur)
            cur = ''
        cur += l + '\n'
    if cur:
        blocks.append(cur)
    return blocks


def ask(part: list, lines: str) -> dict:
    """One request: a batch of found values and a block of suspicious lines → changes."""
    found = '\n'.join(f'{i}. «{e["value"]}» — контекст: {e["context"] or "-"}' for i, e in enumerate(part, 1))
    prompt = f'НАЙДЕНО:\n{found or "-"}\n\nСТРОКИ:\n{lines.strip() or "-"}'
    raw = llm._ollama_generate(prompt, REVIEW_SYSTEM, timeout=120, schema=REVIEW_SCHEMA, num_predict=400)
    data = json.loads(raw)
    return {'drop': [i for i in data.get('drop') or [] if isinstance(i, int)],
            'merge': [p for p in data.get('merge') or [] if isinstance(p, list) and len(p) == 2
                      and all(isinstance(x, int) for x in p)],
            'add': [a for a in data.get('add') or [] if isinstance(a, dict)]}


def review_list(original_text: str, masked_text: str, mappings: list, batch: int = 40,
                deadline: float = None, block_chars: int = 4000) -> dict:
    """Found persons / companies / addresses in batches of `batch` plus suspicious lines;
    the first requests carry both, extra blocks of lines go alone (at most 3 blocks)."""
    entries = _entries(original_text, mappings)
    blocks = _blocks(masked_text, block_chars)[:3]
    parts = [entries[i:i + batch] for i in range(0, len(entries), batch)]
    n = max(len(parts), len(blocks))
    remove, merge, added, errors = [], [], [], 0
    for k in range(n):
        if deadline and time.time() > deadline:
            errors += 1
            break
        part = parts[k] if k < len(parts) else []
        try:
            ans = ask(part, blocks[k] if k < len(blocks) else '')
        except Exception as ex:
            log.error('llm_review_batch', ex, batch=k)
            errors += 1
            continue
        for i in ans['drop']:
            if 1 <= i <= len(part):
                e = part[i - 1]
                if _safe_to_unmask(e['value'], e['type']):
                    remove.append({'token': e['token'], 'type': e['type'], 'category': _category(e['value'])})
        for i, j in ans['merge']:
            if 1 <= i <= len(part) and 1 <= j <= len(part) and i != j:
                a, b = part[i - 1], part[j - 1]
                if a['type'] == b['type'] and a['token'] != b['token'] and _plausible_same(a, b):
                    merge.append({'token': a['token'], 'into': b['token']})
        for e in ans['add']:
            t = (e.get('text') or '').strip()
            if len(t) >= 3 and t in masked_text and not re.search(r'\[[A-Z]+_\d+', t):
                added.append({'text': t, 'type': e.get('type', 'FIO')})
    # A→B and B→A are one proposal; never merge into something proposed for removal
    gone = {r['token'] for r in remove}
    uniq, pairs = [], set()
    for mg in merge:
        key = frozenset((mg['token'], mg['into']))
        if key in pairs or mg['token'] in gone or mg['into'] in gone:
            continue
        pairs.add(key)
        uniq.append(mg)
    return {'remove': remove, 'merge': uniq, 'missed': added, 'checked': len(entries), 'errors': errors}


def _plausible_same(a: dict, b: dict) -> bool:
    """The LLM sometimes calls two different people «the same» — for persons the surname
    must match and initials agree; companies/addresses are shown to the user as is."""
    if a['type'] != 'ФИО':
        return True
    from core.entities import Person
    pa, pb = Person.parse(a['value']), Person.parse(b['value'])
    return bool(pa.surname and pa.surname == pb.surname and pa.compatible(pb))


# a quoted name, two+ capitalized words in a row (not at a sentence start), a surname with
# initials, a long unmasked number
_QUOTED = re.compile(r'«([^»\[]{2,60})»')
_CAPS = re.compile(r'(?<=[a-zа-яё,;:)] )(?:[A-ZА-ЯЁ][a-zа-яё]{2,}(?:[^\S\n]+[A-ZА-ЯЁ][a-zа-яё]+)+)')
_INIT = re.compile(r'(?<![\[\w])[А-ЯЁ][а-яё]{2,}[^\S\n]+[А-ЯЁ]\.[^\S\n]?[А-ЯЁ]\.|[А-ЯЁ]\.[^\S\n]?[А-ЯЁ]\.[^\S\n]?[А-ЯЁ][а-яё]{2,}')
_NUM = re.compile(r'(?<![\d\[_])\d[\d \-]{5,}\d(?![\d\]])')


def suspicious_lines(masked_text: str) -> list:
    """Lines that may hide something the rules missed — a small part of a contract."""
    out = []
    for line in masked_text.split('\n'):
        s = line.strip()
        if len(s) < 6:
            continue
        cand = [m.group(1) for m in _QUOTED.finditer(s)] + [m.group() for m in _CAPS.finditer(s)] + \
            [m.group() for m in _INIT.finditer(s)]
        cand = [c for c in cand if not (is_role_or_position(c) or is_public_body(c) or
                                        re.fullmatch(r'[\d\s.,%/-]+', c))]
        words = re.findall(r'[A-Za-zА-ЯЁа-яё]{3,}', s)
        nums = [n for n in _NUM.findall(s) if not re.fullmatch(r'\d{1,3}(?: \d{3})+', n.strip())] if words else []
        if cand or nums:
            out.append(s)
    return out


_TYPE = {'FIO': 'ФИО', 'YUL': 'ЮЛ', 'ADDR': 'АДРЕС', 'ADDR_PHYS': 'АДРЕС', 'ADDR_CORP': 'АДРЕС',
         'PASSPORT': 'ПАСПОРТ', 'DOB': 'ДАТАРОЖД', 'PHONE': 'ТЕЛЕФОН', 'PHONE_NUMBER': 'ТЕЛЕФОН',
         'TEL': 'ТЕЛЕФОН', 'EMAIL': 'EMAIL', 'INN': 'ИНН', 'OGRN': 'ОГРН', 'ACCOUNT': 'РС'}


def review(original_text: str, masked_text: str, mappings: list, budget_s: float = 150) -> dict:
    """Both steps; returns a proposal for the user and timings."""
    from core.lexicon import not_pii
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
    t0 = time.time()
    llm.check_ollama()
    deadline = t0 + budget_s
    # one worker: Ollama serves requests one by one anyway; the hard limit is waiting for
    # the result, not the socket timeout — a stuck Ollama never hangs the review
    ex = ThreadPoolExecutor(1)
    r = {'remove': [], 'merge': [], 'checked': 0, 'errors': 0}
    missed, t1 = [], t0
    try:
        with log.stage('llm_review'):
            f = ex.submit(review_list, original_text, masked_text, mappings, 40, deadline)
            try:
                r = f.result(timeout=max(1, deadline - time.time()))
            except _Timeout:
                r['errors'] = 1
                log.event('llm_review_timeout', step='list', budget_s=budget_s)
            missed = r.get('missed', [])
            t1 = time.time()
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    known = {m['original_form'] for m in mappings}
    add = []
    for e in missed:
        etype = _TYPE.get(str(e['type']).upper())
        if etype is None or e['text'] in known or not_pii(e['text'], etype):
            continue   # unknown types («date») and roles are never added
        if etype in ('ТЕЛЕФОН', 'ИНН', 'ОГРН', 'РС') and len(re.sub(r'\D', '', e['text'])) < 7:
            continue
        if etype in ('ДАТАРОЖД',) and not llm._birth_context(original_text, e['text']):
            continue
        add.append({'value': e['text'], 'type': etype})
    out = {'add': add, 'remove': r['remove'], 'merge': r['merge'], 'checked': r['checked'],
           'partial': bool(r.get('errors')) or time.time() > deadline,
           'seconds': {'list': round(t1 - t0, 1), 'missed': round(time.time() - t1, 1)}}
    log.event('llm_review', add=len(add), remove=len(r['remove']), merge=len(r['merge']),
              checked=r['checked'], seconds=out['seconds'])
    return out
