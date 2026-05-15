"""
Entity detection — sequential pipeline:
  Pass 1 – OPF regex  (org names in quotes, both prefix and suffix OPF forms)
  Pass 2 – Structured regex (INN, OGRN, accounts, phones, addresses, etc.)
  Pass 3 – spaCy NER (finds PER/ORG missed by regex)
  Pass 4 – LLM via Ollama (optional)

Token format: [FIO_1], [INN_2], [YUL_3] etc.
anonymize_text_pipeline() returns (anonymized_text, replacements_dict).
"""

import re
import sys
import threading
from typing import Tuple, Dict, List

from core.db import get_or_create_token, get_session_mappings, get_top_patterns


# ─────────────────────────────────────────────────────────────────────────────
# spaCy loading
# ─────────────────────────────────────────────────────────────────────────────

_nlp = None
_ner_ready = False
_ner_loading = False
_ner_error = None
_lock = threading.Lock()


def _do_load():
    global _nlp, _ner_ready, _ner_error, _ner_loading
    try:
        import traceback
        import spacy
        if not spacy.util.is_package('ru_core_news_lg'):
            raise RuntimeError(
                "Модель ru_core_news_lg не установлена. "
                "Выполните: python -m spacy download ru_core_news_lg"
            )
        _nlp = spacy.load("ru_core_news_lg")
        _ner_ready = True
        _ner_error = None
        print("[NER] spaCy ru_core_news_lg loaded successfully")
    except Exception as e:
        import traceback as _tb
        _ner_error = str(e)
        print(f"[NER] ERROR: {e}\n{_tb.format_exc()}")
    finally:
        _ner_loading = False


def get_ner_status() -> dict:
    return {
        'ready':   _ner_ready,
        'loading': _ner_loading,
        'error':   _ner_error,
    }


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


# ─────────────────────────────────────────────────────────────────────────────
# Token helpers
# ─────────────────────────────────────────────────────────────────────────────

def _p(pattern):
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


TOKEN_INNER_RE = re.compile(
    r'\[(?:FIO|YUL|INN|OGRN|KPP|RS|KS|BIK|SNILS|PASSPORT|TEL|EMAIL|SWIFT|ADR|DOB'
    r'|ДАТАРОЖД|LIC|URL)_\d+\]'
)

# Matches a bracketed token anywhere in a string (used to detect partial masks)
ANY_TOKEN_RE = re.compile(r'\[[A-Z_А-ЯЁ]+_\d+\]')


def _wrap(token: str) -> str:
    return f'[{token}]'

def _is_bracketed_token(text: str) -> bool:
    return bool(TOKEN_INNER_RE.fullmatch(text.strip()))

def _contains_token(text: str) -> bool:
    """True if text contains any [TYPE_N] mask — such matches should be skipped."""
    return bool(ANY_TOKEN_RE.search(text))


# ─────────────────────────────────────────────────────────────────────────────
# OPF patterns (full Russian Wikipedia list)
# Captures ONLY the inner name; OPF prefix/suffix stays in text.
# Result: "ООО «Новые Выставки»"  →  "ООО «[YUL_1]»"
# ─────────────────────────────────────────────────────────────────────────────

_OPF_FULL = (
    r'(?:'
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
    r'федеральн\w+\s+государственн\w+\s+унитарн\w+\s+предприяти\w+|'
    r'государственн\w+\s+(?:областн\w+\s+|унитарн\w+\s+)?унитарн\w+\s+предприяти\w+|'
    r'муниципальн\w+\s+унитарн\w+\s+предприяти\w+|'
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
    r'товариществ\w+\s+собственников\s+(?:недвижимост\w+|жиль\w+)|'
    r'садовод\w+\s+некоммерческ\w+\s+товариществ\w+|'
    r'огородническ\w+\s+некоммерческ\w+\s+товариществ\w+|'
    r'дачн\w+\s+некоммерческ\w+\s+товариществ\w+|'
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

_OPF_RE_INNER_ANGLE = _p(
    r'«(' + _OPF_SHORT + r')\s+'
    r'([А-ЯЁа-яёA-Za-z\d][А-ЯЁа-яё\w\-\s"]{1,60}?)»'
)


def _apply_opf_pass(text: str, db_path, session_id: str,
                    exclusions: set = None) -> Tuple[str, Dict[str, str]]:
    all_matches = []
    for pattern in (_OPF_RE_ANGLE, _OPF_RE_STRAIGHT):
        for m in pattern.finditer(text):
            inner = m.group(2).strip()
            if len(inner) < 2 or _is_bracketed_token(inner) or _contains_token(inner):
                continue
            all_matches.append((m.start(2), m.end(2), inner))

    for pattern in (_OPF_RE_ANGLE_SFX, _OPF_RE_STRAIGHT_SFX):
        for m in pattern.finditer(text):
            inner = m.group(1).strip()
            if len(inner) < 2 or _is_bracketed_token(inner) or _contains_token(inner):
                continue
            all_matches.append((m.start(1), m.end(1), inner))

    for m in _OPF_RE_INNER_ANGLE.finditer(text):
        inner = m.group(2).strip()
        if len(inner) < 2 or _is_bracketed_token(inner) or _contains_token(inner):
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
        if exclusions and (inner, 'ЮЛ') in exclusions:
            continue
        token = _wrap(get_or_create_token(db_path, session_id, inner, inner, 'ЮЛ'))
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
        (_p(r'(?<!\d)(4[012]\d{18})(?!\d)'), 1),
    ]),
    ('КС', [
        (_p(
            r'(?:к(?:ор(?:р)?)?\.?\s*/\s*с(?:ч(?:[ёе]т)?)?\b|'
            r'корр?\.\s*сч[ёе]т\w*|корреспондентск\w+\s+сч[ёе]т\w*)'
            r'\s*[:=]?\s*' + _NUM + r'?(\d{20})\b'
        ), 1),
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
    ('АДРЕС', [
        (_p(_ADR_KW + r'(\d{6}[,\s]+[\wА-ЯЁа-яё\s\.,\-/№«»"]{10,350})'), 1),
        (_p(_ADR_KW +
            r'((?:(?:Р(?:оссийская\s+)?Федерация|РФ)[,\s]+)?'
            r'г(?:ород)?\.?\s+[\w\-]{2,30}[,\s]+'
            r'[\wА-ЯЁа-яё\s\.,\-/№]{5,350})'), 1),
        (_p(_ADR_KW +
            r'((?:ул(?:ица)?|пр(?:оспект)?|пер(?:еулок)?|бул(?:ьвар)?'
            r'|наб(?:ережная)?|ш(?:оссе)?|пл(?:ощадь)?)\.?\s+'
            r'[\wА-ЯЁа-яё\s\.\-]{2,50}[,\s]+д(?:ом)?\.?\s*[\w/]+'
            r'[\wА-ЯЁа-яё\s\.,\-/№]{0,100})'), 1),
        (_p(
            r'(?<!\d)(\d{6}[,\s]{1,5}'
            r'(?:[А-ЯЁа-яёA-Za-z\-]{2,30}[.,]?\s+)?'
            r'(?:[Гг]\.?\s*|[Гг][Оо][Рр]\.?\s+)'
            r'[А-ЯЁа-яё][А-ЯЁа-яё\-]{1,29}'
            r'[,\s][\wА-ЯЁа-яёA-Za-z\s,\.\-/№«»"]{15,350}?)'
            r'(?=\s*[\n\r]|\s*$)'
        ), 1),
        (_p(_ADR_KW +
            r'([А-ЯЁа-яё][А-ЯЁа-яё\w ]{1,30}'
            r'(?:область|край|республика|округ)[а-яё]*'
            r'[,\s]+'
            r'[^\n]{10,300})'), 1),
    ]),
    ('SWIFT', [
        (_p(r'SWIFT\s*[-:]?\s*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b'), 1),
    ]),
    ('ФИО', [
        (_p(
            r'(?<![А-ЯЁа-яё])'
            r'([А-ЯЁ][а-яё]{1,20}'
            r'\s+[А-ЯЁ][а-яё]{1,15}'
            r'\s+[А-ЯЁ][а-яё]*'
            r'(?:ович|евич|овна|евна|ична)[а-яё]*)'
            r'(?![А-ЯЁа-яё])'
        ), 1),
        (_p(
            r'(?<![А-ЯЁа-яё])'
            r'([А-ЯЁ][а-яё]{2,20}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)'
            r'(?![А-ЯЁа-яё])'
        ), 1),
        (_p(
            r'/\s*'
            r'([А-ЯЁ][а-яё]{1,20}'
            r'(?:\s+[А-ЯЁ][а-яё]{1,15}'
            r'(?:\s+[А-ЯЁ][а-яё]*'
            r'(?:ович|евич|овна|евна|ична)[а-яё]*)?)?'
            r'(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)?)'
            r'\s*/'
        ), 1),
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
    ('ЛИЦЕНЗИЯ', [
        (_p(r'лицензи[яию]\w*\s+(?:цб\s+рф|банка\s+росси\w+|центральн\w+\s+банк\w+)'
            r'\s*(?:' + _NUM + r')?(\d{3,6})\b'), 1),
        (_p(r'цб\s+рф\s+лицензи[яию]\w*\s*(?:' + _NUM + r')?(\d{3,6})\b'), 1),
    ]),
    ('URL', [
        (_p(r'((?:https?://|www\.)[A-Za-zА-ЯЁа-яё0-9\-\.]+\.[A-Za-z]{2,10}'
            r'(?:/[^\s,;)»"\'<>\n]{0,200})?)'), 1),
    ]),
]


# ── FIO validation helpers ────────────────────────────────────────────────────

_FIO_VERB_END_RE = re.compile(
    r'(?:ает|яет|ует|вает|зает|жает|щает|тает|нает|'
    r'ляет|ряет|бает|пает|дает|лает|кает|мает|гает|чает|хает)\s*$',
    re.IGNORECASE | re.UNICODE,
)

_FIO_NON_SURNAME_END_RE = re.compile(
    r'(?:тор|тель|ник|щик|ист|мен|нт|ент|нер|гер|ор|ер|ль|ик|ек|ок|ач|ич)\s*$',
    re.UNICODE,
)

_FIO_TRAIL_RE = re.compile(r'[\s.,;:!?)»"\'—\-]+$', re.UNICODE)


def _validate_fio_regex(text: str) -> bool:
    words = text.split()
    if not words:
        return False
    first = words[0]
    if _FIO_VERB_END_RE.search(first):
        return False
    if len(words) == 2 and _FIO_NON_SURNAME_END_RE.search(first):
        return False
    return True


def _apply_regex_pass(text: str, db_path, session_id: str,
                      exclusions: set = None) -> Tuple[str, Dict[str, str]]:
    matches = []
    used    = []

    for entity_type, patterns in REGEX_PATTERNS:
        for pat, grp in patterns:
            for m in pat.finditer(text):
                if grp == 0:
                    groups = [g for g in m.groups() if g]
                    if not groups:
                        continue
                    value = ' '.join(g.strip() for g in groups)
                    orig_text = m.group(0)
                    s, e  = m.start(), m.end()
                else:
                    try:
                        value = (m.group(grp) or '').strip()
                        orig_text = value
                        s, e  = m.start(grp), m.end(grp)
                    except IndexError:
                        continue

                if not value:
                    continue
                if entity_type == 'АДРЕС' and len(value) < 10:
                    continue

                # Skip if match contains a mask token (already-anonymized content)
                if _contains_token(value) or _contains_token(orig_text):
                    continue

                if exclusions and (orig_text, entity_type) in exclusions:
                    continue

                if entity_type == 'ФИО':
                    stripped = _FIO_TRAIL_RE.sub('', value)
                    if stripped != value:
                        e -= len(value) - len(stripped)
                        value = stripped
                        orig_text = _FIO_TRAIL_RE.sub('', orig_text)
                    if not value:
                        continue
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
            token = _wrap(get_or_create_token(db_path, session_id, orig_text, value, etype))
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
# spaCy NER pass
# ─────────────────────────────────────────────────────────────────────────────

def _apply_spacy_pass(text: str, db_path, session_id: str,
                      exclusions: set = None) -> Tuple[str, Dict[str, str]]:
    if not _ner_ready or _nlp is None:
        return text, {}

    try:
        doc = _nlp(text)
    except Exception as ex:
        print(f'[NER] spaCy processing error: {ex}')
        return text, {}

    replacements = {}
    for ent in doc.ents:
        if ent.label_ not in ('PER', 'ORG'):
            continue

        original = ent.text.strip()
        original = _FIO_TRAIL_RE.sub('', original)
        # Trim trailing punctuation/brackets/quotes that NER often grabs
        original = re.sub(r'^[\s«»"\'(\[]+|[\s«»"\'.,;:!?)\]]+$', '', original)

        if _is_bracketed_token(original) or _contains_token(original):
            continue
        if len(original) < 3:
            continue

        etype = 'ФИО' if ent.label_ == 'PER' else 'ЮЛ'

        if exclusions and (original, etype) in exclusions:
            continue

        if original not in replacements:
            token = get_or_create_token(db_path, session_id, original, original, etype)
            replacements[original] = f'[{token}]'

    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)

    if replacements:
        print(f'[NER] {len(replacements)} entities found via spaCy')
    return text, replacements


# ─────────────────────────────────────────────────────────────────────────────
# Known entities pass
# ─────────────────────────────────────────────────────────────────────────────

def _apply_known_entities(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    mappings = get_session_mappings(db_path, session_id)
    if not mappings:
        return text, {}

    replacements = {}
    for m in sorted(mappings, key=lambda x: -len(x['original_form'])):
        original = m['original_form']
        token = f"[{m['token']}]"
        if original in text and not _is_bracketed_token(original):
            replacements[original] = token

    if not replacements:
        return text, {}

    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)

    print(f'[KNOWN] {len(replacements)} existing entities applied')
    return text, replacements


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def anonymize_text_pipeline(
    text: str,
    db_path,
    session_id: str,
    use_spacy: bool = True,
    use_llm: bool = False,
) -> Tuple[str, Dict[str, str]]:
    """
    Sequential pipeline. Each layer receives text with already-substituted
    [TOKEN] placeholders and does not touch them.

    Returns: (anonymized_text, all_replacements_dict)
    all_replacements_dict = {original_form: "[TOKEN]"}
    """
    from core.db import get_exclusions
    exclusions = get_exclusions(db_path, session_id)

    all_reps: Dict[str, str] = {}

    # Pass 1: OPF + Regex
    text, reps1 = _apply_opf_pass(text, db_path, session_id, exclusions)
    text, reps2 = _apply_regex_pass(text, db_path, session_id, exclusions)
    all_reps.update(reps1)
    all_reps.update(reps2)

    # Pass 2: spaCy NER on already partially masked text
    if use_spacy:
        text, reps3 = _apply_spacy_pass(text, db_path, session_id, exclusions)
        all_reps.update(reps3)

    # Pass 3: LLM on text after regex+spaCy
    if use_llm:
        try:
            from core.llm import apply_llm_pass
            user_patterns = get_top_patterns(db_path, limit=20)
            print(f'[LLM] Loaded {len(user_patterns)} user patterns')
            text, reps4 = apply_llm_pass(text, db_path, session_id,
                                          user_patterns, exclusions)
            all_reps.update(reps4)
        except Exception as ex:
            print(f'[LLM] Pass failed: {ex}')

    # Final pass: apply known DB entries
    text, reps_known = _apply_known_entities(text, db_path, session_id)
    all_reps.update(reps_known)

    return text, all_reps


def apply_reverse(text: str, reverse_map: dict) -> str:
    """Replace [TOKEN] -> original. Also handles bare TOKEN for backward compat."""
    if not reverse_map:
        return text
    combined = {}
    for tok, orig in reverse_map.items():
        combined[f'[{tok}]'] = orig
        combined[tok]        = orig
    for token, orig in sorted(combined.items(), key=lambda x: -len(x[0])):
        text = text.replace(token, orig)
    return text
