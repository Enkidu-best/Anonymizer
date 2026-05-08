"""
Entity detection — sequential pipeline:
  Pass 1 – OPF regex  (org names in quotes, both prefix and suffix OPF forms)
  Pass 2 – Structured regex (INN, OGRN, accounts, phones, addresses, etc.)
  Pass 3 – Natasha NER (finds PER/ORG missed by regex)
  Pass 4 – LLM via Ollama (optional, called once per document from handlers)

Token format: [FIO_1], [INN_2], [YUL_3] etc.
anonymize_text_sequential() returns (anonymized_text, replacements_dict).
"""

import re
import sys
import threading
from dataclasses import dataclass
from typing import List, Tuple, Dict

# ── pkg_resources polyfill ────────────────────────────────────────────────────
# pymorphy2 uses pkg_resources.WorkingSet + iter_entry_points.
# Broken in Python 3.12+ new setuptools and in PyInstaller frozen .exe.

def _make_pkg_resources_polyfill():
    import types
    import importlib.metadata as _m

    pkg = types.ModuleType('pkg_resources')

    def _iter_ep(group):
        # In frozen PyInstaller env distributions() returns nothing —
        # fall back to direct import of pymorphy2_dicts.
        if group == 'pymorphy2_dicts':
            try:
                import pymorphy2_dicts
                class _FakeEP:
                    name = 'pymorphy2_dicts'
                    def load(self):
                        return pymorphy2_dicts
                yield _FakeEP()
                return
            except ImportError:
                pass
        try:
            for dist in _m.distributions():
                eps = dist.entry_points
                if isinstance(eps, dict):
                    yield from eps.get(group, [])
                else:
                    yield from (ep for ep in eps if getattr(ep, 'group', '') == group)
        except Exception:
            pass

    class _WorkingSet:
        def __iter__(self):
            try:
                return iter(_m.distributions())
            except Exception:
                return iter([])
        def iter_entry_points(self, group):
            yield from _iter_ep(group)

    pkg.iter_entry_points    = _iter_ep
    pkg.WorkingSet           = _WorkingSet
    pkg.DistributionNotFound = Exception
    pkg.get_distribution     = lambda n: type('D', (), {
        'project_name': n, 'version': _m.version(n)})()
    return pkg


def _needs_polyfill() -> bool:
    if getattr(sys, 'frozen', False):   # always polyfill in PyInstaller .exe
        return True
    try:
        import pkg_resources
        pkg_resources.WorkingSet()
        list(pkg_resources.iter_entry_points('pymorphy2_dicts'))
        return False
    except Exception:
        return True


if _needs_polyfill():
    sys.modules['pkg_resources'] = _make_pkg_resources_polyfill()
    print('[PATCH] pkg_resources polyfill applied (pymorphy2 fix)')


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Entity:
    original:    str
    canonical:   str
    entity_type: str
    start:       int
    end:         int


def _p(pattern):
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


# ─────────────────────────────────────────────────────────────────────────────
# Token helpers
# ─────────────────────────────────────────────────────────────────────────────

TOKEN_INNER_RE = re.compile(
    r'\[(?:FIO|YUL|INN|OGRN|KPP|RS|KS|BIK|SNILS|PASSPORT|TEL|EMAIL|SWIFT|ADR|DOB'
    r'|ДАТАРОЖД|LIC|URL)_\d+\]'
)

def _wrap(token: str) -> str:
    return f'[{token}]'

def _is_bracketed_token(text: str) -> bool:
    return bool(TOKEN_INNER_RE.fullmatch(text.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# pymorphy2 name-tag filter for FIO
# Instead of a blocklist (which can never be complete), we use a POSITIVE check:
# a NER PER span is accepted as FIO only if pymorphy2 tags at least one word
# as a Russian proper name (Name/Patr/Surn), or it matches the Surname I.O. pattern.
# ─────────────────────────────────────────────────────────────────────────────

_pymorphy2_morph = None   # lazy-loaded MorphAnalyzer

def _get_pymorphy2():
    """Lazily initialize pymorphy2.MorphAnalyzer. Returns None on failure."""
    global _pymorphy2_morph
    if _pymorphy2_morph is None:
        try:
            import pymorphy2
            _pymorphy2_morph = pymorphy2.MorphAnalyzer()
        except Exception as e:
            print(f'[FIO] pymorphy2 unavailable: {e}')
            _pymorphy2_morph = False
    return _pymorphy2_morph if _pymorphy2_morph is not False else None


# ── Compiled patterns for name detection ─────────────────────────────────────

# Surname I.O. — unambiguously a person name
_FIO_INITIALS_RE = re.compile(
    r'[А-ЯЁ][а-яё]{1,}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.',
    re.UNICODE
)

# Patronymic suffixes — appear ONLY in Russian patronymics
# (works on all Python versions, no external library needed)
_PATRONYMIC_RE = re.compile(
    r'(?:ович|евич|овна|евна|ична|вна|вич)$',
    re.IGNORECASE | re.UNICODE
)


def _is_fio_span(text: str) -> bool:
    """
    Positive structural filter — return True only if the NER PER span
    looks like a real Russian person name.  No hardcoded blocklist.

    Three-layer check (any layer suffices):
      1. "Surname I.O." regex — unambiguous initials format.
      2. Any word ends in a Russian patronymic suffix
         (-ович/-евич/-овна/-евна/-ична/-вна/-вич).
         These endings appear EXCLUSIVELY in patronymics, never in
         role words like Принципал/Агент/Покупатель.
      3. pymorphy2 Name/Patr/Surn tag — most accurate, used when
         available (Python ≤ 3.12; unavailable on Python 3.14
         due to getargspec removal).
    """
    # Layer 1 — initials pattern
    if _FIO_INITIALS_RE.search(text):
        return True

    # Layer 2 — patronymic suffix (no library dependency)
    for word in re.findall(r'[А-ЯЁа-яё]{5,}', text):
        if _PATRONYMIC_RE.search(word):
            return True

    # Layer 3 — pymorphy2 morphological tags
    morph = _get_pymorphy2()
    if morph:
        for word in re.findall(r'[А-ЯЁа-яё]{3,}', text):
            try:
                for parse in morph.parse(word):
                    if parse.tag.grammemes & {'Name', 'Patr', 'Surn'}:
                        return True
            except Exception:
                pass

    return False


# ─────────────────────────────────────────────────────────────────────────────
# OPF patterns (full Russian Wikipedia list)
# Captures ONLY the inner name; OPF prefix/suffix stays in text.
# Result: "ООО «Новые Выставки»"  →  "ООО «[YUL_1]»"
# ─────────────────────────────────────────────────────────────────────────────

_OPF_FULL = (
    r'(?:'
    # ── Коммерческие общества и товарищества ─────────────────────────────────
    r'общест\w+\s+с\s+ограниченной\s+ответственност\w+|'
    r'общест\w+\s+с\s+дополнительной\s+ответственност\w+|'
    r'публичн\w+\s+акционерн\w+\s+общест\w+|'
    r'непубличн\w+\s+акционерн\w+\s+общест\w+|'
    r'закрыт\w+\s+акционерн\w+\s+общест\w+|'
    r'открыт\w+\s+акционерн\w+\s+общест\w+|'
    r'акционерн\w+\s+общест\w+|'
    r'полн\w+\s+товариществ\w+|'
    r'товариществ\w+\s+на\s+вер\w+|'
    r'крестьянск\w+\s+(?:фермерск\w+\s+)?хозяйств\w+|'
    r'хозяйственн\w+\s+партнерств\w+|'
    r'производственн\w+\s+кооператив\w+|'
    r'потребительск\w+\s+кооператив\w+|'
    r'инвестиционн\w+\s+товариществ\w+|'
    r'простое\s+товариществ\w+|'
    r'индивидуальн\w+\s+предпринимател\w+|'
    # ── Унитарные предприятия ─────────────────────────────────────────────────
    r'федеральн\w+\s+государственн\w+\s+унитарн\w+\s+предприяти\w+|'
    r'государственн\w+\s+(?:областн\w+\s+|унитарн\w+\s+)?унитарн\w+\s+предприяти\w+|'
    r'муниципальн\w+\s+унитарн\w+\s+предприяти\w+|'
    # ── Некоммерческие организации ────────────────────────────────────────────
    r'автономн\w+\s+некоммерческ\w+\s+организаци\w+|'
    r'некоммерческ\w+\s+партнерств\w+|'
    r'общественн\w+\s+организаци\w+|'
    r'общественн\w+\s+объединени\w+|'
    r'общественн\w+\s+движени\w+|'
    r'государственн\w+\s+корпораци\w+|'
    r'политическ\w+\s+парти\w+|'
    r'профессиональн\w+\s+союз\w+|'
    r'(?:благотворительн\w+\s+)?фонд\s|'
    r'ассоциаци\w+(?:\s+и\s+союз\w+)?|'
    r'объединени\w+\s+юридических\s+лиц|'
    r'казачь\w+\s+общест\w+|'
    r'территориальн\w+\s+общественн\w+\s+самоуправлени\w+|'
    # ── Товарищества собственников / СНТ ────────────────────────────────────
    r'товариществ\w+\s+собственников\s+(?:недвижимост\w+|жиль\w+)|'
    r'садовод\w+\s+некоммерческ\w+\s+товариществ\w+|'
    r'огородническ\w+\s+некоммерческ\w+\s+товариществ\w+|'
    r'дачн\w+\s+некоммерческ\w+\s+товариществ\w+|'
    # ── Учреждения (государственные, муниципальные, образовательные) ─────────
    r'федеральн\w+\s+государственн\w+\s+автономн\w+\s+'
        r'(?:образовательн\w+\s+)?учрежден\w+|'
    r'федеральн\w+\s+государственн\w+\s+бюджетн\w+\s+'
        r'(?:научн\w+\s+|образовательн\w+\s+)?учрежден\w+|'
    r'федеральн\w+\s+государственн\w+\s+казенн\w+\s+учрежден\w+|'
    r'федеральн\w+\s+государственн\w+\s+учрежден\w+|'
    r'федеральн\w+\s+казенн\w+\s+учрежден\w+|'
    r'государственн\w+\s+(?:областн\w+\s+|бюджетн\w+\s+)?учрежден\w+|'
    r'муниципальн\w+\s+(?:бюджетн\w+\s+)?(?:казенн\w+\s+)?'
        r'(?:общеобразовательн\w+\s+|дошкольн\w+\s+)?учрежден\w+|'
    r'государственн\w+\s+(?:бюджетн\w+\s+)?учрежден\w+\s+'
        r'(?:культур\w+|здравоохранени\w+|образовани\w+)|'
    # ── Коммерческий банк ─────────────────────────────────────────────────────
    r'коммерческ\w+\s+банк\w+'
    r')'
)

_OPF_SHORT = (
    r'(?:ООО|ПАО|НАО|ЗАО|ОАО|АО|ОДО|'
    r'ПТ|ТНВ|КТ|КФХ|ХП|ПК|ПотК|'
    r'ГУП|МУП|ФГУП|'
    r'АНО|НП|НКО|ГК|КБ|ИП|'
    r'ТСЖ|СНТ|ОНТ|ДНТ|'
    r'ФГУ|ФГАУ|ФГБУ|ФГКУ|ФКУ|'
    r'ГБУ|ГКУ|ОГУ|МКУ|ФГАОУ)'
)

_OPF_PFX = (
    r'(?:' + _OPF_FULL + r'|' + _OPF_SHORT + r')'
    r'\s+(?:(?:' + _OPF_FULL + r'|' + _OPF_SHORT + r')\s+)?'
)

# Prefix forms: OPF «name» or OPF "name"  (most common in Russian docs)
# Angle quotes: «x never contains », so [^»\n]+ already handles inner «sub» names.
# Straight quotes: inner group allows ONE nested quote for  ООО "МЗ "Ревякино"  →  МЗ "Ревякино
_OPF_RE_ANGLE = _p(
    r'(' + _OPF_PFX + r'«)'
    r'([^»\n]{2,80})'
    r'(»)'
)
_OPF_RE_STRAIGHT = _p(
    r'(' + _OPF_PFX + r'[""])'
    r'([^""\n]{2,60}(?:[""][А-ЯЁа-яёA-Za-z0-9\s\-\.«»]{1,30})?)'
    r'([""])'
)

# Suffix forms: «name» (OPF) or "name" (OPF) — OPF in parentheses after name.
# Matches both short abbreviations (АО, ООО…) and full-form OPF names
# (акционерное общество, публичное акционерное общество, etc.)
_OPF_SUFFIX_SHORT = r'(?:' + _OPF_SHORT + r')'
_OPF_SUFFIX_ANY = r'(?:' + _OPF_SHORT + r'|' + _OPF_FULL + r')'
_OPF_RE_ANGLE_SFX = _p(
    r'«([^»\n]{2,80})»'
    r'\s*\(' + _OPF_SUFFIX_ANY + r'\)'
)
_OPF_RE_STRAIGHT_SFX = _p(
    r'[""]([^""\n]{2,80})[""]'
    r'\s*\(' + _OPF_SUFFIX_ANY + r'\)'
)

# Inner OPF form: «ООО Name» — OPF abbreviation is INSIDE the angle quotes.
# Replaces only the Name (group 2), keeping the OPF visible: «ООО [YUL_N]».
_OPF_RE_INNER_ANGLE = _p(
    r'«(' + _OPF_SHORT + r')\s+'
    r'([А-ЯЁа-яёA-Za-z\d][А-ЯЁа-яё\w\-\s"]{1,60}?)»'
)


def _apply_opf_replacements(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    """
    Find OPF+«name» (prefix form) and «name»+(OPF) (suffix form).
    Replace only the inner name with [YUL_N]; OPF stays in text.
    """
    from core.db import get_or_create_token

    all_matches = []
    # Prefix patterns: groups are (prefix, inner, suffix_char) → inner = group 2
    for pattern in (_OPF_RE_ANGLE, _OPF_RE_STRAIGHT):
        for m in pattern.finditer(text):
            inner = m.group(2).strip()
            if len(inner) < 2 or _is_bracketed_token(inner):
                continue
            all_matches.append((m.start(2), m.end(2), inner))

    # Suffix patterns: group 1 = inner name
    for pattern in (_OPF_RE_ANGLE_SFX, _OPF_RE_STRAIGHT_SFX):
        for m in pattern.finditer(text):
            inner = m.group(1).strip()
            if len(inner) < 2 or _is_bracketed_token(inner):
                continue
            all_matches.append((m.start(1), m.end(1), inner))

    # Inner OPF: «ООО Name» — OPF inside the quotes; replace only Name (group 2)
    for m in _OPF_RE_INNER_ANGLE.finditer(text):
        inner = m.group(2).strip()
        if len(inner) < 2 or _is_bracketed_token(inner):
            continue
        all_matches.append((m.start(2), m.end(2), inner))

    if not all_matches:
        return text, {}

    all_matches.sort(key=lambda x: x[0])
    filtered, last_end = [], -1
    for start, end, inner in all_matches:
        if start >= last_end:
            filtered.append((start, end, inner))
            last_end = end

    replacements: Dict[str, str] = {}
    result, last = [], 0
    for start, end, inner in filtered:
        token = _wrap(get_or_create_token(db_path, session_id, inner, 'ЮЛ'))
        replacements[inner] = token
        result.append(text[last:start])
        result.append(token)
        last = end

    result.append(text[last:])
    new_text = ''.join(result)
    if replacements:
        print(f'[OPF] {len(replacements)} org name(s) replaced')
    return new_text, replacements


# ─────────────────────────────────────────────────────────────────────────────
# Structured regex patterns
# ─────────────────────────────────────────────────────────────────────────────

_NUM = r'(?:№|No\.?|N)\s*'

_MONTHS_RU = (
    r'(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|'
    r'июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)'
)

_ADR_KW = (
    r'(?:адрес(?:у|е)?'
    r'|(?:проживает|зарегистрирован\w*)\s+по\s+адресу'
    r'|местонахождени[яею]?'
    r'|место\s+(?:нахождени[яею]?|жительств\w+|регистраци\w+)'
    r'|(?:юридическ|фактическ|почтов)\w+\s+адрес\w*'
    r'|адрес\s+(?:юридическ|физическ|места\s+нахождени[ея])\w*'
    r'|(?:место\s+)?регистраци[иейю]\w*\s+(?:по\s+)?адрес\w*)'
    r'\s*[:\-]?\s*'
)

REGEX_PATTERNS: List[Tuple[str, list]] = [
    ('ИНН', [
        (_p(r'ИНН\s*[:=\-]?\s*(\d{10}|\d{12})\b'), 1),
        (_p(r'ИНН\s*/\s*(?:КПП|ОГРН)\s*[:=]?\s*(\d{10}|\d{12})\s*/'), 1),
    ]),
    ('ОГРН', [
        (_p(r'ОГРНИП\s*[:=]?\s*(\d{15})\b'), 1),
        (_p(r'ОГРН\s*[:=]?\s*(\d{13})\b'), 1),
        (_p(r'основной\s+(?:государственный\s+)?регистрационный\s+номер\s*[:№=]?\s*(\d{13,15})\b'), 1),
        (_p(r'(?:ИНН|КПП)\s*/\s*ОГРН\s*[:=]?\s*\d{9,12}\s*/\s*(\d{13,15})\b'), 1),
        (_p(r'(?<!\d)([15]\d{12})(?!\d)'), 1),
        (_p(r'(?<!\d)(3\d{14})(?!\d)'), 1),
    ]),
    ('КПП', [
        (_p(r'КПП\s*[:=]?\s*(\d{9})\b'), 1),
        (_p(r'ИНН\s*/\s*КПП\s*[:=]?\s*(?:\d{10}|\d{12})\s*/\s*(\d{9})\b'), 1),
    ]),
    ('РС', [
        (_p(
            r'(?:р(?:асч)?\.?\s*/\s*с(?:ч(?:[ёе]т)?)?\b|расч[ёе]тн\w*\s+сч[ёе]т\w*)'
            r'\s*[:=]?\s*' + _NUM + r'?(\d{20})\b'
        ), 1),
        # Fallback: standalone 20-digit settlement account (label may be in different cell)
        # Settlement accounts always start with 4[012]
        (_p(r'(?<!\d)(4[012]\d{18})(?!\d)'), 1),
    ]),
    ('КС', [
        (_p(
            r'(?:к(?:ор(?:р)?)?\.?\s*/\s*с(?:ч(?:[ёе]т)?)?\b|'
            r'корр?\.\s*сч[ёе]т\w*|корреспондентск\w+\s+сч[ёе]т\w*)'
            r'\s*[:=]?\s*' + _NUM + r'?(\d{20})\b'
        ), 1),
        # Fallback: standalone 20-digit correspondent account (always starts with 301)
        (_p(r'(?<!\d)(30[1-9]\d{17})(?!\d)'), 1),
    ]),
    ('БИК', [
        (_p(r'БИК\s*[:=]?\s*(\d{9})\b'), 1),
    ]),
    ('СНИЛС', [
        (_p(r'\b(\d{3}-\d{3}-\d{3}\s+\d{2})\b'), 1),
        (_p(r'СНИЛС\s*[:=]?\s*(\d{11})\b'), 1),
    ]),
    ('ПАСПОРТ', [
        (_p(r'паспорт\w*[\s:;]+(?:сери[яи]\s+)?(\d{2}\s*\d{2})\s*,?\s*(?:' + _NUM + r')?(\d{6})\b'), 0),
        (_p(r'сери[яи]\s+(\d{2}\s+\d{2})[,;\s]+(?:' + _NUM + r')(\d{6,9})\b'), 0),
        (_p(r'сери[яи]\s+(\d{2,4})\s+(?:' + _NUM + r')(\d{6,9})\b'), 0),
    ]),
    ('ТЕЛЕФОН', [
        (_p(r'(?:тел[ефон.:\s]*\.?|моб\.?\s*[:\s]|факс\s*[:\s])\s*'
           r'(\+?[78]?[\s\-\(]?\d{3}[\s\-\)\.]\s*\d{3}[\s\-\.]\d{2}[\s\-\.]\d{2})\b'), 1),
        (_p(r'(?<!\d)(\+7[\s\-\(]?\d{3}[\s\-\)\.]\s*\d{3}[\s\-\.]\d{2}[\s\-\.]\d{2})\b'), 1),
        (_p(r'(?<!\d)(8[\s\-\(]\d{3}[\s\-\)\.]\s*\d{3}[\s\-\.]\d{2}[\s\-\.]\d{2})\b'), 1),
        (_p(r'(?<!\d)([78][3-9]\d{9})(?!\d)'), 1),
        (_p(r'(?<!\d)(9[0-9]\d{8})(?!\d)'), 1),
    ]),
    ('EMAIL', [
        (_p(r'\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b'), 1),
    ]),
    # АДРЕС — patterns, each requires a keyword or strong positional anchor
    ('АДРЕС', [
        # A: keyword + 6-digit postcode
        (_p(_ADR_KW + r'(\d{6}[,\s]+[\wА-ЯЁа-яё\s\.,\-/№«»"]{10,350})'), 1),
        # B: keyword + city name (г. City, ...)
        (_p(_ADR_KW +
            r'((?:(?:Р(?:оссийская\s+)?Федерация|РФ)[,\s]+)?'
            r'г(?:ород)?\.?\s+[\w\-]{2,30}[,\s]+'
            r'[\wА-ЯЁа-яё\s\.,\-/№]{5,350})'), 1),
        # C: keyword + street type + house number
        (_p(_ADR_KW +
            r'((?:ул(?:ица)?|пр(?:оспект)?|пер(?:еулок)?|бул(?:ьвар)?'
            r'|наб(?:ережная)?|ш(?:оссе)?|пл(?:ощадь)?)\.?\s+'
            r'[\wА-ЯЁа-яё\s\.\-]{2,50}[,\s]+д(?:ом)?\.?\s*[\w/]+'
            r'[\wА-ЯЁа-яё\s\.,\-/№]{0,100})'), 1),
        # D: standalone 6-digit postcode + city abbreviation (no keyword needed)
        # Г.МОСКВА / г. Москва / ГОР. ПЕРМЬ — reliably identifies an address block
        (_p(
            r'(?<!\d)(\d{6}[,\s]{1,5}'
            r'(?:[А-ЯЁа-яёA-Za-z\-]{2,30}[.,]?\s+)?'   # optional country/region
            r'(?:[Гг]\.?\s*|[Гг][Оо][Рр]\.?\s+)'        # г. / ГОР.
            r'[А-ЯЁа-яё][А-ЯЁа-яё\-]{1,29}'             # city name
            r'[,\s][\wА-ЯЁа-яёA-Za-z\s,\.\-/№«»"]{15,350}?)'
            r'(?=\s*[\n\r]|\s*$)'
        ), 1),
        # E: keyword + region/oblast/krai + city + rest (address with region first)
        # Matches: "зарегистрированная по адресу: Ростовская область, г. Ростов-на-Дону, ..."
        # Uses [^\n] so the match stops at the line boundary (doesn't eat following text).
        (_p(_ADR_KW +
            r'([А-ЯЁа-яё][А-ЯЁа-яё\w ]{1,30}'
            r'(?:область|край|республика|округ)[а-яё]*'
            r'[,\s]+'
            r'[^\n]{10,300})'), 1),
    ]),
    ('SWIFT', [
        (_p(r'SWIFT\s*[-:]?\s*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b'), 1),
    ]),
    # ФИО — structural regex patterns (do not depend on NER / Natasha)
    # Pattern A: Full FIO  Surname Firstname Patronymic  (patronymic ending is the key signal)
    # Pattern B: Surname I.O.  (initials format)
    # Pattern C: Signature  / Surname I.O. /  or  / Surname Firstname Patronymic /
    ('ФИО', [
        # A — full FIO: 3 capitalised Russian words, 3rd ends in patronymic suffix
        (_p(
            r'(?<![А-ЯЁа-яё])'
            r'([А-ЯЁ][а-яё]{1,20}'           # Surname
            r'\s+[А-ЯЁ][а-яё]{1,15}'          # Firstname
            r'\s+[А-ЯЁ][а-яё]*'               # Patronymic start
            r'(?:ович|евич|овна|евна|ична)[а-яё]*)'  # Patronymic ending
            r'(?![А-ЯЁа-яё])'
        ), 1),
        # B — Surname + two initials  (Иванов А. А.  or  Иванов А.А.)
        # Verb-ending filter applied in _apply_regex_replacements for this type.
        (_p(
            r'(?<![А-ЯЁа-яё])'
            r'([А-ЯЁ][а-яё]{2,20}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)'
            r'(?![А-ЯЁа-яё])'
        ), 1),
        # C — signature block  / Surname I.O. /  or  / Surname Firstname Patronymic /
        (_p(
            r'/\s*'
            r'([А-ЯЁ][а-яё]{1,20}'            # Surname
            r'(?:\s+[А-ЯЁ][а-яё]{1,15}'       # optional Firstname
            r'(?:\s+[А-ЯЁ][а-яё]*'            # optional Patronymic
            r'(?:ович|евич|овна|евна|ична)[а-яё]*)?)?'
            r'(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)?)'  # or initials
            r'\s*/'
        ), 1),
        # D — initials first: И.О. Фамилия  (e.g. "С.В. Сердитов", "А.Р. Чуйко")
        # IMPORTANT: compiled WITHOUT re.IGNORECASE so that г.о./т.ч. (lowercase)
        # cannot satisfy the [А-ЯЁ] uppercase character class.
        (re.compile(
            r'(?<![А-ЯЁа-яёA-Za-z.])'
            r'([А-ЯЁ]\.\s*[А-ЯЁ]\.\s+[А-ЯЁ][а-яё]{2,25})'
            r'(?![А-ЯЁа-яё])',
            re.UNICODE
        ), 1),
    ]),
    ('ДАТАРОЖД', [
        (_p(r'\b(\d{1,2}\s+' + _MONTHS_RU + r'\s+\d{4})\s+(?:года?\s+рожд\w+|рожд\w+)'), 1),
        (_p(r'(?:рожд[ёе]н\w*\s+)(\d{1,2}\s+' + _MONTHS_RU + r'\s+\d{4})\b'), 1),
        (_p(r'дата\s+рождени\w+\s*[:\s]\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})\b'), 1),
    ]),
    # ЦБ licence numbers  (Лицензия ЦБ РФ № 2721)
    ('ЛИЦЕНЗИЯ', [
        (_p(r'лицензи[яию]\w*\s+(?:цб\s+рф|банка\s+росси\w+|центральн\w+\s+банк\w+)'
            r'\s*(?:' + _NUM + r')?(\d{3,6})\b'), 1),
        (_p(r'цб\s+рф\s+лицензи[яию]\w*\s*(?:' + _NUM + r')?(\d{3,6})\b'), 1),
    ]),
    # Website URLs
    ('URL', [
        (_p(r'((?:https?://|www\.)[A-Za-zА-ЯЁа-яё0-9\-\.]+\.[A-Za-z]{2,10}'
            r'(?:/[^\s,;)»"\'<>\n]{0,200})?)'), 1),
    ]),
]


# ── FIO validation helpers ────────────────────────────────────────────────────

# Russian 3rd-person singular present-tense verb endings that look like surnames.
# E.g. "Возглавляет" ends in "яет" → not a surname.
_FIO_VERB_END_RE = re.compile(
    r'(?:ает|яет|ует|вает|зает|жает|щает|тает|нает|'
    r'ляет|ряет|бает|пает|дает|лает|кает|мает|гает|чает|хает)\s*$',
    re.IGNORECASE | re.UNICODE,
)

# Common Russian role/title/position word endings that appear before initials
# but are NOT surnames.  E.g. "Директор А.Б." → Директор ends in "-тор".
# This prevents Pattern B (Surname I.O.) from matching "Директор А.Б." as FIO.
_FIO_NON_SURNAME_END_RE = re.compile(
    r'(?:тор|тель|ник|щик|ист|мен|нт|ент|нер|гер|ор|ер|ль|ик|ек|ок|ач|ич)\s*$',
    re.UNICODE,   # case-sensitive — matches only lowercase endings (role words are
)                 # never all-caps), leaves uppercase surnames like "ИВАНОВ" unaffected

# Trailing punctuation to strip from captured FIO values
_FIO_TRAIL_RE = re.compile(r'[\s.,;:!?)»"\'—\-]+$', re.UNICODE)


def _validate_fio_regex(text: str) -> bool:
    """
    Return False if the first word of a FIO match looks like a Russian verb or
    a common role/title/position word (not a surname).
    """
    words = text.split()
    if not words:
        return False
    first = words[0]
    # Reject verb-ending first word
    if _FIO_VERB_END_RE.search(first):
        return False
    # Reject role/title word ending (e.g. Директор, Менеджер, Начальник, ...)
    # Only apply when the match has format "Word I.O." (exactly 2 tokens: word + initials)
    if len(words) == 2 and _FIO_NON_SURNAME_END_RE.search(first):
        return False
    return True


def _apply_regex_replacements(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    from core.db import get_or_create_token

    # matches: (start, end, canonical_value, entity_type, original_text_in_doc)
    # canonical_value — stored in DB (groups joined by space, no separators)
    # original_text_in_doc — actual substring to replace in text
    #   For grp==0 patterns (e.g. ПАСПОРТ), these differ:
    #   canonical = "60 17 062138"  but  original = "паспорт серия 60 17№ 062138"
    matches = []
    used    = []

    for entity_type, patterns in REGEX_PATTERNS:
        for pat, grp in patterns:
            for m in pat.finditer(text):
                if grp == 0:
                    groups = [g for g in m.groups() if g]
                    if not groups:
                        continue
                    # Canonical form: groups joined (the actual private values)
                    value = ' '.join(g.strip() for g in groups)
                    # Original text in the document (includes keyword + separators)
                    orig_text = m.group(0)
                    s, e  = m.start(), m.end()
                else:
                    try:
                        value = (m.group(grp) or '').strip()
                        orig_text = value      # same for grp != 0
                        s, e  = m.start(grp), m.end(grp)
                    except IndexError:
                        continue

                if not value:
                    continue
                if entity_type == 'АДРЕС' and len(value) < 10:
                    continue

                # ФИО-specific validation & cleanup
                if entity_type == 'ФИО':
                    # Strip trailing punctuation (e.g. "А.Р. ЧУЙКО)" → "А.Р. ЧУЙКО")
                    stripped = _FIO_TRAIL_RE.sub('', value)
                    if stripped != value:
                        e -= len(value) - len(stripped)
                        value = stripped
                        orig_text = _FIO_TRAIL_RE.sub('', orig_text)
                    if not value:
                        continue
                    # Reject if first word looks like a verb or role title
                    if not _validate_fio_regex(value):
                        continue

                if any(not (e <= us or s >= ue) for us, ue in used):
                    continue

                matches.append((s, e, value, entity_type, orig_text))
                used.append((s, e))

    if not matches:
        return text, {}

    matches.sort(key=lambda x: x[0])
    replacements = {}
    for _, _, value, etype, orig_text in matches:
        if orig_text not in replacements:
            token = _wrap(get_or_create_token(db_path, session_id, value, etype))
            replacements[orig_text] = token

    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)

    if replacements:
        by_type: Dict[str, int] = {}
        for _, _, _, etype, _ in matches:
            by_type[etype] = by_type.get(etype, 0) + 1
        print(f'[REGEX] {len(replacements)} entities: ' +
              ', '.join(f'{k}={v}' for k, v in sorted(by_type.items())))
    return text, replacements


# ─────────────────────────────────────────────────────────────────────────────
# Natasha NER
# ─────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_ner_ready = _ner_loading = False
_ner_error = None
_segmenter = _morph_vocab = _morph_tagger = _ner_tagger = None


def get_ner_status() -> dict:
    return {
        'ready':   _ner_ready,
        'loading': _ner_loading,
        'error':   _ner_error,
        'python':  sys.version,
    }


def _do_load():
    global _ner_ready, _ner_loading, _ner_error
    global _segmenter, _morph_vocab, _morph_tagger, _ner_tagger
    try:
        from natasha import (Segmenter, MorphVocab, NewsEmbedding,
                             NewsMorphTagger, NewsNERTagger)
        _segmenter    = Segmenter()
        _morph_vocab  = MorphVocab()
        emb           = NewsEmbedding()
        _morph_tagger = NewsMorphTagger(emb)
        _ner_tagger   = NewsNERTagger(emb)
        _ner_ready    = True
        _ner_error    = None
        print('[NER] Natasha loaded successfully')
    except Exception as ex:
        import traceback
        _ner_error = str(ex)
        print(f'[NER] LOAD ERROR: {ex}\n{traceback.format_exc()}')
    finally:
        _ner_loading = False


def start_ner_loading():
    global _ner_loading
    with _lock:
        if _ner_ready or _ner_loading:
            return
        _ner_loading = True
    threading.Thread(target=_do_load, daemon=True).start()


def retry_ner_loading():
    global _ner_loading, _ner_error, _ner_ready
    with _lock:
        if _ner_loading or _ner_ready:
            return
        _ner_error = None
        _ner_loading = True
    threading.Thread(target=_do_load, daemon=True).start()


def _apply_ner_replacements(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    """
    Run Natasha NER for FIO (PER) only.

    ORG spans are NOT processed here — organizations are detected exclusively
    by OPF-based regex in _apply_opf_replacements().  Natasha's ORG tagger
    produces too many false positives in Russian legal text (common nouns,
    document section headers, role words tagged as ORG).

    For PER spans, only accept the span if _is_fio_span() returns True:
    the span must contain a word tagged by pymorphy2 as Name/Patr/Surn, or
    match the Surname I.O. initials pattern.  This avoids any hardcoded
    blocklist — the filter is structural/morphological.
    """
    if not _ner_ready:
        return text, {}

    from natasha import Doc
    from core.db import get_or_create_token

    try:
        doc = Doc(text)
        doc.segment(_segmenter)
        doc.tag_morph(_morph_tagger)
        doc.tag_ner(_ner_tagger)
    except Exception as ex:
        print(f'[NER] Processing error: {ex}')
        return text, {}

    token_spans = [(m.start(), m.end()) for m in TOKEN_INNER_RE.finditer(text)]

    replacements = {}
    skipped = 0
    for span in doc.spans:
        if span.type != 'PER':          # ORG spans are skipped — YUL only from OPF
            continue

        s, e = span.start, span.stop
        original = span.text.strip()
        # Strip trailing punctuation that Natasha sometimes includes in a PER span
        original = _FIO_TRAIL_RE.sub('', original)

        if any(not (e <= ts or s >= te) for ts, te in token_spans):
            continue
        if len(original) < 3 or _is_bracketed_token(original):
            continue

        # Structural / morphological filter — no blocklist needed
        if not _is_fio_span(original):
            skipped += 1
            continue

        try:
            span.normalize(_morph_vocab)
            canon = span.normal
        except Exception:
            canon = original

        if original not in replacements:
            token = _wrap(get_or_create_token(db_path, session_id, canon, 'ФИО'))
            replacements[original] = token

    if replacements or skipped:
        print(f'[NER] {len(replacements)} FIO found'
              + (f', {skipped} non-name spans filtered' if skipped else ''))

    if replacements:
        for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
            text = text.replace(orig, tok)

    return text, replacements


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def _apply_known_entities(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    """
    Final pass: scan existing DB mappings against the current text and replace
    any canonical_form values that are still present.  This handles:
    - Entities manually added to the DB by the user
    - Entities found in previous documents in the same session
    Re-running the original file will correctly tokenize everything in the DB.
    """
    from core.db import get_session_mappings

    mappings = get_session_mappings(db_path, session_id)
    if not mappings:
        return text, {}

    replacements = {}
    for m in sorted(mappings, key=lambda x: -len(x['canonical_form'])):
        canonical = m['canonical_form']
        token = f"[{m['token']}]"
        if canonical in text and not _is_bracketed_token(canonical):
            replacements[canonical] = token

    if not replacements:
        return text, {}

    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)

    print(f'[KNOWN] {len(replacements)} existing entities applied')
    return text, replacements


def anonymize_text_sequential(text: str, db_path, session_id: str,
                               use_llm: bool = False,
                               use_regex_ner: bool = True) -> Tuple[str, Dict[str, str]]:
    """
    Passes 1-3 (+ optional LLM pass 4 when use_llm=True).
    Set use_regex_ner=False to skip passes 1-3 and run only LLM (for comparison).
    For DOCX/PDF, handlers call this per paragraph with use_llm=False,
    then run apply_llm_pass once on the full document separately.
    """
    all_reps: Dict[str, str] = {}

    if use_regex_ner:
        text, reps1 = _apply_opf_replacements(text, db_path, session_id)
        all_reps.update(reps1)

        text, reps2 = _apply_regex_replacements(text, db_path, session_id)
        all_reps.update(reps2)

        text, reps3 = _apply_ner_replacements(text, db_path, session_id)
        all_reps.update(reps3)

    if use_llm:
        try:
            from core.llm import apply_llm_pass
            # full_scan=True when regex/NER skipped (LLM-only mode) so LLM covers
            # ALL entity types including INN, OGRN, accounts, phones, emails, etc.
            text, reps4 = apply_llm_pass(text, db_path, session_id,
                                          full_scan=not use_regex_ner)
            all_reps.update(reps4)
        except Exception as ex:
            print(f'[LLM] Pass failed: {ex}')

    # Final pass: apply known DB entries (handles manual additions + re-runs)
    text, reps_known = _apply_known_entities(text, db_path, session_id)
    all_reps.update(reps_known)

    return text, all_reps


def apply_reverse(text: str, reverse_map: dict) -> str:
    """Replace [TOKEN] → original. Also handles bare TOKEN for backward compat."""
    if not reverse_map:
        return text
    combined = {}
    for tok, orig in reverse_map.items():
        combined[f'[{tok}]'] = orig
        combined[tok]        = orig
    for token, orig in sorted(combined.items(), key=lambda x: -len(x[0])):
        text = text.replace(token, orig)
    return text
