"""Rule detectors (layer 1): form + context + checksum.

Each detector returns a list of `Hit(start, end, type)` spans in the given
text. `find_all()` runs them in priority order; the caller resolves overlaps
(first come wins) and skips spans that touch existing [TOKEN] placeholders.

Only values are marked — keywords such as «паспорт серии», «ИНН», «ООО»
stay in the text. Algorithms follow docs/PII_VARIANTS_CATALOG.md.
"""
import re
from functools import lru_cache
from typing import List, NamedTuple

from core import validators as V


class Hit(NamedTuple):
    start: int
    end: int
    type: str


_I = re.IGNORECASE | re.UNICODE
_U = re.UNICODE


def _left(text, pos, n=60):
    return text[max(0, pos - n):pos]


def _ctx(regex, text, pos, n=60):
    return bool(regex.search(_left(text, pos, n)))


# ── Numeric identifiers ──────────────────────────────────────────────────────

_RUN_RE = re.compile(r'(?<![\d\w])\d+(?![\d])', _U)
_INN_CTX = re.compile(r'ИНН|TIN|Tax\s*ID|налогоплательщик', _I)
_KPP_CTX = re.compile(r'КПП|причины\s+постановки', _I)
_SNILS_CTX = re.compile(r'СНИЛС|страхов\w*\s+номер|лицев\w*\s+сч[её]т\w*\s*$', _I)
_OKPO_CTX = re.compile(r'ОКПО', _I)
_OMS_CTX = re.compile(r'ОМС|полис', _I)
_CARD_CTX = re.compile(r'карт', _I)
_PASSPORT_CTX = re.compile(r'паспорт|удостоверени\w+\s+личност', _I)
_PHONE_CTX = re.compile(r'\b(?:тел|моб|факс|phone|whatsapp|звон)', _I)
_SEP_AFTER_INN = re.compile(r'^[\s/|,;\\]{1,6}$')


def _numeric(text: str) -> List[Hit]:
    hits = []
    inn_ends = []
    for m in _RUN_RE.finditer(text):
        d, s, e = m.group(), m.start(), m.end()
        n = len(d)
        if text[e:e + 1] in ',.' and text[e + 1:e + 2].isdigit():
            continue  # decimal number
        left = _left(text, s, 60)
        if n in (10, 12) and V.inn(d):
            if _ctx(_PASSPORT_CTX, text, s, 25) or _ctx(_PHONE_CTX, text, s, 25):
                continue
            hits.append(Hit(s, e, 'ИНН'))
            inn_ends.append(e)
        elif n == 13 and V.ogrn(d):
            hits.append(Hit(s, e, 'ОГРН'))
        elif n == 15 and V.ogrnip(d):
            hits.append(Hit(s, e, 'ОГРН'))
        elif n == 9 and (_KPP_CTX.search(left) and not re.search(r'БИК\s*:?\s*$', left, _I)
                         or any(_SEP_AFTER_INN.match(text[ie:s] or 'x') for ie in inn_ends)):
            hits.append(Hit(s, e, 'КПП'))
        elif n == 11 and V.snils(d) and _SNILS_CTX.search(left):
            hits.append(Hit(s, e, 'СНИЛС'))
        elif n == 20:
            hits.append(Hit(s, e, 'КС' if d.startswith('301') else 'РС'))
        elif n == 16 and (_OMS_CTX.search(_left(text, s, 30))):
            hits.append(Hit(s, e, 'ПОЛИС'))
        elif n == 16 and (V.luhn(d) and _CARD_CTX.search(left)):
            hits.append(Hit(s, e, 'КАРТА'))
        elif n in (8, 10) and _OKPO_CTX.search(_left(text, s, 20)):
            hits.append(Hit(s, e, 'ОКПО'))
    return hits


_INN12_GROUPED = re.compile(r'(?<![\d])\d{4}[  ]\d{4}[  ]\d{4}(?![\d])')
_ACC_GROUPED = re.compile(r'(?<![\d])\d{5}[ \-]\d{3}[ \-]\d[ \-]\d{4}[ \-]\d{7}(?![\d])')
_CARD_GROUPED = re.compile(r'(?<![\d])\d{4}([ \-])\d{4}\1\d{4}\1\d{4}(?:\1\d{1,3})?(?![\d])')
_SNILS_GROUPED = re.compile(r'(?<![\d])\d{3}[\- ]\d{3}[\- ]\d{3}[\- ]\d{2}(?![\d])')
_IBAN_RE = re.compile(r'\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b')


def _grouped_numbers(text: str) -> List[Hit]:
    hits = []
    for m in _CARD_GROUPED.finditer(text):
        if V.luhn(m.group()) or _ctx(_CARD_CTX, text, m.start(), 40):
            hits.append(Hit(m.start(), m.end(), 'КАРТА'))
    for m in _INN12_GROUPED.finditer(text):
        if V.inn12(m.group()):
            hits.append(Hit(m.start(), m.end(), 'ИНН'))
    for m in _ACC_GROUPED.finditer(text):
        hits.append(Hit(m.start(), m.end(), 'РС'))
    for m in _SNILS_GROUPED.finditer(text):
        if V.snils(m.group()) or _ctx(_SNILS_CTX, text, m.start(), 40):
            hits.append(Hit(m.start(), m.end(), 'СНИЛС'))
    for m in _IBAN_RE.finditer(text):
        if V.iban(m.group()):
            hits.append(Hit(m.start(), m.end(), 'IBAN'))
    return hits


_SWIFT_RE = re.compile(
    r'(?:SWIFT|BIC)(?:\s*(?:code|код|/\s*BIC))?\s*[:\-]?\s*'
    r'([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b', re.UNICODE)


def _swift(text):
    return [Hit(m.start(1), m.end(1), 'SWIFT') for m in _SWIFT_RE.finditer(text)]


# ── Phones ───────────────────────────────────────────────────────────────────

_PHONE_RU = re.compile(
    r'(?<![\d\w+])(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?!\d)')
_PHONE_INTL = re.compile(
    r'(?<![\d\w])\+(?!7)\d{1,3}(?:[\s\-]?\(?\d{1,4}\)?){1,2}(?:[\s\-]?\d{2,4}){2,4}(?!\d)')
_PHONE_LOCAL = re.compile(
    r'(?<![\d\w])(?:\(\d{3,5}\)\s*)?\d{3}[\s\-]\d{2}[\s\-]\d{2}(?!\d)')


def _phones(text: str) -> List[Hit]:
    hits = []
    for m in _PHONE_RU.finditer(text):
        hits.append(Hit(m.start(), m.end(), 'ТЕЛЕФОН'))
    for m in _PHONE_INTL.finditer(text):
        if 9 <= len(V.digits(m.group())) <= 15:
            hits.append(Hit(m.start(), m.end(), 'ТЕЛЕФОН'))
    for m in _PHONE_LOCAL.finditer(text):
        if _ctx(_PHONE_CTX, text, m.start(), 30):
            hits.append(Hit(m.start(), m.end(), 'ТЕЛЕФОН'))
    return hits


# ── Passport and other personal documents ────────────────────────────────────

_NUMW = r'(?:№|N|No\.?|номер)'
_SER_NUM = re.compile(
    r'(?<![\d])(\d{2}\s?\d{2})(?:\s*,?\s*' + _NUMW + r'?\s*)(\d{6})(?![\d])', _I)
_PASS_ONE = re.compile(r'(?<![\d])(\d{10})(?![\d])')
_FOREIGN = re.compile(r'(?<![\d])(\d{2}\s?\d{7})(?![\d])')
_FOREIGN_CTX = re.compile(r'загран', _I)
_SERIES_CTX = re.compile(r'паспорт|сери[яи]|удостоверени\w+\s+личност', _I)
_DIV_CODE = re.compile(
    r'(?:код\w*\s+подразделени\w*|к/п|к\.п\.)\s*[:№]?\s*(\d{3}[\-\s]?\d{3})(?![\d])', _I)
_ISSUED = re.compile(
    r'выдан[аоы]?\s*:?\s*(?:\d{2}\.\d{2}\.\d{4}\s*(?:г\.?\s*)?)?'
    r'([А-ЯЁ][^\n;]{2,150}?)(?=\s*(?:,|;|\n|$|\d{2}\.\d{2}\.\d{4}))', _U)
_DRIVER = re.compile(
    r'водительск\w+\s+удостоверени\w+\s*(?:серии\s*|№\s*)?(\d{2}\s?\d{2}\s?№?\s?\d{6})', _I)


def _passport(text: str) -> List[Hit]:
    hits = []
    for m in _SER_NUM.finditer(text):
        if not _ctx(_SERIES_CTX, text, m.start(), 60):
            continue
        gap = text[m.end(1):m.start(2)]
        if gap.strip():
            hits.append(Hit(m.start(1), m.end(1), 'ПАСПОРТ'))
            hits.append(Hit(m.start(2), m.end(2), 'ПАСПОРТ'))
        else:
            hits.append(Hit(m.start(1), m.end(2), 'ПАСПОРТ'))
    for m in _PASS_ONE.finditer(text):
        if _ctx(_PASSPORT_CTX, text, m.start(), 25):
            hits.append(Hit(m.start(1), m.end(1), 'ПАСПОРТ'))
    for m in _FOREIGN.finditer(text):
        if _ctx(_FOREIGN_CTX, text, m.start(), 40):
            hits.append(Hit(m.start(1), m.end(1), 'ПАСПОРТ'))
    for m in _DIV_CODE.finditer(text):
        hits.append(Hit(m.start(1), m.end(1), 'ПАСПОРТ'))
    for m in _ISSUED.finditer(text):
        if _ctx(_PASSPORT_CTX, text, m.start(), 150) or _ctx(re.compile(r'сери[яи]', _I), text, m.start(), 60):
            hits.append(Hit(m.start(1), m.end(1), 'ПАСПОРТ'))
    for m in _DRIVER.finditer(text):
        hits.append(Hit(m.start(1), m.end(1), 'ВУ'))
    return hits


# ── Birth dates and places ───────────────────────────────────────────────────

_MONTHS = (r'(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*')
_DATE = r'(?:\d{1,2}\.\d{1,2}\.\d{2,4}|\d{1,2}\s+' + _MONTHS + r'\s+\d{4})'
_DOB_PATTERNS = [
    re.compile(r'(' + _DATE + r')\s*(?:г\.?\s*)?(?:г\.\s*р\.|года?\s+рожд\w*|г\.р\.?)', _I),
    re.compile(r'(?<![\d.])(\d{4})\s*г\.\s*р\.?', _I),
    re.compile(r'(?:родил(?:ся|ась)|рожд[её]н\w*)\s+(' + _DATE + r')', _I),
    re.compile(r'дата\s+(?:и\s+место\s+)?рождени\w*\s*[:\-]?\s*(' + _DATE + r')', _I),
]
_PLACE = r'(?:г\.|гор\.|город|пос\.|пгт\.?|с\.|село|дер\.|деревня|ст-ца|станица)\s*[А-ЯЁ][\w\-]+(?:\s+[А-ЯЁ][\w\-]+)?'
_BIRTHPLACE = re.compile(r'место\s+рождени\w*\s*[:\-]?\s*(?:' + _DATE + r'\s*,\s*)?(' + _PLACE + r')', _I)


def _birth(text: str) -> List[Hit]:
    hits = []
    for pat in _DOB_PATTERNS:
        for m in pat.finditer(text):
            hits.append(Hit(m.start(1), m.end(1), 'ДАТАРОЖД'))
    for m in _BIRTHPLACE.finditer(text):
        hits.append(Hit(m.start(1), m.end(1), 'АДРЕС'))
    return hits


# ── Addresses: chain of address components ───────────────────────────────────

_NM = r'(?:\d{1,2}-?[яйе]\s+)?[А-ЯЁ][А-ЯЁа-яё\-]+(?:\s+[А-ЯЁ][А-ЯЁа-яё\-]+){0,2}'
_STREET_MK = (r'(?i:ул\.|улица|пр-т|пр-кт|просп\.|проспект|пер\.|пер\b|переулок|наб\.|набережная|'
              r'ш\.|шоссе|б-р|бул\.|бульвар|пл\.|площадь|проезд|пр-д|туп\.|тупик|аллея|линия|мкр\.|'
              r'микрорайон|кв-л|квартал)')
_HOUSE = r'(?i:д\.|дом|вл\.|владение|зд\.|здание)\s*№?\s*\d+[А-Яа-яA-Za-z]?(?:[/\-]\d+[А-Яа-я]?)?'
_EXTRA = (r'(?i:кв\.|квартира|оф\.|офис|пом\.|помещ\.|помещение|стр\.|строение|корп\.|корпус|к\.|'
          r'лит\.|литера|литер|блок|эт\.|этаж|комн\.|комната|ком\.)\s*№?\s*[\wА-Яа-я][\wА-Яа-я/\-]*')
_COMP = {
    'index': r'\d{6}',
    'country': r'(?:Российская\s+Федерация|РФ|Россия)',
    'region': (r'(?:' + _NM + r'\s+(?i:обл\.|область|край|автономный\s+округ|АО)|'
               r'(?i:Республика|респ\.)\s+' + _NM + r')'),
    'district': r'(?:' + _NM + r'\s+(?i:р-н|район)|(?i:р-н|район)\s+' + _NM + r')',
    'city': (r'(?:(?i:г\.|гор\.|город|пос\.|пгт\.?|с\.|село|дер\.|деревня|ст-ца|рп\.?|'
             r'вн\.тер\.г\.)\s*' + _NM + r')'),
    'street': r'(?:' + _STREET_MK + r'\s*' + _NM + r'|' + _NM + r'\s+' + _STREET_MK + r')',
    'house': _HOUSE,
    'extra': _EXTRA,
    'pobox': r'(?i:а/я|абонентский\s+ящик)\s*№?\s*\d+',
    'num': r'\d{1,4}[А-Яа-я]?(?:/\d{1,4})?(?![\d.:])',
}
_COMP_RE = {k: re.compile(v, re.UNICODE) for k, v in _COMP.items()}
_ORDER = ['pobox', 'index', 'country', 'region', 'district', 'city', 'street', 'house', 'extra']
_ADDR_CTX = re.compile(r'адрес|местонахождени|место\s+нахождени|место\s+жительств|проживающ|'
                       r'зарегистрирован|регистраци', _I)
_JOIN = re.compile(r'\s*,?\s*', _U)


def _match_comp(text, pos, prev):
    for k in _ORDER:
        m = _COMP_RE[k].match(text, pos)
        if m and m.end() > pos:
            return k, m.end()
    if prev == 'street':
        m = _COMP_RE['num'].match(text, pos)
        if m:
            return 'house', m.end()
    return None, pos


_START_RE = re.compile(
    r'(?<![\wА-Яа-яЁё])(?=\d{6}|[А-ЯЁ]|(?i:ул\.|пр-т|пер\.|пер\b|наб\.|г\.|гор\.|город|пос\.|мкр\.|а/я|'
    r'республика|респ\.|д\.|дом|кв\.|оф\.|офис|стр\.|корп\.|лит\.|блок|вн\.тер))', re.UNICODE)


def _addresses(text: str) -> List[Hit]:
    hits = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _START_RE.search(text, pos)
        if not m:
            break
        start = m.start()
        kinds, ends, cur, prev = [], [], start, None
        while True:
            k, e = _match_comp(text, cur, prev)
            if not k:
                break
            kinds.append(k)
            ends.append(e)
            prev = k
            j = _JOIN.match(text, e)
            nxt = j.end() if j else e
            if nxt == e and e < n and text[e:e + 1] not in ' ,':
                break
            cur = nxt
        if kinds:
            ks = set(kinds)
            ok = (('street' in ks and ({'house', 'extra'} & ks))
                  or 'pobox' in ks
                  or ('index' in ks and ({'city', 'street', 'region'} & ks))
                  or (len(kinds) >= 2 and ({'city', 'region', 'district'} & ks)
                      and _ctx(_ADDR_CTX, text, start, 80)))
            if ok:
                # drop a dangling leading country
                s = start
                end = ends[-1]
                if kinds[0] == 'country' and len(kinds) > 1:
                    j = _JOIN.match(text, ends[0])
                    s = j.end()
                hits.append(Hit(s, end, 'АДРЕС'))
                pos = end
                continue
        pos = start + 1
    return hits


_CADASTRAL = re.compile(r'(?<![\d:])\d{2}:\d{2}:\d{6,7}:\d{1,6}(?![\d:])')


def _cadastral(text):
    return [Hit(m.start(), m.end(), 'КАДАСТР') for m in _CADASTRAL.finditer(text)]


# ── Other identifiers ────────────────────────────────────────────────────────

_PLATE = re.compile(r'(?<![\wА-Яа-я])[АВЕКМНОРСТУХABEKMHOPCTYX]\s?\d{3}\s?[АВЕКМНОРСТУХABEKMHOPCTYX]{2}\s?\d{2,3}(?![\w])')
_VIN = re.compile(r'(?<![A-Z0-9])(?=[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9]))(?=[A-HJ-NPR-Z0-9]*\d)(?=[A-HJ-NPR-Z0-9]*[A-Z])[A-HJ-NPR-Z0-9]{17}')
_NOTARY = re.compile(r'(?<![\d/])\d{2,3}/\d{1,4}-н/\d{2,3}-\d{4}-\d{1,3}-\d{1,6}(?![\d])')
_NICK = re.compile(r'(?<![\w@.])@[A-Za-z][A-Za-z0-9_]{3,31}\b')
_EMAIL_OBF = re.compile(
    r'[A-Za-z0-9._%+\-]+\s*[\[\(\{]\s*(?:at|собака)\s*[\]\)\}]\s*[A-Za-z0-9\-]+'
    r'(?:\s*[\[\(\{]\s*(?:dot|точка)\s*[\]\)\}]\s*|\.)[A-Za-z]{2,}', re.I)


def _other(text):
    hits = [Hit(m.start(), m.end(), 'ГОСНОМЕР') for m in _PLATE.finditer(text)]
    hits += [Hit(m.start(), m.end(), 'VIN') for m in _VIN.finditer(text)]
    hits += [Hit(m.start(), m.end(), 'НОТАРИУС') for m in _NOTARY.finditer(text)]
    hits += [Hit(m.start(), m.end(), 'EMAIL') for m in _EMAIL_OBF.finditer(text)]
    hits += [Hit(m.start(), m.end(), 'НИК') for m in _NICK.finditer(text)]
    return hits


# ── Legal entities without quotes / foreign ──────────────────────────────────

_OPF_BARE = re.compile(
    r'(?<![\wА-Яа-я])(?:ООО|ПАО|АО|ЗАО|ОАО|НАО|ГК)\s+'
    r'([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z0-9\-]+(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z0-9\-]+)?)', _U)
_OPF_TAIL = re.compile(
    r'(?<![\wА-Яа-я«"])((?:[Бб]анк\s+)?[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z0-9\-]+(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z0-9\-]+){0,2})'
    r'\s*\((?:ПАО|АО|ООО|ЗАО|ОАО|НАО)\)', _U)
_FOREIGN_ORG = re.compile(
    r'((?:[A-Z][A-Za-z0-9&\'\-]*\.?\s+){0,4}[A-Z][A-Za-z0-9&\'\-]*)\s*,?\s+'
    r'(?:LLC|L\.L\.C\.|Ltd\.?|Limited|Inc\.?|Corp\.?|Corporation|GmbH|AG|S\.A\.|SA|B\.V\.|BV|N\.V\.|PLC|LLP|LP|SARL|S\.?r\.?l\.?|Pte\.?)(?![A-Za-z])')
_GENERIC_ORG_QUOTED = re.compile(
    r'(?<![\wА-Яа-я])(?:[Оо]бществ\w*|[Кк]омпани\w*|[Фф]ирм\w*|[Пп]редприяти\w*)\s+'
    r'[«"“„]([А-ЯЁA-Z0-9][^«»"“”„\n]{1,80})[»"”“]', _U)
# Generic words that are never a company name on their own (roles, headings)
_NOT_ORG_NAME = {
    'покупатель', 'продавец', 'заказчик', 'исполнитель', 'подрядчик', 'арендатор',
    'арендодатель', 'займодавец', 'заемщик', 'заёмщик', 'кредитор', 'должник',
    'общество', 'компания', 'банк', 'сторона', 'стороны', 'агент', 'принципал',
    'поставщик', 'гарант', 'бенефициар', 'цедент', 'цессионарий', 'залогодатель',
    'залогодержатель', 'лицензиар', 'лицензиат', 'участник', 'эмитент',
}


_PAYMENT_CTX = re.compile(r'р/сч?|к/сч?|сч[её]т\w*|БИК|\d{20}', _I)


def _is_payment_bank(text, start):
    """«р/с ... в ПАО Сбербанк» — the paying bank is public info, not PII (catalog §4, §12.4)."""
    left = _left(text, start, 60)
    return bool(re.search(r'\bв\s+(?:\S+\s+)?$', left)) and bool(_PAYMENT_CTX.search(left))


def _orgs(text: str) -> List[Hit]:
    hits = []
    for rx in (_OPF_BARE, _OPF_TAIL, _GENERIC_ORG_QUOTED):
        for m in rx.finditer(text):
            name = m.group(1)
            if name.lower() in _NOT_ORG_NAME:
                continue
            if _is_payment_bank(text, m.start()):
                continue
            if rx is _OPF_BARE and name.isupper() and len(name) <= 3 and name in ('КБ', 'НКО', 'МФО'):
                continue
            hits.append(Hit(m.start(1), m.end(1), 'ЮЛ'))
    for m in _FOREIGN_ORG.finditer(text):
        hits.append(Hit(m.start(1), m.end(1), 'ЮЛ'))
    return hits


# ── Persons: morphology (pymorphy3) ──────────────────────────────────────────

@lru_cache(maxsize=1)
def _morph():
    try:
        import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except Exception:
        return None


@lru_cache(maxsize=20000)
def _tags(word: str) -> frozenset:
    """Grammemes Surn/Name/Patr found among confident parses of a word."""
    m = _morph()
    if m is None:
        return frozenset()
    out = set()
    parses = m.parse(word)
    best = parses[0].score if parses else 0
    for p in parses:
        if p.score < best * 0.1:
            continue
        for g in ('Surn', 'Name', 'Patr', 'Geox', 'Orgn'):
            if g in p.tag:
                out.add(g)
    return frozenset(out)


_LAT2CYR = [
    ('shch', 'щ'), ('sch', 'щ'), ('yo', 'ё'), ('zh', 'ж'), ('kh', 'х'), ('ts', 'ц'),
    ('ch', 'ч'), ('sh', 'ш'), ('yu', 'ю'), ('ya', 'я'), ('iy', 'ий'), ('yy', 'ый'),
    ('ye', 'е'), ('a', 'а'), ('b', 'б'), ('v', 'в'), ('g', 'г'), ('d', 'д'), ('e', 'е'),
    ('z', 'з'), ('i', 'и'), ('y', 'ы'), ('k', 'к'), ('l', 'л'), ('m', 'м'), ('n', 'н'),
    ('o', 'о'), ('p', 'п'), ('r', 'р'), ('s', 'с'), ('t', 'т'), ('u', 'у'), ('f', 'ф'),
    ('h', 'х'), ('c', 'к'), ('w', 'в'), ('x', 'кс'), ('j', 'й'), ('q', 'к'),
]


def _to_cyr(word: str) -> str:
    w = word.lower()
    out = ''
    i = 0
    while i < len(w):
        for lat, cyr in _LAT2CYR:
            if w.startswith(lat, i):
                out += cyr
                i += len(lat)
                break
        else:
            out += w[i]
            i += 1
    return out.capitalize()


_CAP = r'[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?'
_WORD_CAP = re.compile(r'(?<![\wА-Яа-яЁё.])' + _CAP + r'(?![\wА-Яа-яЁё])', _U)
_INIT1 = re.compile(r'\s*([А-ЯЁ]\.(?:\s*[А-ЯЁ]\.)?)', _U)
_ROLE_CTX = re.compile(
    r'(?:директор\w*|заявител\w*|ответчик\w*|истц\w*|ист[её]ц|представител\w*|'
    r'председател\w*|секретар\w*|гражданин\w*|гражданк\w*|руководител\w*|'
    r'бухгалтер\w*|нотариус\w*|участник\w*|учредител\w*|акционер\w*|'
    r'заемщик\w*|заёмщик\w*|поручител\w*|наследник\w*|свидетел\w*|'
    r'потерпевш\w*|подсудим\w*|обвиняем\w*|третье\s+лицо|ИП|президент\w*|'
    r'управляющ\w*|ликвидатор\w*|арбитражн\w+\s+управляющ\w*|судья|адвокат\w*)'
    r'[\s:,\-–—_]*$', _I)
_TURKIC = re.compile(
    r'(?<![\wА-Яа-яЁё])((?:' + _CAP + r'\s+)?' + _CAP + r'\s+' + _CAP +
    r'\s+(?:оглы|кызы|гызы|улы|уулу))(?![\wА-Яа-яЁё])', _U)
_LAT_PATR = re.compile(
    r'(?<![A-Za-z])([A-Z][a-z]+\s+[A-Z][a-z]+\s+[A-Z][a-z]+(?:ovich|evich|ovna|evna|ichna|ich))(?![A-Za-z])')
_LAT_PAIR = re.compile(r'(?<![A-Za-z])([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})(?![A-Za-z])')


def _is_surn(w):
    return 'Surn' in _tags(w) and 'Geox' not in _tags(w)


def _is_name(w):
    return 'Name' in _tags(w)


def _persons(text: str) -> List[Hit]:
    hits = []
    words = [(m.start(), m.end(), m.group()) for m in _WORD_CAP.finditer(text)]
    i = 0
    while i < len(words):
        s, e, w = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        adjacent = nxt and re.fullmatch(r'\s+', text[e:nxt[0]])
        # Surname + Name (+ Patronymic) or Name + Surname
        if adjacent and ((_is_surn(w) and _is_name(nxt[2])) or (_is_name(w) and _is_surn(nxt[2])
                                                                and not _is_name(nxt[2]))):
            end = nxt[1]
            j = i + 2
            if j < len(words) and re.fullmatch(r'\s+', text[end:words[j][0]]) and 'Patr' in _tags(words[j][2]):
                end = words[j][1]
                j += 1
            hits.append(Hit(s, end, 'ФИО'))
            i = j
            continue
        # Surname + initials
        mi = _INIT1.match(text, e)
        if mi and _is_surn(w) and not re.match(r'[А-ЯЁа-яё]', text[mi.end():mi.end() + 1]):
            hits.append(Hit(s, mi.end(), 'ФИО'))
            i += 1
            continue
        # Lone surname after a role word
        if _is_surn(w) and not _is_name(w) and _ROLE_CTX.search(_left(text, s, 60)):
            hits.append(Hit(s, e, 'ФИО'))
        i += 1
    for m in _TURKIC.finditer(text):
        hits.append(Hit(m.start(1), m.end(1), 'ФИО'))
    for m in _LAT_PATR.finditer(text):
        hits.append(Hit(m.start(1), m.end(1), 'ФИО'))
    for m in _LAT_PAIR.finditer(text):
        a, b = _to_cyr(m.group(1)), _to_cyr(m.group(2))
        if (_is_name(a) and _is_surn(b)) or (_is_surn(a) and _is_name(b)):
            hits.append(Hit(m.start(1), m.end(2), 'ФИО'))
    return hits


# ── Entry point ──────────────────────────────────────────────────────────────

DETECTORS = [_passport, _phones, _grouped_numbers, _numeric, _swift, _birth,
             _cadastral, _other, _addresses, _orgs, _persons]


def find_all(text: str) -> List[Hit]:
    out = []
    for det in DETECTORS:
        out.extend(det(text))
    return out
