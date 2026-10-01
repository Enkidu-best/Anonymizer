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
from functools import lru_cache
from typing import Tuple, Dict, List

from core.db import get_or_create_token, get_session_mappings, get_top_patterns
from core.detectors import _is_payment_bank


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

def _cs(pattern):
    return re.compile(pattern, re.UNICODE)


def _p(pattern):
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


_TOKEN_PREFIXES = (
    'FIO', 'YUL', 'INN', 'OGRN', 'KPP', 'RS', 'KS', 'BIK', 'SNILS',
    'PASSPORT', 'TEL', 'EMAIL', 'SWIFT', 'ADR', 'DOB', 'LIC', 'URL',
    'CARD', 'IBAN', 'CAD', 'CAR', 'VIN', 'OKPO', 'OMS', 'DL', 'NICK', 'NOT', 'REALTY', 'REG',
    'ДАТАРОЖД',
)
_TOKEN_PFX_ALT = '|'.join(_TOKEN_PREFIXES)

TOKEN_INNER_RE = re.compile(rf'\[(?:{_TOKEN_PFX_ALT})_\d+\]')

# Full bracketed token (e.g. "[YUL_1]")
ANY_TOKEN_RE = re.compile(rf'\[(?:{_TOKEN_PFX_ALT})_\d+\]')

# Partial token leak: open bracket + known prefix + digit, no closing bracket needed.
# Catches NER fragments like "АО «[YUL_1" that grabbed only part of a mask.
PARTIAL_TOKEN_RE = re.compile(rf'\[(?:{_TOKEN_PFX_ALT})_\d+')


@lru_cache(maxsize=256)
def _bounded_pattern(keys):
    parts = []
    for k in sorted(keys, key=len, reverse=True):
        # numbers: only digits are a boundary («No40702…», «БИК044…» are fine);
        # words: any letter/digit is («Иванов» ≠ «Ивановский»)
        pre = r'(?<!\d)' if k[:1].isdigit() else r'(?<![\w])' if k[:1].isalnum() else ''
        post = r'(?!\d)' if k[-1:].isdigit() else r'(?![\w])' if k[-1:].isalnum() else ''
        parts.append(pre + re.escape(k) + post)
    return re.compile('|'.join(parts), re.UNICODE)


def replace_bounded(text: str, replacements: Dict[str, str]) -> str:
    """Replace every key with its value in one pass, longest key first, without
    touching keys embedded in longer words or numbers («Иванов» ≠ «Ивановский»)."""
    reps = {k: v for k, v in replacements.items() if k}
    if not text or not reps:
        return text
    return _bounded_pattern(tuple(reps)).sub(lambda m: reps[m.group(0)], text)


def contains_bounded(text: str, key: str) -> bool:
    return bool(key) and bool(_bounded_pattern((key,)).search(text))


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
# Result: "ООО «Ромашка-Экспо»"  →  "ООО «[YUL_1]»"
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
    r'(?:благотворительн\w+\s+)?фонд\w*|'
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
    r'ГБУ|ГКУ|ОГУ|МКУ|ФГАОУ|'
    r'ГБУЗ|ГАУЗ|ГКУЗ|ФГБУЗ|ГБОУ|ГАОУ|ФГБОУ|МБОУ|МАОУ|МКОУ|МБДОУ|МАДОУ|'
    r'МБУ|МАУ|ГАУ|ГОУ|МОУ|МОО|РОО)'
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

# Typographic quote pairs: “…”  „…“  ‘…’  '…'
_OPF_RE_TYPO = _p(
    r'(' + _OPF_PFX + r'[“„‘\'])'
    r'([^“”„‘’\'\n]{2,80})'
    r'([”“’\'])'
)

_OPF_SUFFIX_SHORT = r'(?:' + _OPF_SHORT + r')'
_OPF_SUFFIX_ANY = r'(?:' + _OPF_SHORT + r'|' + _OPF_FULL + r')'
_OPF_RE_ANGLE_SFX = _p(
    r'«([^»\n]{2,80})»'
    r'\s*\(' + _OPF_SUFFIX_ANY + r'\)'
)
_OPF_RE_STRAIGHT_SFX = _p(
    r'["“„]([^"“”„\n]{2,80})["”“]'
    r'\s*\(' + _OPF_SUFFIX_ANY + r'\)'
)

_OPF_RE_INNER_ANGLE = _p(
    r'«(' + _OPF_SHORT + r')\s+'
    r'([А-ЯЁа-яёA-Za-z\d][А-ЯЁа-яё\w\-\s"]{1,60}?)»'
)


def _apply_opf_pass(text: str, db_path, session_id: str,
                    exclusions: set = None) -> Tuple[str, Dict[str, str]]:
    all_matches = []
    for pattern in (_OPF_RE_ANGLE, _OPF_RE_STRAIGHT, _OPF_RE_TYPO):
        for m in pattern.finditer(text):
            inner = m.group(2).strip()
            if len(inner) < 2 or _is_bracketed_token(inner) or _contains_token(inner):
                continue
            if _is_payment_bank(text, m.start()):
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
        (_p(r'ИНН[^\S\n]*[:=\-]?[^\S\n]*(\d{10}|\d{12})\b'), 1),
        (_p(r'ИНН[^\S\n]*/[^\S\n]*(?:КПП|ОГРН)[^\S\n]*[:=]?[^\S\n]*(\d{10}|\d{12})[^\S\n]*/'), 1),
    ]),
    ('ОГРН', [
        (_p(r'ОГРНИП[^\S\n]*[:=]?[^\S\n]*(\d{15})\b'), 1),
        (_p(r'ОГРН[^\S\n]*[:=]?[^\S\n]*(\d{13})\b'), 1),
        (_p(r'основной[^\S\n]+(?:государственный[^\S\n]+)?регистрационный[^\S\n]+номер[^\S\n]*[:№=]?[^\S\n]*(\d{13,15})\b'), 1),
        (_p(r'(?:ИНН|КПП)[^\S\n]*/[^\S\n]*ОГРН[^\S\n]*[:=]?[^\S\n]*\d{9,12}[^\S\n]*/[^\S\n]*(\d{13,15})\b'), 1),
        (_p(r'(?<!\d)([15]\d{12})(?!\d)'), 1),
        (_p(r'(?<!\d)(3\d{14})(?!\d)'), 1),
    ]),
    ('КПП', [
        (_p(r'КПП[^\S\n]*[:=]?[^\S\n]*(\d{9})\b'), 1),
        (_p(r'ИНН[^\S\n]*/[^\S\n]*КПП[^\S\n]*[:=]?[^\S\n]*(?:\d{10}|\d{12})[^\S\n]*/[^\S\n]*(\d{9})\b'), 1),
    ]),
    ('РС', [
        (_p(
            r'(?:р(?:асч)?\.?[^\S\n]*/[^\S\n]*с(?:ч(?:[ёе]т)?)?\b|расч[ёе]тн\w*[^\S\n]+сч[ёе]т\w*)'
            r'[^\S\n]*[:=]?[^\S\n]*' + _NUM + r'?(\d{20})\b'
        ), 1),
        (_p(r'(?<!\d)(4[012]\d{18})(?!\d)'), 1),
    ]),
    ('КС', [
        (_p(
            r'(?:к(?:ор(?:р)?)?\.?[^\S\n]*/[^\S\n]*с(?:ч(?:[ёе]т)?)?\b|'
            r'корр?\.[^\S\n]*сч[ёе]т\w*|корреспондентск\w+[^\S\n]+сч[ёе]т\w*)'
            r'[^\S\n]*[:=]?[^\S\n]*' + _NUM + r'?(\d{20})\b'
        ), 1),
        (_p(r'(?<!\d)(30[1-9]\d{17})(?!\d)'), 1),
    ]),
    ('БИК', [
        (_p(r'БИК[^\S\n]*[:=]?[^\S\n]*(\d{9})\b'), 1),
    ]),
    ('СНИЛС', [
        (_p(r'\b(\d{3}-\d{3}-\d{3}[^\S\n]+\d{2})\b'), 1),
        (_p(r'СНИЛС[^\S\n]*[:=]?[^\S\n]*(\d{11})\b'), 1),
    ]),
    ('ПАСПОРТ', [
        (_p(r'паспорт\w*[ \t\u00a0:;]+(?:сери[яи][^\S\n]+)?(\d{2}[^\S\n]*\d{2})[^\S\n]*,?[^\S\n]*(?:' + _NUM + r')?(\d{6})\b'), 0),
        (_p(r'сери[яи][^\S\n]+(\d{2}[^\S\n]+\d{2})[,; \t\u00a0]+(?:' + _NUM + r')(\d{6,9})\b'), 0),
        (_p(r'сери[яи][^\S\n]+(\d{2,4})[^\S\n]+(?:' + _NUM + r')(\d{6,9})\b'), 0),
    ]),
    ('ТЕЛЕФОН', [
        (_p(r'(?:тел[ефон.: \t\u00a0]*\.?|моб\.?[^\S\n]*[: \t\u00a0]|факс[^\S\n]*[: \t\u00a0])[^\S\n]*'
           r'(\+?[78]?[ \t\u00a0\-\(]?\d{3}[ \t\u00a0\-\)\.][^\S\n]*\d{3}[ \t\u00a0\-\.]\d{2}[ \t\u00a0\-\.]\d{2})\b'), 1),
        (_p(r'(?<!\d)(\+7[ \t\u00a0\-\(]?\d{3}[ \t\u00a0\-\)\.][^\S\n]*\d{3}[ \t\u00a0\-\.]\d{2}[ \t\u00a0\-\.]\d{2})\b'), 1),
        (_p(r'(?<!\d)(8[ \t\u00a0\-\(]\d{3}[ \t\u00a0\-\)\.][^\S\n]*\d{3}[ \t\u00a0\-\.]\d{2}[ \t\u00a0\-\.]\d{2})\b'), 1),
        (_p(r'(?<!\d)([78][3-9]\d{9})(?!\d)'), 1),
        (_p(r'(?<!\d)(9[0-9]\d{8})(?!\d)'), 1),
    ]),
    ('EMAIL', [
        (_p(r'\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b'), 1),
    ]),
    ('АДРЕС', [
        (_p(_ADR_KW + r'(\d{6}[, \t\u00a0]+[\wА-ЯЁа-яё \t\u00a0\.,\-/№«»"]{10,350})'), 1),
        (_p(_ADR_KW +
            r'((?:(?:Р(?:оссийская[^\S\n]+)?Федерация|РФ)[, \t\u00a0]+)?'
            r'г(?:ород)?\.?[^\S\n]+[\w\-]{2,30}[, \t\u00a0]+'
            r'[\wА-ЯЁа-яё \t\u00a0\.,\-/№]{5,350})'), 1),
        (_p(_ADR_KW +
            r'((?:ул(?:ица)?|пр(?:оспект)?|пер(?:еулок)?|бул(?:ьвар)?'
            r'|наб(?:ережная)?|ш(?:оссе)?|пл(?:ощадь)?)\.?[^\S\n]+'
            r'[\wА-ЯЁа-яё \t\u00a0\.\-]{2,50}[, \t\u00a0]+д(?:ом)?\.?[^\S\n]*[\w/]+'
            r'[\wА-ЯЁа-яё \t\u00a0\.,\-/№]{0,100})'), 1),
        (_p(
            r'(?<!\d)(\d{6}[, \t\u00a0]{1,5}'
            r'(?:[А-ЯЁа-яёA-Za-z\-]{2,30}[.,]?[^\S\n]+)?'
            r'(?:[Гг]\.?[^\S\n]*|[Гг][Оо][Рр]\.?[^\S\n]+)'
            r'[А-ЯЁа-яё][А-ЯЁа-яё\-]{1,29}'
            r'[, \t\u00a0][\wА-ЯЁа-яёA-Za-z \t\u00a0,\.\-/№«»"]{15,350}?)'
            r'(?=[^\S\n]*[\n\r]|[^\S\n]*$)'
        ), 1),
        (_p(_ADR_KW +
            r'([А-ЯЁа-яё][А-ЯЁа-яё\w ]{1,30}'
            r'(?:область|край|республика|округ)[а-яё]*'
            r'[, \t\u00a0]+'
            r'[^\n]{10,300})'), 1),
    ]),
    ('SWIFT', [
        (_p(r'SWIFT[^\S\n]*[-:]?[^\S\n]*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b'), 1),
    ]),
    ('ФИО', [
        (_cs(
            r'(?<![А-ЯЁа-яё])'
            r'([А-ЯЁ][а-яё]{1,20}'
            r'[^\S\n]+[А-ЯЁ][а-яё]{1,15}'
            r'[^\S\n]+[А-ЯЁ][а-яё]*'
            r'(?:(?:[оеё]вич|ьич|инич|ич)(?:а|у|ем|е)?|(?:[оеё]вн|[иы]чн|иничн)(?:а|ы|е|у|ой|ою))(?![а-яё]))'
            r'(?![А-ЯЁа-яё])'
        ), 1),
        (_cs(
            r'(?<![А-ЯЁа-яё])'
            r'([А-ЯЁ][а-яё]{2,20}[^\S\n]+[А-ЯЁ]\.[^\S\n]*[А-ЯЁ]\.)'
            r'(?![А-ЯЁа-яё])'
        ), 1),
        (_cs(
            r'/[^\S\n]*'
            r'([А-ЯЁ][а-яё]{1,20}'
            r'(?:[^\S\n]+[А-ЯЁ][а-яё]{1,15}'
            r'(?:[^\S\n]+[А-ЯЁ][а-яё]*'
            r'(?:(?:[оеё]вич|ьич|инич|ич)(?:а|у|ем|е)?|(?:[оеё]вн|[иы]чн|иничн)(?:а|ы|е|у|ой|ою))(?![а-яё]))?)?'
            r'(?:[^\S\n]+[А-ЯЁ]\.[^\S\n]*[А-ЯЁ]\.)?)'
            r'[^\S\n]*/'
        ), 1),
        (re.compile(
            r'(?<![А-ЯЁа-яёA-Za-z.])'
            r'([А-ЯЁ]\.[^\S\n]*[А-ЯЁ]\.[^\S\n]+[А-ЯЁ][а-яё]{2,25})'
            r'(?![А-ЯЁа-яё])',
            re.UNICODE
        ), 1),
    ]),
    ('ДАТАРОЖД', [
        (_p(r'\b(\d{1,2}[^\S\n]+' + _MONTHS_RU + r'[^\S\n]+\d{4})[^\S\n]+(?:года?[^\S\n]+рожд\w+|рожд\w+)'), 1),
        (_p(r'(?:рожд[ёе]н\w*[^\S\n]+)(\d{1,2}[^\S\n]+' + _MONTHS_RU + r'[^\S\n]+\d{4})\b'), 1),
        (_p(r'дата[^\S\n]+рождени\w+[^\S\n]*[: \t\u00a0][^\S\n]*(\d{1,2}[./]\d{1,2}[./]\d{2,4})\b'), 1),
    ]),
    ('ЛИЦЕНЗИЯ', [
        (_p(r'лицензи[яию]\w*[^\S\n]+(?:цб[^\S\n]+рф|банка[^\S\n]+росси\w+|центральн\w+[^\S\n]+банк\w+)'
            r'[^\S\n]*(?:' + _NUM + r')?(\d{3,6})\b'), 1),
        (_p(r'цб[^\S\n]+рф[^\S\n]+лицензи[яию]\w*[^\S\n]*(?:' + _NUM + r')?(\d{3,6})\b'), 1),
    ]),
    ('URL', [
        (_p(r'((?:https?://|www\.)[A-Za-zА-ЯЁа-яё0-9\-\.]+\.[A-Za-z]{2,10}'
            r'(?:/[^ \t\u00a0,;)»"\'<>\n]{0,200})?)'), 1),
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
    'москва','санкт-петербург','новосибирск','россия','рф','московский','московская',
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
    r'\b[А-ЯЁ][а-яё]+(?:(?:[оеё]вич|ьич|инич|ич)(?:а|у|ем|е)?|(?:[оеё]вн|[иы]чн|иничн)(?:а|ы|е|у|ой|ою))\b',
    re.UNICODE,
)


def _name_like_word(w: str) -> bool:
    """A word that can be part of a person's name: dictionary surname/name/patronymic,
    an initial, or a capitalized word unknown to the dictionary («Кацнельс»)."""
    from core.detectors import _tags, _unknown, _SURN_END
    w = w.strip('.,;:()«»"\'')
    if not w or re.fullmatch(r'[А-ЯЁA-Z]\.?(?:[А-ЯЁA-Z]\.?)?', w):
        return True
    cap = w.capitalize() if w.isupper() else w
    tags = _tags(cap)
    if tags & {'Surn', 'Name', 'Patr'}:
        return True
    return _unknown(cap) and bool(_SURN_END.search(cap.lower()) or not re.search('[а-яё]', cap.lower()))


def _validate_spacy_fio(text: str) -> bool:
    """Strict FIO check for spaCy outputs. See test_bugs_v23 for examples."""
    words = [w for w in re.split(r'[\s]+', text.strip()) if w]
    # every word must look like a part of a name: «Витрина», «Стороной»,
    # «Арендодателя Арендная» are ordinary words that spaCy tags as PER
    if not words or not all(_name_like_word(w) for w in words):
        return False
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


# Public bodies and courts are not secret («курс Центрального банка России», «Росреестр»)
_PUBLIC_ORG_RE = re.compile(
    r'центральн\w*\s+банк|банк\w*\s+росси|^цб\b|росреестр|федеральн\w*\s+налогов|^и?фнс\b|'
    r'министерств|правительств|арбитражн\w*\s+суд|верховн\w*\s+суд|конституционн\w*\s+суд|'
    r'федеральн\w*\s+служб|управлени\w*\s+федеральн|государственн\w*\s+дум|пенсионн\w*\s+фонд|'
    r'социальн\w*\s+фонд|прокуратур|^суд\b|^мвд\b|^фссп\b|^асв\b|агентств\w*\s+по\s+страхованию',
    re.IGNORECASE)


def _validate_spacy_org(text: str) -> bool:
    """Strict ORG check. See test_bugs_v23 for the cases that drove these rules."""
    raw = text.strip()
    if _PUBLIC_ORG_RE.search(raw):
        return False
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
        # a heading word in capitals («ФИНАНСОВОЙ») is not an acronym
        from core.detectors import _unknown
        return _unknown(t.capitalize())

    # Brand-style single token (MasterCard, Yandex, Сбербанк) — but not an ordinary
    # dictionary word that merely starts a sentence («Отчет», «Персонал»)
    if ' ' not in t and 3 <= len(t) <= 25 and re.fullmatch(r'[A-Za-zА-ЯЁа-яё0-9+\-]+', t):
        if t[:1].isupper():
            from core.detectors import _unknown, _tags
            brand = (_unknown(t) or 'Orgn' in _tags(t) or re.search(r'[A-Z].*[a-z].*[A-Z]|[0-9+\-]|[A-Za-z]', t))
            return bool(brand)

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
    # head words and fail here. Real org names like "Полярный Торговый Банк"
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


@lru_cache(maxsize=20000)
def morph_normal(word: str) -> str:
    """Lower-cased dictionary form of a word («Пастухова» → «пастухов»)."""
    m = _get_morph()
    w = word.lower().replace('ё', 'е')
    if m is None:
        return w
    try:
        return m.parse(w)[0].normal_form.replace('ё', 'е')
    except Exception:
        return w


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


_PUBLIC_LEGAL_URL = re.compile(
    r'(?:consultant\.ru|garant\.ru|kontur(?:-extern)?\.ru|pravo\.gov\.ru|publication\.pravo|'
    r'kad\.arbitr\.ru|sudact\.ru|vsrf\.ru|ksrf\.ru|cbr\.ru|nalog\.(?:gov\.)?ru|minfin|'
    r'government\.ru|kremlin\.ru|duma\.gov|docs\.cntd\.ru|base\.garant)', re.IGNORECASE)


def _apply_regex_pass(text: str, db_path, session_id: str,
                      exclusions: set = None) -> Tuple[str, Dict[str, str]]:
    from core.detectors import find_all
    matches = []
    used    = []

    def _add(s, e, etype):
        value = text[s:e]
        stripped = value.strip()
        if not stripped:
            return
        s += len(value) - len(value.lstrip())
        e -= len(value) - len(value.rstrip())
        value = stripped
        if etype == 'ФИО':
            cut = _FIO_TRAIL_RE.sub('', value)
            if cut != value and not re.search(r'[А-ЯЁ]\.$', value):
                e -= len(value) - len(cut)
                value = cut
            if not value or not _validate_fio_regex(value):
                return
        if etype == 'АДРЕС' and len(value) < 5:
            return
        if etype == 'URL' and _PUBLIC_LEGAL_URL.search(value):
            return   # links to public legal databases are not PII
        if _contains_token(value) or _contains_token(text[max(0, s - 1):e + 1]):
            return
        if exclusions and (value, etype) in exclusions:
            return
        if any(not (e <= us or s >= ue) for us, ue in used):
            return
        matches.append((s, e, value, etype))
        used.append((s, e))

    # 1. New rule detectors (form + context + checksum), values only
    for h in find_all(text):
        _add(h.start, h.end, h.type)

    # 2. Legacy patterns; multi-group patterns mask each group separately
    for entity_type, patterns in REGEX_PATTERNS:
        for pat, grp in patterns:
            for m in pat.finditer(text):
                if grp == 0:
                    for gi in range(1, (m.lastindex or 0) + 1):
                        if m.group(gi):
                            _add(m.start(gi), m.end(gi), entity_type)
                else:
                    try:
                        if m.group(grp):
                            _add(m.start(grp), m.end(grp), entity_type)
                    except IndexError:
                        continue

    if not matches:
        return text, {}

    matches.sort(key=lambda x: x[0])
    replacements = {}
    out, last = [], 0
    for s, e, value, etype in matches:
        if value not in replacements:
            replacements[value] = _wrap(get_or_create_token(db_path, session_id, value, value, etype))
        out.append(text[last:s])
        out.append(replacements[value])
        last = e
    out.append(text[last:])
    text = ''.join(out)

    by_type: Dict[str, int] = {}
    for _, _, _, etype in matches:
        by_type[etype] = by_type.get(etype, 0) + 1
    print(f'[REGEX] {len(replacements)} entities: ' +
          ', '.join(f'{k}={v}' for k, v in sorted(by_type.items())))
    return text, replacements


# ─────────────────────────────────────────────────────────────────────────────
# spaCy NER pass
# ─────────────────────────────────────────────────────────────────────────────

_OPF_LEAD_RE = re.compile(r'^(?:' + _OPF_SHORT + r'|' + _OPF_FULL + r')\s+', re.IGNORECASE | re.UNICODE)
_OPF_ONLY_RE = re.compile(r'(?:' + _OPF_SHORT + r'|' + _OPF_FULL + r')(?:\s+(?:' + _OPF_SHORT + r'))?',
                          re.IGNORECASE | re.UNICODE)


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
    spans = []
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
        if '\n' in original or '\t' in original:
            rejected['short'] += 1   # spaCy glued words from different lines / cells
            continue
        if _is_bracketed_token(original) or _contains_token(original):
            rejected['mask'] += 1
            continue

        etype = 'ФИО' if ent.label_ == 'PER' else 'ЮЛ'

        if etype == 'ЮЛ':
            # Keep the legal form in the text: «ООО Вектор» → mask only «Вектор»
            original = _OPF_LEAD_RE.sub('', original).strip(' «»"\'“”„')
            if not original or _OPF_ONLY_RE.fullmatch(original) or not original[:1].isupper() and not original[:1].isdigit():
                rejected['org_filter'] += 1
                continue
            if _is_payment_bank(text, ent.start_char):
                rejected['org_filter'] += 1
                continue

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

        off = text.find(original, ent.start_char, ent.end_char + 1)
        if off < 0:
            continue
        if original not in replacements:
            token = get_or_create_token(db_path, session_id, original, canonical, etype)
            replacements[original] = f'[{token}]'
        spans.append((off, off + len(original), replacements[original]))

    spans.sort()
    out, last = [], 0
    for s_, e_, tok in spans:
        if s_ < last:
            continue
        out.append(text[last:s_])
        out.append(tok)
        last = e_
    out.append(text[last:])
    text = ''.join(out)

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
        if contains_bounded(text, original) and not _is_bracketed_token(original):
            replacements[original] = token

    if not replacements:
        return text, {}

    text = replace_bounded(text, replacements)

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
        if not contains_bounded(text, val):
            continue
        token = get_or_create_token(db_path, session_id, val, val, etype)
        replacements[val] = f'[{token}]'

    text = replace_bounded(text, replacements)
    if replacements:
        print(f'[KNOWN-GLOBAL] {len(replacements)} entities applied from previous sessions')
    return text, replacements


_CAP_WORD_RE = re.compile(r'(?<![\w\[])[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?(?![\w\]])|(?<![\w\[])[А-ЯЁ]{3,}(?![\w\]])')


_ALIAS_RE = re.compile(r'\[(YUL_\d+)\]»?([^\[\]\n(]{0,40}?)\(([A-ZА-ЯЁ][A-ZА-ЯЁ0-9&\-]{1,8})\)')


def _propagate_orgs(text: str, db_path, session_id: str, exclusions: set = None):
    """«Northwind Trading & Investments (NTI)… NTI получила» and «Zelora Holding… Zelora подала»
    → one token. Only distinctive words spread: Latin names that are not corporate nouns,
    abbreviations, words absent from the dictionary («Вектрум»); never ordinary words."""
    from core.db import add_alias
    from core.detectors import _GENERIC_LATIN, _unknown
    opf = re.compile(r'(?:' + _OPF_SHORT + r')$')
    for m in _ALIAS_RE.finditer(text):
        abbr = m.group(3)
        if not opf.match(abbr) and not (exclusions and (abbr, 'ЮЛ') in exclusions):
            add_alias(db_path, session_id, m.group(1), abbr, 'ЮЛ')
    words = {}
    for mp in get_session_mappings(db_path, session_id):
        if mp['entity_type'] != 'ЮЛ':
            continue
        for w in re.findall(r'[A-Za-zА-ЯЁа-яё][A-Za-zА-ЯЁа-яё0-9&\-]*', mp['original_form']):
            if len(w) < 2 or w in _GENERIC_LATIN or opf.match(w):
                continue
            latin = w.isascii() and w[0].isupper()
            abbr = w.isupper() and len(w) <= 8
            if latin or abbr or (len(w) >= 4 and w[0].isupper() and _unknown(w)):
                words.setdefault(w, mp['token'])
    if not words:
        return text, {}
    reps = {w: f'[{t}]' for w, t in words.items()}
    spans = [(a, b, v) for a, b, v in replace_spans(text, reps)
             if not (exclusions and (text[a:b], 'ЮЛ') in exclusions)]
    for a, b, v in spans:
        add_alias(db_path, session_id, v.strip('[]'), text[a:b], 'ЮЛ')
    if spans:
        print(f'[PROPAGATE] {len(spans)} company mention(s)')
    return apply_spans(text, spans), {text[a:b]: v for a, b, v in spans}


def _propagate_surnames(text: str, db_path, session_id: str, exclusions: set = None):
    """«Белозёров обязуется…» after «Белозёров Аркадий Львович» → the same token.

    A capitalized word whose surname key equals the surname of exactly one masked
    person is masked too. Words that are first names or common dictionary words
    without a surname reading are skipped («Морозов» yes, «Мороз» no)."""
    from core.entities import Person, surname_key
    from core.detectors import _tags, _SURN_END
    persons = {}
    for m in get_session_mappings(db_path, session_id):
        if m['entity_type'] == 'ФИО':
            sk = Person.parse(m['original_form']).surname
            if sk and len(sk) >= 3:
                persons.setdefault(sk, set()).add(m['token'])
    if not persons:
        return text, {}
    reps, spans = {}, []
    for mt in _CAP_WORD_RE.finditer(text):
        w = mt.group()
        cap = w.capitalize() if w.isupper() else w
        tags = _tags(cap)
        if 'Name' in tags or 'Patr' in tags:
            continue
        if 'Surn' not in tags and not _SURN_END.search(cap.lower()):
            continue
        toks = persons.get(surname_key(cap))
        if not toks or len(toks) != 1:
            continue
        if exclusions and (w, 'ФИО') in exclusions:
            continue
        tok = _wrap(get_or_create_token(db_path, session_id, w, w, 'ФИО'))
        reps[w] = tok
        spans.append((mt.start(), mt.end(), tok))
    if spans:
        print(f'[PROPAGATE] {len(spans)} surname mention(s)')
    return apply_spans(text, spans), reps


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

    # Companies: abbreviation in brackets after a name, distinctive words of names elsewhere
    text, reps_org = _propagate_orgs(text, db_path, session_id, exclusions)
    all_reps.update(reps_org)

    # Propagation: surnames of found persons in any case form, anywhere in the text
    text, reps_prop = _propagate_surnames(text, db_path, session_id, exclusions)
    all_reps.update(reps_prop)

    # Final pass: apply known DB entries
    text, reps_known = _apply_known_entities(text, db_path, session_id)
    all_reps.update(reps_known)

    return text, all_reps


# Russian spellings an external LLM may use for token prefixes
_RU_PREFIX = {'ФИО': 'FIO', 'ЮЛ': 'YUL', 'ИНН': 'INN', 'ОГРН': 'OGRN', 'КПП': 'KPP',
              'ТЕЛ': 'TEL', 'АДР': 'ADR', 'АДРЕС': 'ADR', 'ПАСПОРТ': 'PASSPORT', 'БИК': 'BIK',
              'СНИЛС': 'SNILS', 'РС': 'RS', 'КС': 'KS'}
_PFX_ALL = sorted(set(_TOKEN_PREFIXES) | set(_RU_PREFIX), key=len, reverse=True)
_PFX_ALT_ALL = '|'.join(map(re.escape, _PFX_ALL))
# [FIO_1]  [FIO 1]  [FIO-1]  [fio_1]  [ФИО_1]  and bare FIO_1 (not inside FIO_10)
TOKEN_LOOSE_RE = re.compile(
    rf'\[\s*({_PFX_ALT_ALL})[\s_\-]*(\d+)\s*\]'
    rf'|(?<![A-Za-zА-Яа-яЁё0-9_])({_PFX_ALT_ALL})[_\-](\d+)(?![\d])',
    re.IGNORECASE | re.UNICODE)


def _token_key(m) -> str:
    pfx, n = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
    up = pfx.upper()
    return f'{_RU_PREFIX.get(up, up)}_{int(n)}'


def reverse_spans(text: str, reverse_map: dict) -> List[Tuple[int, int, str]]:
    """Spans of tokens in text with the value to restore."""
    out = []
    for m in TOKEN_LOOSE_RE.finditer(text):
        val = reverse_map.get(_token_key(m))
        if val is not None:
            out.append((m.start(), m.end(), val))
    return out


def replace_spans(text: str, replacements: Dict[str, str]) -> List[Tuple[int, int, str]]:
    """Spans of replacement keys in text (word boundaries, longest first)."""
    reps = {k: v for k, v in replacements.items() if k}
    if not text or not reps:
        return []
    return [(m.start(), m.end(), reps[m.group(0)])
            for m in _bounded_pattern(tuple(reps)).finditer(text)]


def apply_spans(text: str, spans) -> str:
    out, last = [], 0
    for a, b, v in sorted(spans):
        if a < last:
            continue
        out.append(text[last:a])
        out.append(v)
        last = b
    out.append(text[last:])
    return ''.join(out)


def make_finder(replacements: Dict[str, str], log: list = None):
    """find(text) → [(start, end, token)]; with log, records (token, original) in order."""
    def find(text):
        spans = replace_spans(text, replacements)
        if log is not None:
            log.extend((v.strip('[]'), text[a:b]) for a, b, v in sorted(spans))
        return spans
    return find


def make_rev_finder(db_path, session_id: str, occurrences: dict = None):
    """find(text) → [(start, end, original)]. The k-th occurrence of a token gets the
    k-th recorded form of the original file (exact case); otherwise the main form."""
    from core.db import get_reverse_info
    info = get_reverse_info(db_path, session_id)
    occ = occurrences or {}
    seen: Dict[str, int] = {}

    def find(text):
        out = []
        for m in TOKEN_LOOSE_RE.finditer(text):
            key = _token_key(m)
            if key not in info:
                continue
            value, edited = info[key]
            k = seen.get(key, 0)
            seen[key] = k + 1
            forms = occ.get(key)
            if forms and not edited and k < len(forms):
                value = forms[k]
            out.append((m.start(), m.end(), value))
        return out
    return find


def anonymize_text(text: str, db_path, session_id: str, use_spacy: bool = True,
                   use_llm: bool = False):
    """Plain-text anonymization. Returns (masked_text, occurrences {token: [forms]})."""
    _, reps = anonymize_text_pipeline(text, db_path, session_id, use_spacy=use_spacy, use_llm=use_llm)
    log = []
    out = apply_spans(text, make_finder(reps, log)(text))
    occ: Dict[str, list] = {}
    for tok, orig in log:
        occ.setdefault(tok, []).append(orig)
    return out, occ


def restore_text(text: str, db_path, session_id: str, occurrences: dict = None) -> str:
    return apply_spans(text, make_rev_finder(db_path, session_id, occurrences)(text))


def apply_reverse(text: str, reverse_map: dict) -> str:
    """Replace tokens with original values. Tolerates the ways an external LLM
    rewrites tokens: [FIO_1], FIO_1, [FIO 1], [FIO-1], [fio_1], [ФИО_1], [FIO_1]у."""
    if not reverse_map:
        return text
    return apply_spans(text, reverse_spans(text, reverse_map))
