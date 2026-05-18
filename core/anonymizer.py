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


_TOKEN_PREFIXES = (
    'FIO', 'YUL', 'INN', 'OGRN', 'KPP', 'RS', 'KS', 'BIK', 'SNILS',
    'PASSPORT', 'TEL', 'EMAIL', 'SWIFT', 'ADR', 'DOB', 'LIC', 'URL',
    'ДАТАРОЖД',
)
_TOKEN_PFX_ALT = '|'.join(_TOKEN_PREFIXES)

TOKEN_INNER_RE = re.compile(rf'\[(?:{_TOKEN_PFX_ALT})_\d+\]')

# Full bracketed token (e.g. "[YUL_1]")
ANY_TOKEN_RE = re.compile(rf'\[(?:{_TOKEN_PFX_ALT})_\d+\]')

# Partial token leak: open bracket + known prefix + digit, no closing bracket needed.
# Catches NER fragments like "АО «[YUL_1" that grabbed only part of a mask.
PARTIAL_TOKEN_RE = re.compile(rf'\[(?:{_TOKEN_PFX_ALT})_\d+')


def _wrap(token: str) -> str:
    return f'[{token}]'

def _is_bracketed_token(text: str) -> bool:
    return bool(TOKEN_INNER_RE.fullmatch(text.strip()))

def _contains_token(text: str) -> bool:
    """True if text contains a full [TYPE_N] mask OR a partial bracket leak
    like '[YUL_1' that NER may have grabbed mid-token."""
    return bool(PARTIAL_TOKEN_RE.search(text))


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
    r'ляет|ряет|бает|пает|дает|лает|кает|мает|гает|чает|хает|'
    r'ован|ёван|ирован|изован)\s*$',
    re.IGNORECASE | re.UNICODE,
)

_FIO_NON_SURNAME_END_RE = re.compile(
    r'(?:тор|тель|ник|щик|ист|мен|нт|ент|нер|гер|ор|ер|ль|ик|ек|ок|ач|ич)\s*$',
    re.UNICODE,
)

_FIO_TRAIL_RE = re.compile(r'[\s.,;:!?)»"\'—\-]+$', re.UNICODE)

# Russian surname endings (covers common patterns including plural genitive
# like "Москотельниковых")
_SURNAME_ENDING_RE = re.compile(
    r'(?:'
    r'ов|ев|ёв|ин|ын|кий|ский|цкий|ской|цкой|чкий|ной|ний|'
    r'ова|ева|ёва|ина|ына|кая|ская|цкая|чкая|ская|ной|ная|няя|'
    r'ых|их|енко|ёнко|онок|ёнок|чук|юк|ук|ян|ан|швили|дзе|оглу|заде|вич|евич'
    r')$',
    re.IGNORECASE | re.UNICODE,
)

# Common words frequently mis-tagged by spaCy as PER/ORG in Russian business
# documents. Lower-cased; matched against the lowered single-word entity.
_STOP_TOKENS = {
    # generic nouns / job titles
    'возглавляет','возглавлял','подтвержден','подтверждено','подтверждена','инфраструктура',
    'риски','риск','заключение','заключения','бенефициары','бенефициар','руководитель',
    'статус','рейтинг','рейтинги','показатели','рекомендации','прибыль','прибыли',
    'кредит','ставка','ставки','счет','счёт','счета','счёта','рубль','рублей','рублях',
    'оглавление','содержание','введение','глава','раздел','параграф','примечание',
    'договор','договоры','протокол','акт','справка','анализ','отчет','отчёт','устав',
    'председатель','генеральный','директор','президент','секретарь','собственник',
    'участник','учредитель','собрание','решение','подпись','одобрение','согласие',
    'председателя','правления','совета','директоров','владелец','владельцы',
    'банка','банком','банке','банки','банков','банку','деятельность','деятельности',
    'гарантии','гарантия','гарантий','обязательства','обязательство',
    'включен','включена','включено','включены','выполнен','выполнена','выполнено',
    'регион','региональный','региональная','региональные',
    'единственный','единственная','единственное','единственные',
    'надежный','надежная','надежное','надежные','надёжный','надёжная',
    # geo (we don't anonymize cities/countries here)
    'москва','санкт-петербург','сыктывкар','россия','рф','московский','московская',
    # business abbrevs that are NOT companies on their own
    'огрн','инн','кпп','бик','снилс','рс','кс','ндс','усн','ос','осно','осн','еио',
}


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


# ── Strict validators for spaCy outputs ──────────────────────────────────────

_HAS_INITIALS_RE = re.compile(r'[А-ЯЁ]\.\s*[А-ЯЁ]\.', re.UNICODE)
_HAS_PATRONYMIC_RE = re.compile(
    r'\b[А-ЯЁ][а-яё]+(?:ович|евич|овна|евна|ична|инична|иничн)\b',
    re.UNICODE,
)


def _validate_spacy_fio(text: str) -> bool:
    """Strict FIO check for spaCy outputs. See test_bugs_v23 for examples."""
    t = text.strip().strip('«»"\'(),.;:—–-')
    if not t or len(t) < 3 or len(t) > 80:
        return False
    if any(ch in t for ch in '()!?[]{}/'):
        return False
    if t.count(',') >= 2 or ';' in t:
        return False

    # If ANY token in the phrase is a known generic noun/verb/header, reject.
    words = t.split()
    lower_words = [w.lower().strip('.,') for w in words]
    if any(lw in _STOP_TOKENS for lw in lower_words):
        return False

    # First word must start with a capital letter — names always do.
    if not words[0][:1].isupper():
        return False

    # Quick wins: initials or patronymic
    if _HAS_INITIALS_RE.search(t) or _HAS_PATRONYMIC_RE.search(t):
        return _validate_fio_regex(t)

    # Single word: must look like a surname
    if len(words) == 1:
        w = words[0]
        if _FIO_VERB_END_RE.search(w):
            return False
        return bool(_SURNAME_ENDING_RE.search(w))

    if 2 <= len(words) <= 4:
        if not _validate_fio_regex(t):
            return False
        # Every word must start with capital — phrases like "Совета директоров"
        # have one capitalised + one lower-cased token in source text and would
        # already fail this check (NER often preserves casing).
        if not all(w[:1].isupper() for w in words):
            return False
        if any(_SURNAME_ENDING_RE.search(w) or _HAS_INITIALS_RE.search(w) for w in words):
            return True
        return False

    return False


_ORG_QUOTE_RE = re.compile(r'[«"\'“„]', re.UNICODE)
_OPF_ANY_RE = re.compile(
    r'\b(?:ООО|ОАО|ЗАО|АО|ПАО|НАО|ОДО|ИП|КФХ|ТСЖ|СНТ|'
    r'ФГУП|ГУП|МУП|АНО|НП|НКО|ГК|КБ|'
    r'ФГУ|ФГАУ|ФГБУ|ФГКУ|ФКУ|ГБУ|ГКУ|ОГУ|МКУ|ФГАОУ|'
    r'общество|общества|организация|товарищество|кооператив|фонд|корпорация|'
    r'банк|банка|банком|банке)\b',
    re.IGNORECASE | re.UNICODE,
)


def _validate_spacy_org(text: str) -> bool:
    """Strict ORG check. See test_bugs_v23 for the cases that drove these rules."""
    raw = text.strip()
    t = raw.strip('«»"\'(),.;:—–-')
    if not t or len(t) < 2 or len(t) > 80:
        return False
    # Sentence-like content
    if t.count(',') >= 2 or ';' in t or any(ch in t for ch in '!?[]'):
        return False
    # Unbalanced parens suggest a captured fragment
    if t.count('(') != t.count(')'):
        return False

    low = t.lower()
    if low in _STOP_TOKENS:
        return False

    # All-caps ASCII or Cyrillic acronym (АСВ, МИР, VISA) — 2-10 chars
    if 2 <= len(t) <= 10 and re.fullmatch(r'[A-ZА-ЯЁ0-9]+', t):
        return True

    # Mixed case brand-style single token (MasterCard, Yandex, etc.)
    if ' ' not in t and 3 <= len(t) <= 25 and re.fullmatch(r'[A-Za-zА-ЯЁа-яё0-9+\-]+', t):
        if t[:1].isupper():
            return True

    words = t.split()
    if len(words) > 8:
        return False

    has_quote = bool(_ORG_QUOTE_RE.search(text))
    has_opf = bool(_OPF_ANY_RE.search(t))

    # Require structural signal: quotes OR explicit OPF mention
    if not (has_quote or has_opf):
        return False

    # Strip the OPF marker words — what remains should still look like a name
    # ("(акционерное общество)" alone has nothing left after stripping OPF).
    rest = _OPF_ANY_RE.sub(' ', t).strip(' «»"\'(),.;:—–-')
    if not rest or len(rest) < 2:
        return False
    rest_words = rest.split()
    # For capital-letter check, strip leading punctuation/quotes from each word
    def _starts_upper(w):
        s = w.lstrip('«»"\'(.,;:—–-')
        return bool(s) and s[:1].isupper()

    # Reject if the "name" after OPF is just generic words ("банк", "общество")
    if all(w.lower().strip('«»"\'(.,;:—–-') in _STOP_TOKENS for w in rest_words):
        return False
    # Every word must start with a capital — descriptions like
    # "надежный региональный банк «домашнего» типа" have all-lowercase
    # head words and fail here. Real org names like "Северный Народный Банк"
    # have all words capitalised.
    if not all(_starts_upper(w) for w in rest_words):
        return False
    # Reject when ≥ ⅔ of remaining words are stop tokens (description, not name)
    bad_remaining = sum(
        1 for w in rest_words if w.lower().strip('«»"\'(.,;:—–-') in _STOP_TOKENS
    )
    if rest_words and bad_remaining * 3 >= len(rest_words) * 2:
        return False

    return True


# ── pymorphy3 lazy loader for FIO normalization ──────────────────────────────

_morph = None
def _get_morph():
    global _morph
    if _morph is not None:
        return _morph
    try:
        import pymorphy3
        _morph = pymorphy3.MorphAnalyzer()
    except Exception as ex:
        print(f'[NER] pymorphy3 unavailable: {ex}')
        _morph = False
    return _morph


def _normalize_fio(text: str) -> str:
    """Normalize an FIO string to nominative case word-by-word (pymorphy3).
    Returns original text on any failure. Pure best-effort."""
    morph = _get_morph()
    if not morph:
        return text
    try:
        out = []
        for w in text.split():
            # Keep initials like "А." as-is
            if re.fullmatch(r'[А-ЯЁA-Z]\.', w):
                out.append(w)
                continue
            p = morph.parse(w)[0]
            try:
                inflected = p.inflect({'nomn'})
                out.append(inflected.word.capitalize() if inflected else w)
            except Exception:
                out.append(w)
        return ' '.join(out)
    except Exception:
        return text


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
    rejected = {'short': 0, 'mask': 0, 'fio_filter': 0, 'org_filter': 0}

    for ent in doc.ents:
        if ent.label_ not in ('PER', 'ORG'):
            continue

        original = ent.text.strip()
        original = _FIO_TRAIL_RE.sub('', original)
        # Trim leading/trailing punctuation/brackets/quotes that NER often grabs.
        # Also peel off any bracket-mask fragment NER might have glued on.
        original = re.sub(r'^[\s«»"\'(\[]+|[\s«»"\'.,;:!?)\]]+$', '', original).strip()
        # If the NER span started/ended inside a [TOKEN_N] mask, strip that part
        original = PARTIAL_TOKEN_RE.sub('', original).strip()
        original = re.sub(r'^[\s«»"\'(\[]+|[\s«»"\'.,;:!?)\]]+$', '', original).strip()

        if not original or len(original) < 3:
            rejected['short'] += 1
            continue
        if _is_bracketed_token(original) or _contains_token(original):
            rejected['mask'] += 1
            continue

        etype = 'ФИО' if ent.label_ == 'PER' else 'ЮЛ'

        if etype == 'ФИО':
            if not _validate_spacy_fio(original):
                rejected['fio_filter'] += 1
                continue
        else:
            if not _validate_spacy_org(original):
                rejected['org_filter'] += 1
                continue

        if exclusions and (original, etype) in exclusions:
            continue

        # canonical_form: nominative for FIO via pymorphy3, identical for ORG
        canonical = _normalize_fio(original) if etype == 'ФИО' else original

        if original not in replacements:
            token = get_or_create_token(db_path, session_id, original, canonical, etype)
            replacements[original] = f'[{token}]'

    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)

    if replacements or any(rejected.values()):
        print(f'[NER] kept {len(replacements)} entities, rejected: {rejected}')
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

def _apply_global_known(text: str, db_path, session_id: str,
                         exclusions: set = None) -> Tuple[str, Dict[str, str]]:
    """Apply entities the user has confirmed in previous sessions.
    Runs BEFORE other passes so they don't try to re-detect these values."""
    from core.db import get_known_entities
    known = get_known_entities(db_path, limit=500)
    if not known:
        return text, {}

    replacements = {}
    # Replace longest first so we don't shadow longer matches
    for item in sorted(known, key=lambda x: -len(x['value'])):
        val = item['value']
        etype = item['entity_type']
        if not val or len(val) < 2:
            continue
        if _is_bracketed_token(val) or _contains_token(val):
            continue
        if exclusions and (val, etype) in exclusions:
            continue
        if val not in text:
            continue
        token = get_or_create_token(db_path, session_id, val, val, etype)
        replacements[val] = f'[{token}]'

    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)
    if replacements:
        print(f'[KNOWN-GLOBAL] {len(replacements)} entities applied from previous sessions')
    return text, replacements


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

    # Pass 0: globally-learned entities from prior sessions
    text, reps0 = _apply_global_known(text, db_path, session_id, exclusions)
    all_reps.update(reps0)

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
