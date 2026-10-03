"""Entity identity: decide whether two found strings are the same person/company.

«Иванов Пётр Сергеевич», «Иванова Петра Сергеевича», «Иванову П.С.», «П.С. Иванов»
and a lone «Иванов» (when only one Ivanov is known) are one person → one token.
«ООО «Вектор»», «ВЕКТОР», «Вектора» are one company.
"""
import re
from functools import lru_cache
from typing import NamedTuple, Optional

_WORD = re.compile(r'[A-Za-zА-ЯЁа-яё][A-Za-zА-ЯЁа-яё\-]*\.?')


@lru_cache(maxsize=1)
def _morph():
    try:
        import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except Exception:
        return None


def _yo(s: str) -> str:
    return s.lower().replace('ё', 'е')


@lru_cache(maxsize=20000)
def _parses(word: str):
    m = _morph()
    return m.parse(word.lower()) if m else []


_SURN_SUFFIX = [
    (re.compile(r'(ск|цк)(?:ий|ого|ому|им|ом|ая|ой|ую|ою)$'), r'\1'),
    (re.compile(r'([ое]в|[иы]н)(?:а|у|ым|ой|ом|е|ою|ых)?$'), r'\1'),
    (re.compile(r'(ич)(?:а|у|ем|е)$'), r'\1'),
]


@lru_cache(maxsize=20000)
def surname_key(word: str) -> str:
    w = _yo(word)
    for p in _parses(w):
        if 'Surn' in p.tag:
            return _yo(p.normal_form)
    for rx, rep in _SURN_SUFFIX:
        if rx.search(w):
            return rx.sub(rep, w)
    # unknown declinable surname: «Кацнельса» → «кацнельс»
    return re.sub(r'(?<=[бвгджзклмнпрстфхцчшщ])(?:а|у|ом|ем|е|ым)$', '', w) if len(w) > 4 else w


@lru_cache(maxsize=20000)
def _lemma(word: str, gram: str) -> Optional[str]:
    """All dictionary forms of the word for a grammeme, joined by «|» («анне|анна»)."""
    forms = sorted({_yo(p.normal_form) for p in _parses(word) if gram in p.tag})
    return '|'.join(forms) or None


class Person(NamedTuple):
    surname: Optional[str]
    name: Optional[str]      # lemma or initial letter
    patr: Optional[str]

    @staticmethod
    def parse(text: str) -> 'Person':
        surname = name = patr = None
        initials = re.findall(r'(?<![А-ЯЁа-яё])([А-ЯЁ])\.', text)
        words = [w for w in _WORD.findall(text) if not re.fullmatch(r'[А-ЯЁA-Z]\.', w)]
        for w in words:
            w = w.rstrip('.')
            cap = w[:1].upper() + w[1:].lower() if w.isupper() else w
            if name is not None and patr is None and _lemma(cap, 'Patr'):
                patr = _lemma(cap, 'Patr')            # patronymic follows the first name
            elif name is None and _lemma(cap, 'Name') and not _lemma(cap, 'Surn'):
                name = _lemma(cap, 'Name')
            elif surname is None:
                surname = surname_key(cap)
        if initials:
            if name is None:
                name = _yo(initials[0])
            if patr is None and len(initials) > 1:
                patr = _yo(initials[1])
        return Person(surname, name, patr)

    def compatible(self, other: 'Person') -> bool:
        if self.surname and other.surname and self.surname != other.surname:
            return False
        if not (self.surname and other.surname):
            # without a surname on one side require full name + patronymic match
            if self.surname or other.surname:
                return False
            return bool(self.name and self.patr) and self.name == other.name and \
                _same(self.patr, other.patr)
        return _same(self.name, other.name) and _same(self.patr, other.patr)

    def specificity(self) -> int:
        return sum(1 for x in self if x)


def _same(a, b) -> bool:
    if not a or not b:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return bool(set(a.split('|')) & set(b.split('|')))


@lru_cache(maxsize=20000)
def org_key(text: str) -> str:
    words = re.findall(r'[A-Za-zА-ЯЁа-яё0-9]+', text)
    out = []
    for w in words:
        lw = _yo(w)
        if re.search('[а-я]', lw):
            ps = _parses(lw)
            lw = _yo(ps[0].normal_form) if ps else lw
        out.append(lw)
    return ' '.join(out)


def value_key(text: str, etype: str) -> str:
    digits = re.sub(r'\D', '', text)
    if etype in ('ИНН', 'ОГРН', 'КПП', 'РС', 'КС', 'БИК', 'СНИЛС', 'КАРТА', 'ТЕЛЕФОН', 'ОКПО', 'ПОЛИС') and digits:
        return digits[-10:] if etype == 'ТЕЛЕФОН' else digits
    if etype == 'ЮЛ':
        return org_key(text.replace('\xad', ''))
    if etype in ('АДРЕС', 'ADR'):
        return address_key(text)
    return re.sub(r'\s+', ' ', _yo(text)).strip(' .,;:')


def compact_key(text: str) -> str:
    """Letters and digits only: «ВекторФ уд» (OCR space), «Вектор\\xadФуд», «ВЕКТОРФУД» → «векторфуд»."""
    return re.sub(r'[^0-9a-zа-я]', '', _yo(text.replace('\xad', '')))


_ADDR_ABBR = [
    (r'\b(?:город|гор)\b\.?', 'г'), (r'\bг\.', 'г'), (r'\bулица\b|\bул\.', 'ул'), (r'\bдом\b|\bд\.', 'д'),
    (r'\bквартира\b|\bкв\.', 'кв'), (r'\bпроспект\b|\bпр-кт\b|\bпр-т\b|\bпросп\.', 'пр'),
    (r'\bпереулок\b|\bпер\.', 'пер'), (r'\bстроение\b|\bстр\.', 'стр'), (r'\bкорпус\b|\bкорп\.|\bк\.', 'к'),
    (r'\bофис\b|\bоф\.', 'оф'), (r'\bпомещение\b|\bпомещ\.|\bпом\.', 'пом'), (r'\bобласть\b|\bобл\.', 'обл'),
    (r'\bнабережная\b|\bнаб\.', 'наб'), (r'\bшоссе\b|\bш\.', 'ш'), (r'\bбульвар\b|\bб-р\b', 'бр'),
    (r'\bрайона?\b|\bр-н[а]?\b\.?', 'рн'), (r'\bкомната\b|\bкомн\.', 'комн'),
    (r'\bреспублика\b|\bресп\.', 'респ'), (r'\bмикрорайон\b|\bмкр\.?\b', 'мкр'),
    # a settlement: «пос.», «пгт», «посёлок», «р.п.», «с.», «дер.» — one kind, the name decides
    (r'\bпос[её]лок\s+городского\s+типа\b|\bрабочий\s+пос[её]лок\b|\bпос[её]лок\b|\bпос\.|\bпгт\b\.?'
     r'|\bр\.\s?п\.|\bрп\b\.?|\bсело\b|\bдеревня\b|\bдер\.|\bп\.(?=\s*[а-я])|\bс\.(?=\s*[а-я])', 'нп'),
]
_ADDR_UNIT = ('д', 'к', 'стр', 'кв', 'оф', 'пом', 'комн')     # house-level parts start a new part
_ADDR_ROOM = ('кв', 'оф', 'пом', 'комн')


def address_parts(text: str) -> list:
    """Normalized parts of an address: «г. Тверь, ул. Озёрная, д. 17» = «город Тверь, улица
    Озерная, дом 17» = «Тверь г.,\nОзерная ул., д.17.»; index and country do not matter.
    Everything before the house is one sorted bag of words («р-н Конаковский» = «Конаковский
    район», with or without commas); house, корпус, flat, room are parts in their order."""
    t = _yo(text)
    t = re.sub(r'(?<!\d)\d{6}(?!\d)', ' ', t)
    t = re.sub(r'российская\s+федерация|\bрф\b|\bроссия\b', ' ', t)
    for rx, rep in _ADDR_ABBR:
        t = re.sub(rx, f' {rep} ', t)
    t = re.sub(r'(?<![0-9a-zа-я])(' + '|'.join(_ADDR_UNIT) + r')(?=\s+\d)', r',\1', t)
    place, parts = [], []
    for chunk in t.split(','):
        words = re.findall(r'[0-9a-zа-я/]+', chunk)
        if not words:
            continue
        if words[0] in _ADDR_UNIT and len(words) > 1 and words[1][:1].isdigit():
            parts.append(' '.join(words))
        elif parts:
            parts.append(' '.join(sorted(words)))
        else:
            place += words
    # region, district, settlement, street — one bag of words: commas and order do not matter
    return ([' '.join(sorted(place))] if place else []) + parts


def address_key(text: str) -> str:
    return ', '.join(address_parts(text))


def same_person(a: str, b: str) -> bool:
    pa, pb = Person.parse(a), Person.parse(b)
    if not (pa.surname or pa.name) or not (pb.surname or pb.name):
        return False
    return pa.compatible(pb)


def _lev(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


_NUMBER_TYPES = {'ИНН', 'ОГРН', 'КПП', 'РС', 'КС', 'БИК', 'СНИЛС', 'ПАСПОРТ', 'КАДАСТР', 'НЕДВИЖ', 'ЕГРН',
                 'ТЕЛЕФОН', 'КАРТА', 'IBAN', 'ОКПО', 'ПОЛИС', 'ВУ', 'РЕГНОМЕР', 'ГОСНОМЕР', 'VIN', 'SWIFT'}


def merge_key(text: str, etype: str) -> str:
    """Key of the AUTOMATIC merge rule (task 3, §1.1): equal keys = one entity, one token.

    numbers — digits (and series letters) only, never «similar»; persons — surname lemma +
    first letters of name and patronymic; companies — letters/digits without legal form,
    quotes, case, ё, spaces; addresses — normalized abbreviations, punctuation, line breaks."""
    t = (text or '').replace('\xad', '')
    if etype in _NUMBER_TYPES:
        if etype == 'EMAIL':
            return t.strip().lower()
        k = re.sub(r'[^0-9A-Za-zА-ЯЁа-яё]', '', t).upper()
        return k[-10:] if etype == 'ТЕЛЕФОН' else k
    if etype == 'EMAIL':
        return t.strip().lower()
    if etype in ('ФИО', 'FIO'):
        p = Person.parse(t)
        if not p.surname:
            return ''
        return f'{p.surname}|{(p.name or "")[:1]}|{(p.patr or "")[:1]}'
    if etype in ('ЮЛ', 'YUL'):
        from core.anonymizer import _OPF_SHORT
        t = re.sub(r'(?<![А-ЯЁA-Z])(?:' + _OPF_SHORT + r')(?![А-ЯЁA-Z])', ' ', t)
        return compact_key(t)
    if etype in _ADDRESS_TYPES:
        return address_key(t)
    return re.sub(r'\s+', ' ', _yo(t)).strip(' .,;:')


_ADDRESS_TYPES = {'АДРЕС', 'ADR', 'АДРЕС_ФИЗ', 'АДРЕС_ЮР', 'ADDR_PHYS', 'ADDR_CORP'}
_PERSON_TYPES = {'ФИО', 'FIO'}
_ORG_TYPES = {'ЮЛ', 'YUL'}


def _tok_prefix(token: str) -> str:
    return token.strip('[]').rsplit('_', 1)[0]


def _forms(mappings) -> dict:
    """{token: (type, [values])} for active rows."""
    out = {}
    for m in mappings:
        if m.get('status', 'active') != 'active':
            continue
        out.setdefault(m['token'], (m['entity_type'], []))[1].append(m['original_form'])
    return out


def _persons_compatible(xs, ys) -> bool:
    px, py = [Person.parse(v) for v in xs], [Person.parse(v) for v in ys]
    if not any(p.surname for p in px) or not any(p.surname for p in py):
        return False
    return all(a.compatible(b) for a in px for b in py)


def auto_groups(mappings, distinct=()) -> list:
    """Tokens that are one entity by the AUTOMATIC rules (task 3, §1.1): lists of tokens,
    the first (lowest number) is the one to keep. Persons: every form of one token is
    compatible with every form of the other, and the whole group is pairwise compatible
    (an «И.И. Иванов» between «Иван Иванович» and «Илья Ильич» merges with neither)."""
    forms = _forms(mappings)
    parent = {t: t for t in forms}

    def find(t):
        while parent[t] != t:
            parent[t] = parent[parent[t]]
            t = parent[t]
        return t

    def union(a, b):
        parent[find(a)] = find(b)

    buckets = {}
    persons = {}
    for tok, (etype, vals) in forms.items():
        pfx = _tok_prefix(tok)
        if etype in _PERSON_TYPES:
            for v in vals:
                sk = Person.parse(v).surname
                if sk:
                    persons.setdefault((pfx, sk), set()).add(tok)
            continue
        for v in vals:
            k = merge_key(v, etype)
            if etype in _ORG_TYPES and len(k) < 4:
                continue
            if k:
                buckets.setdefault((pfx, k), set()).add(tok)
            if etype in _ORG_TYPES:
                ok = org_key(v)
                if ok:
                    buckets.setdefault((pfx, 'lemmas', ok), set()).add(tok)
    for toks in buckets.values():
        toks = list(toks)
        for t in toks[1:]:
            union(toks[0], t)
    for toks in persons.values():
        toks = sorted(toks)
        comp = {}
        for i, a in enumerate(toks):
            for b in toks[i + 1:]:
                if _persons_compatible(forms[a][1], forms[b][1]):
                    comp.setdefault(a, set()).add(b)
                    comp.setdefault(b, set()).add(a)
        seen = set()
        for a in comp:
            if a in seen:
                continue
            group, stack = set(), [a]
            while stack:
                x = stack.pop()
                if x not in group:
                    group.add(x)
                    stack.extend(comp.get(x, ()))
            seen |= group
            g = sorted(group)
            if all(y in comp.get(x, ()) for i, x in enumerate(g) for y in g[i + 1:]):
                for t in g[1:]:
                    union(g[0], t)
    groups = {}
    for t in forms:
        groups.setdefault(find(t), []).append(t)
    distinct = {frozenset(x) for x in distinct}
    # a group holding a pair the user called «разные» is left as it is
    return [sorted(g, key=_tok_order) for g in groups.values() if len(g) > 1
            and not any(frozenset((x, y)) in distinct for i, x in enumerate(g) for y in g[i + 1:])]


def _tok_order(token: str):
    pfx, _, n = token.strip('[]').rpartition('_')
    return (pfx, int(n) if n.isdigit() else 0, token)


def _initials_only(p: 'Person') -> bool:
    return bool(p.name) and len(p.name) == 1


def review_pairs(mappings, rejected=()) -> list:
    """Pairs to show a HUMAN in «Проверка дублей» (task 3, §1.1, §5.4) — never numbers:
    persons — namesakes where one side has only initials and they differ, or a compatible
    form left apart; companies — 1 differing char per 8 letters (OCR); addresses — one is
    the other plus a room / flat / office, or a typo with the same numbers. Different
    houses, корпуса and rooms are never proposed. `rejected`: {frozenset((a, b))} the user
    marked «Это разные»."""
    forms = _forms(mappings)
    rejected = {frozenset(x) for x in rejected}
    items = sorted(forms.items(), key=lambda x: _tok_order(x[0]))
    out = []
    for i, (a, (ta, va)) in enumerate(items):
        for b, (tb, vb) in items[i + 1:]:
            if _tok_prefix(a) != _tok_prefix(b) or frozenset((a, b)) in rejected:
                continue
            x, y = va[0], vb[0]
            if ta in _PERSON_TYPES:
                ok = _propose_persons(va, vb)
            elif ta in _ORG_TYPES:
                ka, kb = merge_key(x, ta), merge_key(y, tb)
                lim = max(1, min(len(ka), len(kb)) // 8)
                ok = len(ka) >= 4 and len(kb) >= 4 and ka != kb and _lev(ka, kb, lim) <= lim
            elif ta in _ADDRESS_TYPES:
                ok = _propose_addresses(x, y)
            else:
                ok = False
            if ok:
                out.append({'a': a, 'b': b, 'type': ta, 'a_value': x, 'b_value': y})
    return out


similar_pairs = review_pairs


def _propose_persons(va, vb) -> bool:
    pa, pb = Person.parse(va[0]), Person.parse(vb[0])
    if not pa.surname or pa.surname != pb.surname:
        return False
    if _persons_compatible(va, vb):
        return True                          # compatible but left apart (ambiguous namesakes)
    # different initials: «Белозёров П.С.» next to «Белозёров Аркадий Львович»; two different
    # full first names are different people and are not proposed
    return _initials_only(pa) or _initials_only(pb)


def _house_numbers(parts) -> list:
    return [p for p in parts if re.search(r'\d', p)]


def _propose_addresses(x: str, y: str) -> bool:
    px, py = address_parts(x), address_parts(y)
    if px == py:
        return False
    short, long_ = (px, py) if len(px) <= len(py) else (py, px)
    if long_[:len(short)] == short:
        has_house = any(re.search(r'(?:^| )д(?: |$)', p) for p in short)
        extra = long_[len(short):]
        return has_house and all(any(w in _ADDR_ROOM for w in p.split()) for p in extra)
    # a typo with the same numbers and the same number of parts
    if len(px) != len(py) or _house_numbers(px) != _house_numbers(py):
        return False
    ka, kb = ''.join(px).replace(' ', ''), ''.join(py).replace(' ', '')
    lim = max(1, min(len(ka), len(kb)) // 8)
    return _lev(ka, kb, lim) <= lim
