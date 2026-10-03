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
from core.lexicon import not_pii


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
    'CARD', 'IBAN', 'CAD', 'CAR', 'VIN', 'OKPO', 'OMS', 'DL', 'NICK', 'NOT', 'REALTY', 'EGRN', 'REG',
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
    r'(?:ООО|ПАО|НАО|ЗАО|ОАО|АО|ОДО|ОсОО|ТОО|ЖШС|ЧП|'
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
            if _is_payment_bank(text, m.start()) or not_pii(inner, 'ЮЛ'):
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
_NOT_ORG_TOKENS = {'HYPERLINK', 'PAGE', 'MERGEFORMAT', 'TOC', 'REF', 'RUR', 'RUB', 'USD', 'EUR', 'CNY', 'GBP'}
_PUBLIC_ORG_RE = re.compile(
    r'центральн\w*\s+банк|банк\w*\s+росси|^цб\b|росреестр|федеральн\w*\s+налогов|^и?фнс\b|'
    r'министерств|правительств|арбитражн\w*\s+суд|верховн\w*\s+суд|конституционн\w*\s+суд|'
    r'федеральн\w*\s+служб|управлени\w*\s+федеральн|государственн\w*\s+дум|пенсионн\w*\s+фонд|'
    r'социальн\w*\s+фонд|^мин(?:фин|юст|эконом|труд|здрав|обр|цифр|промторг|энерго)\w*|прокуратур|^суд\b|^мвд\b|^фссп\b|^асв\b|агентств\w*\s+по\s+страхованию',
    re.IGNORECASE)


def _validate_spacy_org(text: str) -> bool:
    """Strict ORG check. See test_bugs_v23 for the cases that drove these rules."""
    raw = text.strip()
    if _PUBLIC_ORG_RE.search(raw):
        return False
    if raw.count('«') != raw.count('»') or raw.count('"') % 2:
        return False   # fragment cut inside quotes: «КД «Вектор»
    if raw.upper() in _NOT_ORG_TOKENS:
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

    # A bare acronym (КП, РВПС, АППГ, УКР…) is usually an internal abbreviation of a
    # department or a term, not a company: companies come with a legal form or quotes,
    # which the rule layer already handles.
    if re.fullmatch(r'[A-ZА-ЯЁ0-9&\-]+', t):
        return False

    # Brand-style single token (MasterCard, Yandex, Сбербанк) — but not an ordinary
    # dictionary word that merely starts a sentence («Отчет», «Персонал»)
    if ' ' not in t and 3 <= len(t) <= 25 and re.fullmatch(r'[A-Za-zА-ЯЁа-яё0-9+\-]+', t):
        if t[:1].isupper():
            # only brand-like Latin names (CaseBook, MasterCard) or dictionary organisations;
            # a capitalized Russian word is usually just the start of a sentence («Выведенные»)
            from core.detectors import _tags
            return bool('Orgn' in _tags(t) or re.fullmatch(r'[A-Z][a-z]+[A-Z][A-Za-z]+|[A-Z][a-z]{3,}', t))

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
    if not has_quote:
        from core.detectors import _unknown
        if not any(_unknown(w.strip('«»"\'(),.').capitalize()) or re.search('[A-Za-z]', w)
                   for w in rest_words):
            return False   # only dictionary words, no quotes: a heading, not a name
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
        if etype in ('ФИО', 'ЮЛ') and not_pii(value, etype):
            return   # role of a party, job title, heading, public body (core/lexicon.py)
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
        if not_pii(original, etype):
            rejected['fio_filter' if etype == 'ФИО' else 'org_filter'] += 1
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
            abbr = w.isupper() and 3 <= len(w) <= 8
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
    llm_only: bool = False,
) -> Tuple[str, Dict[str, str]]:
    """
    Sequential pipeline. Each layer receives text with already-substituted
    [TOKEN] placeholders and does not touch them.

    Returns: (anonymized_text, all_replacements_dict)
    all_replacements_dict = {original_form: "[TOKEN]"}
    Every layer is a journal stage with its duration and the number of finds per type.
    """
    from core.db import get_exclusions
    from core import log
    exclusions = get_exclusions(db_path, session_id)
    text_in = text
    all_reps: Dict[str, str] = {}
    job = log.current_job()

    def run(name, fn, *args):
        nonlocal text
        with log.stage(name):
            text, reps = fn(text, *args)
        all_reps.update(reps)
        by_type: Dict[str, int] = {}
        for tok in set(reps.values()):
            p = tok.strip('[]').rsplit('_', 1)[0]
            by_type[p] = by_type.get(p, 0) + 1
        if by_type:
            log.event('layer', layer=name, found=by_type)
            if job:
                job.add(**{f'{name}.{k}': v for k, v in by_type.items()})

    if not llm_only:
        run('known_global', _apply_global_known, db_path, session_id, exclusions)
        # values already found in this session (earlier files, manual additions) go first:
        # later layers must not cut them into pieces
        run('known_session_first', _apply_known_entities, db_path, session_id)
        run('opf', _apply_opf_pass, db_path, session_id, exclusions)
        run('rules', _apply_regex_pass, db_path, session_id, exclusions)
        if use_spacy:
            run('spacy', _apply_spacy_pass, db_path, session_id, exclusions)
    if use_llm or llm_only:
        def _llm(t, *a):
            try:
                from core.llm import apply_llm_pass
                return apply_llm_pass(t, db_path, session_id, get_top_patterns(db_path, limit=20), exclusions)
            except Exception as ex:
                log.error('llm_pass_failed', ex)
                return t, {}
        run('llm', _llm)
    if not llm_only:
        run('propagate_orgs', _propagate_orgs, db_path, session_id, exclusions)
        run('propagate_surnames', _propagate_surnames, db_path, session_id, exclusions)
    run('known_session', _apply_known_entities, db_path, session_id)
    _record_org_forms(text_in, all_reps, db_path, session_id)
    # end-of-processing check of the base: one entity written several ways → one token
    from core.db import consolidate_session
    with log.stage('consolidate'):
        merged = consolidate_session(db_path, session_id)
    if merged:
        log.event('layer', layer='consolidate', merged=len(merged))
        all_reps = {k: merged.get(v, v) for k, v in all_reps.items()}
        text = re.sub('|'.join(map(re.escape, merged)), lambda m: merged[m.group(0)], text)
    return text, all_reps


_FULL_TO_SHORT = [
    (r'общест\w+\s+с\s+ограниченной', 'ООО'), (r'публичн\w+\s+акционерн', 'ПАО'),
    (r'непубличн\w+\s+акционерн', 'НАО'), (r'закрыт\w+\s+акционерн', 'ЗАО'),
    (r'открыт\w+\s+акционерн', 'ОАО'), (r'акционерн\w+\s+общест', 'АО'),
    (r'автономн\w+\s+некоммерческ', 'АНО'), (r'некоммерческ\w+\s+партнерств', 'НП'),
    (r'коммерческ\w+\s+банк', 'КБ'), (r'федеральн\w+\s+государственн\w+\s+унитарн', 'ФГУП'),
    (r'муниципальн\w+\s+унитарн', 'МУП'), (r'государственн\w+\s+унитарн', 'ГУП'),
]
_OPF_BEFORE_RE = re.compile(r'(' + _OPF_SHORT + r'|' + _OPF_FULL + r')[^\S\n]*[«"“„\']?[^\S\n]*$',
                            re.IGNORECASE | re.UNICODE)


def short_opf(opf: str) -> str:
    """«Общество с ограниченной ответственностью» → «ООО»; short forms as written."""
    t = re.sub(r'\s+', ' ', opf).strip()
    for rx, short in _FULL_TO_SHORT:
        if re.match(rx, t, re.IGNORECASE):
            return short
    return t if re.fullmatch(_OPF_SHORT, t) else ''


def _record_org_forms(text: str, reps: Dict[str, str], db_path, session_id: str):
    """The legal form right before a company name in the document («ООО «[YUL_1]»») is kept
    with the company: an external LLM drops it, the restore puts it back (task 3, §2.2)."""
    from core.db import set_org_forms
    forms = {}
    for orig, tok in reps.items():
        key = tok.strip('[]')
        if not key.startswith('YUL_') or key in forms or len(orig) < 2:
            continue
        for m in _bounded_pattern((orig,)).finditer(text):
            b = _OPF_BEFORE_RE.search(text[max(0, m.start() - 60):m.start()])
            opf = short_opf(b.group(1)) if b else ''
            if opf:
                forms[key] = opf
                break
    if forms:
        set_org_forms(db_path, session_id, forms)


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


def place_hash(anonymized_text: str) -> str:
    """Fingerprint of an anonymized place (paragraph, cell, page): whitespace collapsed,
    tokens in canonical form — so «[fio 1]» written by an LLM still matches «[FIO_1]»."""
    import hashlib
    t = TOKEN_LOOSE_RE.sub(lambda m: f'[{_token_key(m)}]', anonymized_text)
    t = re.sub(r'\s+', ' ', t).strip()
    return hashlib.sha1(t.encode('utf-8')).hexdigest()


def make_finder(replacements: Dict[str, str], log: list = None, places: list = None):
    """find(text) → [(start, end, token)].

    log    — receives (token, original) in document order (preview «show originals»);
    places — receives (fingerprint of the anonymized place, [(token, original), …]) so that
             restore can give every place its exact forms."""
    def find(text):
        spans = sorted(replace_spans(text, replacements))
        items = [(v.strip('[]'), text[a:b]) for a, b, v in spans]
        if log is not None:
            log.extend(items)
        if places is not None and items:
            places.append((place_hash(apply_spans(text, spans)), items))
        return spans
    return find


class Restored(str):
    """A restored value with its kind for the preview and the Word highlight (task 3, §2.4):
    mark 'exact' — the form of this very place or a value without case (numbers, companies);
    'case' — a name in new text, case chosen automatically."""
    mark = 'exact'
    highlight = False


def _restored(value: str, mark: str, highlight: bool = False) -> Restored:
    r = Restored(value)
    r.mark, r.highlight = mark, highlight
    return r


_ORG_NOUNS = {'компания', 'общество', 'организация', 'фирма', 'банк', 'фонд', 'предприятие', 'учреждение',
              'партнерство', 'товарищество', 'кооператив', 'группа', 'холдинг', 'корпорация', 'контрагент',
              'ответственность', 'ассоциация', 'союз', 'агентство', 'центр', 'завод', 'концерн'}
_OPF_WORD_RE = re.compile(r'^(?:' + _OPF_SHORT + r')$')


def org_in_new_text(name: str, opf: str, before: str, after: str, settings: dict) -> str:
    """A company name put into new text (task 3, §2.2): already in quotes → the name;
    otherwise «name»; standing alone (no legal form or «компания» on the left) → «ООО «name»»."""
    if re.search(r'[«"“„]', name):
        return name
    if re.search(r'[«"“„\']\**\s*$', before) and re.match(r'\s*\**[»"”“\']', after):
        return name
    q = f'«{name}»' if settings.get('org_quotes', 'always') == 'always' else name
    words = re.findall(r'[А-ЯЁа-яёA-Za-z]+', before[-40:])
    prev = words[-1] if words else ''
    has_form = bool(prev) and (_OPF_WORD_RE.match(prev) or morph_normal(prev) in _ORG_NOUNS)
    mode = settings.get('org_opf', 'context')
    if has_form or not opf or mode == 'never':
        return q
    return f'{opf} {q}'


def _line_window(text: str, s: int, e: int):
    """Text left and right of a token inside its line (the same for a paragraph and a whole file)."""
    a = text.rfind('\n', 0, s) + 1
    b = text.find('\n', e)
    return text[max(a, s - 120):s], text[e:(b if b >= 0 else len(text))][:60]


def make_rev_finder(db_path, session_id: str, occurrences: dict = None, file_key: str = None,
                    settings: dict = None):
    """find(text) → [(start, end, Restored)], restoring by place.

    A place whose anonymized text is unchanged (same fingerprint) gets the exact forms
    recorded for it, whatever order or file it comes in. Other places (text written by
    an external LLM) get the main form; there a person's name is declined by the rules
    of core/cases.py (or by one short LLM request prepared with `find.prepare(text)`) and
    counted in stats['check_case']; a company gets quotes / legal form (task 3, §2.2).
    Tokens unknown to the session go to stats['unknown'].
    Mappings marked «не маскировать» or replaced still restore (never deleted)."""
    from core.db import get_reverse_info, get_places, get_org_forms
    from core import settings as user_settings
    from core.cases import decline, target_case
    st = settings or user_settings.get()
    info = get_reverse_info(db_path, session_id)
    forms = get_org_forms(db_path, session_id)
    places = get_places(db_path, session_id, file_key)
    stats = {'tokens': 0, 'restored': 0, 'exact': 0, 'check_case': 0, 'unknown': []}
    used: Dict[str, int] = {}
    llm_forms: Dict[tuple, str] = {}
    marks: list = []

    def base_name(key):
        value, edited = info[key]
        return value if edited else decline(value, 'nomn')

    def case_at(text, s, e, depth=0):
        """Case a name needs at [s, e): rules; «[FIO_1] и [FIO_2]» — the same case as the first."""
        left, right = _line_window(text, s, e)
        prev = None
        for pm in TOKEN_LOOSE_RE.finditer(left):
            prev = pm
        if depth < 3 and prev is not None and re.fullmatch(r'\s*(?:и|или|,)\s*', left[prev.end():]) and \
                _token_key(prev).startswith('FIO_'):
            off = s - len(left)
            c = case_at(text, off + prev.start(), off + prev.end(), depth + 1)
            if c:
                return c
        return target_case(left, right)

    def person(key, text, m):
        base = base_name(key)
        case = case_at(text, m.start(), m.end())
        if case:
            return decline(base, case)
        left, right = _line_window(text, m.start(), m.end())
        return llm_forms.get((left, key, right), base)

    def find(text):
        out = []
        matches = list(TOKEN_LOOSE_RE.finditer(text))
        if not matches:
            return out
        h = place_hash(text)
        variants = places.get(h) or []
        k_place = used.get(h, 0)              # the k-th place with this fingerprint
        used[h] = k_place + 1
        items = variants[min(k_place, len(variants) - 1)] if variants else None
        for k, m in enumerate(matches):
            key = _token_key(m)
            stats['tokens'] += 1
            if key not in info:
                if key not in stats['unknown']:
                    stats['unknown'].append(key)
                marks.append(('unknown', m.group(0)))
                continue
            value, edited = info[key]
            mark = 'exact'
            pfx = key.split('_')[0]
            if items and k < len(items) and items[k][0] == key:
                value = items[k][1]            # exact form of this very place
            elif pfx == 'FIO':
                value, mark = person(key, text, m), 'case'
            elif pfx == 'YUL':
                value = org_in_new_text(value, forms.get(key, ''), text[:m.start()], text[m.end():], st)
            if mark == 'case':
                stats['check_case'] += 1       # new text: the case of the name was chosen
            else:
                stats['exact'] += 1
            stats['restored'] += 1
            marks.append((mark, value))
            out.append((m.start(), m.end(), _restored(value, mark, mark == 'case' and st.get('highlight_case'))))
        return out

    def prepare(full_text: str):
        """Before writing: names in new text whose case the rules can't tell → one LLM request."""
        from core.cases import same_person_form, case_of_form
        todo = []
        for line in full_text.split('\n'):
            ms = list(TOKEN_LOOSE_RE.finditer(line))
            if not ms or place_hash(line) in places:
                continue
            for m in ms:
                key = _token_key(m)
                if not key.startswith('FIO_') or key not in info:
                    continue
                left, right = _line_window(line, m.start(), m.end())
                if case_at(line, m.start(), m.end()) is None and (left, key, right) not in llm_forms:
                    sent = left + '[' + base_name(key) + ']' + right
                    sent = TOKEN_LOOSE_RE.sub(lambda x: info.get(_token_key(x), (x.group(0),))[0], sent)
                    todo.append(((left, key, right), sent, base_name(key)))
                    llm_forms[(left, key, right)] = base_name(key)
        if not todo:
            return 0
        from core import llm
        answers = llm.choose_cases([(sent, base) for _, sent, base in todo[:40]])
        ok = 0
        for i, (k, _, base) in enumerate(todo[:40]):
            a = answers.get(i)
            c = case_of_form(base, a) if a and same_person_form(base, a) else None
            if c:
                llm_forms[k] = decline(base, c)     # the LLM tells the case, the form is ours
                ok += 1
        stats['llm_cases'] = ok
        return ok

    find.stats = stats
    find.prepare = prepare
    find.marks = marks
    return find


def anonymize_text(text: str, db_path, session_id: str, use_spacy: bool = True,
                   use_llm: bool = False):
    """Plain-text anonymization. Returns (masked_text, occurrences {token: [forms]})."""
    from core.db import save_places
    _, reps = anonymize_text_pipeline(text, db_path, session_id, use_spacy=use_spacy, use_llm=use_llm)
    log, places = [], []
    out = apply_spans(text, make_finder(reps, log, places)(text))
    save_places(db_path, session_id, places, file_key='__text__')
    occ: Dict[str, list] = {}
    for tok, orig in log:
        occ.setdefault(tok, []).append(orig)
    return out, occ


def restore_text(text: str, db_path, session_id: str, occurrences: dict = None) -> str:
    return apply_spans(text, make_rev_finder(db_path, session_id)(text))


def apply_reverse(text: str, reverse_map: dict) -> str:
    """Replace tokens with original values. Tolerates the ways an external LLM
    rewrites tokens: [FIO_1], FIO_1, [FIO 1], [FIO-1], [fio_1], [ФИО_1], [FIO_1]у."""
    if not reverse_map:
        return text
    return apply_spans(text, reverse_spans(text, reverse_map))
