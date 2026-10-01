"""The local LLM as a reviewer of the rule layers (task 2, §1.5, §3).

Instead of reading the whole document in 3 000-char chunks (minutes), two short calls:

1. review_list — every found person / company / address with one line of context:
   is it personal data, which type, a duplicate of which item. Tens of short lines →
   seconds. The LLM may only propose to unmask persons, companies, addresses; numbers
   with a checksum are never touched.
2. find_missed — only «suspicious» lines: not masked, with capitalized words that are
   not roles/positions, quotes or long digit groups (5-15 % of a contract).

The result is a proposal {add: [...], remove: [...], merge: [...]}; the user applies it.
"""
import json
import re
import time

from core import llm, log
from core.lexicon import is_role_or_position, is_public_body

REVIEW_TYPES = ('ФИО', 'ЮЛ', 'АДРЕС')

REVIEW_SYSTEM = """Ты проверяешь значения, найденные автоматически в российском юридическом документе.
Для КАЖДОГО пункта выбери категорию:
PERSON — фамилия/имя/отчество конкретного человека (в любом падеже, с инициалами);
COMPANY — название конкретной организации;
ADDRESS — адрес конкретного объекта;
ROLE — роль стороны договора (Арендатор, Покупатель, Сторона, Поручитель…);
POSITION — должность (Генеральный директор, Председатель…);
HEADING — заголовок раздела или обычные слова;
LAW — название закона, кодекса, нормативного акта;
PUBLIC — госорган, суд, Банк России, публичный реестр.
same_as — номер пункта, если это то же лицо/компания/адрес в другой форме, иначе null.
Строго JSON по схеме, по одному объекту на каждый пункт."""

REVIEW_SCHEMA = {
    'type': 'object',
    'properties': {'items': {'type': 'array', 'items': {
        'type': 'object',
        'properties': {'id': {'type': 'integer'},
                       'category': {'type': 'string', 'enum': ['PERSON', 'COMPANY', 'ADDRESS', 'ROLE',
                                                               'POSITION', 'HEADING', 'LAW', 'PUBLIC']},
                       'same_as': {'type': ['integer', 'null']}},
        'required': ['id', 'category']}}},
    'required': ['items'],
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


MISSED_SYSTEM = """Ты ищешь персональные данные, которые пропустила автоматическая проверка
в российском юридическом документе. Данные, уже заменённые метками вида [FIO_1], [YUL_2],
не трогай. Перечисли ТОЛЬКО незамаскированные: ФИО людей (в любом падеже, с инициалами),
названия конкретных организаций (без ООО/АО и кавычек), адреса конкретных объектов,
номера документов и реквизиты. НЕ включай: роли сторон (Арендатор, Покупатель, Сторона),
должности, заголовки, названия законов, госорганов, судов, даты договоров и сроков.
Текст значения копируй точно как в документе. Если ничего нет — пустой список."""


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


def review_list(original_text: str, mappings: list, batch: int = 40, deadline: float = None) -> dict:
    """Verdicts for all found persons / companies / addresses, in batches of `batch`."""
    entries, seen = [], set()
    for m in mappings:
        if m['entity_type'] not in REVIEW_TYPES or m['token'] in seen:
            continue
        seen.add(m['token'])
        entries.append({'token': m['token'], 'type': m['entity_type'], 'value': m['original_form'],
                        'context': _context_line(original_text, m['original_form'])})
    remove, merge, errors = [], [], 0
    for start in range(0, len(entries), batch):
        if deadline and time.time() > deadline:
            errors += 1
            break
        part = entries[start:start + batch]
        lines = [f'{i}. «{e["value"]}» — контекст: {e["context"] or "-"}' for i, e in enumerate(part, 1)]
        try:
            raw = llm._ollama_generate('\n'.join(lines), REVIEW_SYSTEM, timeout=120, schema=REVIEW_SCHEMA,
                                       num_predict=32 * len(part) + 200)
            verdicts = json.loads(raw).get('items', [])
        except Exception as ex:
            log.error('llm_review_batch', ex, batch=start // batch)
            errors += 1
            continue
        for v in verdicts:
            i = v.get('id')
            if not isinstance(i, int) or not 1 <= i <= len(part):
                continue
            e = part[i - 1]
            if v.get('category') in NOT_PII and _safe_to_unmask(e['value'], e['type']):
                remove.append({'token': e['token'], 'type': e['type'], 'category': v['category']})
            j = v.get('same_as')
            if isinstance(j, int) and 1 <= j <= len(part) and j != i:
                other = part[j - 1]
                if other['type'] == e['type'] and other['token'] != e['token'] and _plausible_same(e, other):
                    merge.append({'token': e['token'], 'into': other['token']})
    # A→B and B→A are one proposal; never merge into something proposed for removal
    gone = {r['token'] for r in remove}
    uniq, pairs = [], set()
    for mg in merge:
        key = frozenset((mg['token'], mg['into']))
        if key in pairs or mg['token'] in gone or mg['into'] in gone:
            continue
        pairs.add(key)
        uniq.append(mg)
    return {'remove': remove, 'merge': uniq, 'checked': len(entries), 'errors': errors}


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


def find_missed(masked_text: str, block_chars: int = 5000, deadline: float = None) -> list:
    """1-3 calls over suspicious lines only. Returns [{text, type}] found in the text."""
    lines = suspicious_lines(masked_text)
    blocks, cur = [], ''
    for l in lines:
        if len(cur) + len(l) > block_chars and cur:
            blocks.append(cur)
            cur = ''
        cur += l + '\n'
    if cur:
        blocks.append(cur)
    found = []
    for b in blocks[:3]:
        if deadline and time.time() > deadline:
            break
        try:
            data = json.loads(llm._ollama_generate(b, MISSED_SYSTEM, timeout=120))
        except Exception as ex:
            log.error('llm_missed_failed', ex)
            continue
        for e in data.get('entities', []):
            t = (e.get('text') or '').strip()
            if len(t) >= 3 and t in masked_text and not re.search(r'\[[A-Z]+_\d+', t):
                found.append({'text': t, 'type': e.get('type', 'FIO')})
    return found


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
            f_list = ex.submit(review_list, original_text, mappings, 40, deadline)
            try:
                r = f_list.result(timeout=max(1, deadline - time.time()))
            except _Timeout:
                r['errors'] = 1
                log.event('llm_review_timeout', step='list', budget_s=budget_s)
            t1 = time.time()
            if time.time() < deadline:
                f_missed = ex.submit(find_missed, masked_text, 5000, deadline)
                try:
                    missed = f_missed.result(timeout=max(1, deadline - time.time()))
                except _Timeout:
                    r['errors'] = r.get('errors', 0) + 1
                    log.event('llm_review_timeout', step='missed', budget_s=budget_s)
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
