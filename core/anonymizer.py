"""
Entity detection — sequential pipeline:
  Pass 1 – Regex   (INN, OGRN, accounts, phones, OPF org names)
  Pass 2 – Natasha NER on ALREADY-anonymized text (finds what regex missed)
  Pass 3 – LLM via Ollama (optional)

Token format: [FIO_1], [INN_2], [YUL_3] etc.
Tokens are recognizable by NER/LLM so they skip already-processed spans.

anonymize_text_sequential() returns (anonymized_text, replacements_dict)
so DOCX/PDF/XLSX handlers can apply the SAME replacements to their own content.
"""

import re
import sys
import threading
from dataclasses import dataclass
from typing import List, Tuple, Dict

# ── pkg_resources polyfill ────────────────────────────────────────────────────
# pymorphy2 uses pkg_resources.WorkingSet + iter_entry_points.
# These are missing/broken in Python 3.12+ venvs and in PyInstaller frozen exes.

def _make_pkg_resources_polyfill():
    import types
    import importlib.metadata as _m

    pkg = types.ModuleType('pkg_resources')

    def _iter_ep(group):
        # In PyInstaller frozen exe, importlib.metadata may find nothing.
        # Try direct import of pymorphy2_dicts as a reliable fallback.
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
        # Normal path: iterate installed distributions
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
    # Always polyfill in PyInstaller frozen exe — pkg_resources can't scan bundled dists
    if getattr(sys, 'frozen', False):
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
    original:    str   # exact text in document
    canonical:   str   # normalised (nominative for names)
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
    r'|ДАТАРОЖД)_\d+\]'
)

def _wrap(token: str) -> str:
    return f'[{token}]'

def _is_bracketed_token(text: str) -> bool:
    return bool(TOKEN_INNER_RE.fullmatch(text.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# FIO blocklist — job titles / legal roles that NER misclassifies as persons
# ─────────────────────────────────────────────────────────────────────────────

_FIO_BLOCKLIST = frozenset({
    # Legal roles
    'аудитор',            # аудитор
    'бенефициар',       # бенефициар
    'принципал',          # принципал
    'продавец',            # продавец
    'покупатель',         # покупатель
    'займодавец',          # займодавец
    'заёмщик',            # заёмщик
    'заемщик',            # заемщик
    'залогодатель',       # залогодатель
    'залогодержатель',    # залогодержатель
    'поручитель',         # поручитель
    'должник',            # должник
    'кредитор',           # кредитор
    'цедент',               # цедент
    'цессионарий',        # цессионарий
    'лицензиар',          # лицензиар
    'лицензиат',          # лицензиат
    'арендодатель',       # арендодатель
    'арендатор',          # арендатор
    'исполнитель',        # исполнитель
    'заказчик',           # заказчик
    'подрядчик',          # подрядчик
    'субподрядчик',       # субподрядчик
    'комитент',           # комитент
    'комиссионер',        # комиссионер
    'доверитель',          # доверитель
    'поверенный',          # поверенный
    'хранитель',          # хранитель
    'поклажедатель',      # поклажедатель
    'перевозчик',          # перевозчик
    'отправитель',        # отправитель
    'получатель',         # получатель
    'страховщик',          # страховщик
    'страхователь',       # страхователь
    'выгодоприобретатель',  # выгодоприобретатель
    'агент',               # агент
    'гарант',              # гарант
    'плательщик',          # плательщик
    'нотариус',           # нотариус
    'регистратор',        # регистратор
    'депозитарий',        # депозитарий
    'акционер',          # акционер
    'участник',           # участник
    'учредитель',         # учредитель
    'директор',           # директор
    'президент',          # президент
    'председатель',       # председатель
    'секретарь',          # секретарь
    'бухгалтер',          # бухгалтер
    'юрист',               # юрист
    'адвокат',            # адвокат
    'представитель',       # представитель
    'сторона',             # сторона
    'стороны',            # стороны
    'общество',            # общество
    'организация',         # организация
    'предприятие',         # предприятие
    'банк',                # банк
})


# ─────────────────────────────────────────────────────────────────────────────
# OPF patterns
# Captures ONLY the inner name between quotes; OPF prefix stays in text.
# Result: "ООО «НОВЫЕ ВЫСТАВКИ»"  →  "ООО «[YUL_1]»"
# ─────────────────────────────────────────────────────────────────────────────

_OPF_FULL = (
    r'(?:общест\w+\s+с\s+ограниченной\s+ответственност\w+|'
    r'публичн\w+\s+акционерн\w+\s+общест\w+|'
    r'непубличн\w+\s+акционерн\w+\s+общест\w+|'
    r'закрыт\w+\s+акционерн\w+\s+общест\w+|'
    r'открыт\w+\s+акционерн\w+\s+общест\w+|'
    r'акционерн\w+\s+общест\w+|'
    r'государственн\w+\s+унитарн\w+\s+предприяти\w+|'
    r'муниципальн\w+\s+унитарн\w+\s+предприяти\w+|'
    r'федеральн\w+\s+государственн\w+\s+унитарн\w+\s+предприяти\w+|'
    r'федеральн\w+\s+государственн\w+\s+бюджетн\w+\s+учрежден\w+|'
    r'автономн\w+\s+некоммерческ\w+\s+организаци\w+|'
    r'некоммерческ\w+\s+партнерств\w+|'
    r'производственн\w+\s+кооператив\w+|'
    r'потребительск\w+\s+кооператив\w+|'
    r'коммерческ\w+\s+банк\w+|'
    r'индивидуальн\w+\s+предпринимател\w+|'
    r'товариществ\w+\s+собственников\s+жил\w+|'
    r'садов\w+\s+некоммерческ\w+\s+товариществ\w+|'
    r'огородническ\w+\s+некоммерческ\w+\s+товариществ\w+'
    r')'
)

_OPF_SHORT = (
    r'(?:ООО|ПАО|НАО|ЗАО|ОАО|АО|ГУП|МУП|ФГУП|ФГБУ|ФГАОУ|'
    r'АНО|НП|НКО|КБ|ИП|ТСЖ|СНТ|ОНТ|ПК|ПотК)'
)

_OPF_PFX = (
    r'(?:' + _OPF_FULL + r'|' + _OPF_SHORT + r')'
    r'\s+(?:(?:' + _OPF_FULL + r'|' + _OPF_SHORT + r')\s+)?'
)

# Angle-bracket outer «»: inner may contain straight " but NOT closing »
_OPF_RE_ANGLE = _p(
    r'(' + _OPF_PFX + r'«)'          # group 1: OPF + «
    r'([^»\n]{2,80})'                  # group 2: inner name
    r'(»)'                             # group 3: closing »
)
# Straight/curly outer " ": inner may contain «» but NOT closing "
_OPF_RE_STRAIGHT = _p(
    r'(' + _OPF_PFX + r'[“"])'        # group 1: OPF + " or "
    r'([^”"\n]{2,80})'                 # group 2: inner name
    r'([”"])'                          # group 3: closing " or "
)


def _apply_opf_replacements(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    """
    Find OPF + «Name» patterns (both angle and straight quotes).
    Replace only the inner name with a token; OPF prefix stays in text.
    Returns (new_text, {inner_name: [YUL_N]}).
    """
    from core.db import get_or_create_token

    all_matches = []
    for pattern in (_OPF_RE_ANGLE, _OPF_RE_STRAIGHT):
        for m in pattern.finditer(text):
            inner_s = m.group(2).strip()
            if len(inner_s) < 2 or _is_bracketed_token(inner_s):
                continue
            all_matches.append((m.start(2), m.end(2), inner_s))

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
# Regex patterns for structured entities
# ─────────────────────────────────────────────────────────────────────────────

_NUM = r'(?:№|No\.?|N)\s*'

_MONTHS_RU = (
    r'(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|'
    r'июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)'
)

_ADR_KEYWORDS = (
    r'(?:адрес(?:у|е)?'
    r'|(?:проживает|зарегистрирован(?:а)?)\s+по\s+адресу)'
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
    ]),
    ('КС', [
        (_p(
            r'(?:к(?:ор(?:р)?)?\.?\s*/\s*с(?:ч(?:[ёе]т)?)?\b|'
            r'корр?\.\s*сч[ёе]т\w*|корреспондентск\w+\s+сч[ёе]т\w*)'
            r'\s*[:=]?\s*' + _NUM + r'?(\d{20})\b'
        ), 1),
    ]),
    ('БИК', [
        (_p(r'БИК\s*[:=]?\s*(\d{9})\b'), 1),
    ]),
    ('СНИЛС', [
        (_p(r'\b(\d{3}-\d{3}-\d{3}\s+\d{2})\b'), 1),
        (_p(r'СНИЛС\s*[:=]?\s*(\d{11})\b'), 1),
    ]),
    ('ПАСПОРТ', [
        (_p(r'паспорт\w*\s+(?:серии?\s+)?(\d{2}\s*\d{2})\s*,?\s*(?:' + _NUM + r')?(\d{6})\b'), 0),
        (_p(r'серии?\s+(\d{2}\s+\d{2})[,;\s]+(?:' + _NUM + r')(\d{6,9})\b'), 0),
        (_p(r'серии?\s+(\d{2,4})\s+(?:' + _NUM + r')(\d{6,9})\b'), 0),
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
    # АДРЕС — three patterns each requiring a substantive address component:
    #   1. starts with 6-digit postcode
    #   2. starts with city keyword
    #   3. street type + house number
    ('АДРЕС', [
        # Pattern A: postcode present (most reliable signal)
        (_p(
            _ADR_KEYWORDS +
            r'(\d{6}[,\s]+[\w\s\.,\-/]{10,300})'
        ), 1),
        # Pattern B: city keyword present (optional RF/Russian Federation prefix)
        (_p(
            _ADR_KEYWORDS +
            r'((?:(?:Р(?:оссийская\s+)?Федерация|РФ)[,\s]+)?'
            r'г(?:ород)?\.?\s+[\w\-]{2,30}[,\s]+'
            r'[\w\s\.,\-/]{5,300})'
        ), 1),
        # Pattern C: street type + house number (definitive address signal)
        (_p(
            _ADR_KEYWORDS +
            r'((?:ул(?:ица)?'
            r'|пр(?:оспект)?'
            r'|пер(?:еулок)?'
            r'|бул(?:ьвар)?'
            r'|наб(?:ережная)?'
            r'|ш(?:оссе)?'
            r'|пл(?:ощадь)?)\.?\s+'
            r'[\w\s\.\-]{2,50}[,\s]+'
            r'д(?:ом)?\.?\s*[\w/]+'
            r'[\w\s\.,\-/]{0,100})'
        ), 1),
    ]),
    ('SWIFT', [
        (_p(r'SWIFT\s*[-:]?\s*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b'), 1),
    ]),
    ('ДАТАРОЖД', [
        (_p(r'\b(\d{1,2}\s+' + _MONTHS_RU + r'\s+\d{4})\s+(?:года?\s+рожд\w+|рожд\w+)'), 1),
        (_p(r'(?:рожд[ёе]н\w*\s+)(\d{1,2}\s+' + _MONTHS_RU + r'\s+\d{4})\b'), 1),
        (_p(r'дата\s+рождени\w+\s*[:\s]\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})\b'), 1),
    ]),
]


def _apply_regex_replacements(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    """Run all regex patterns, replace matched values with tokens, return (new_text, reps)."""
    from core.db import get_or_create_token

    matches = []  # (start, end, value, entity_type)
    used    = []  # (start, end)

    for entity_type, patterns in REGEX_PATTERNS:
        for pat, grp in patterns:
            for m in pat.finditer(text):
                if grp == 0:
                    groups = [g for g in m.groups() if g]
                    if not groups:
                        continue
                    value = ' '.join(g.strip() for g in groups)
                    s, e  = m.start(), m.end()
                else:
                    try:
                        value = (m.group(grp) or '').strip()
                        s, e  = m.start(grp), m.end(grp)
                    except IndexError:
                        continue

                if not value:
                    continue
                # Minimum length for address captures to filter noise
                if entity_type == 'АДРЕС' and len(value) < 10:
                    continue
                if any(not (e <= us or s >= ue) for us, ue in used):
                    continue

                matches.append((s, e, value, entity_type))
                used.append((s, e))

    if not matches:
        return text, {}

    matches.sort(key=lambda x: x[0])
    replacements = {}
    for _, _, value, etype in matches:
        if value not in replacements:
            token = _wrap(get_or_create_token(db_path, session_id, value, etype))
            replacements[value] = token

    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)

    if replacements:
        by_type: Dict[str, int] = {}
        for _, _, _, etype in matches:
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
    Run Natasha NER on text that already has [TOKEN] placeholders.
    Skip spans that overlap with existing tokens or are in the FIO blocklist.
    Returns (new_text, additional_replacements).
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
    skipped_blocklist = 0
    for span in doc.spans:
        if span.type not in ('PER', 'ORG'):
            continue

        s, e = span.start, span.stop
        original = span.text.strip()

        if any(not (e <= ts or s >= te) for ts, te in token_spans):
            continue
        if len(original) < 3 or _is_bracketed_token(original):
            continue

        etype = 'ФИО' if span.type == 'PER' else 'ЮЛ'

        # Skip known legal roles / job titles
        if etype == 'ФИО' and original.lower().rstrip('.') in _FIO_BLOCKLIST:
            skipped_blocklist += 1
            continue

        try:
            span.normalize(_morph_vocab)
            canon = span.normal
        except Exception:
            canon = original

        if original not in replacements:
            token = _wrap(get_or_create_token(db_path, session_id, canon, etype))
            replacements[original] = token

    if replacements or skipped_blocklist:
        print(f'[NER] {len(replacements)} new entities'
              + (f', {skipped_blocklist} blocklist skips' if skipped_blocklist else ''))

    if replacements:
        for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
            text = text.replace(orig, tok)

    return text, replacements


# ─────────────────────────────────────────────────────────────────────────────
# Public API — sequential pipeline
# ─────────────────────────────────────────────────────────────────────────────

def anonymize_text_sequential(text: str, db_path, session_id: str,
                               use_llm: bool = False) -> Tuple[str, Dict[str, str]]:
    """
    Sequential anonymization. Returns (anonymized_text, all_replacements_dict).
    all_replacements_dict maps original_value -> [TOKEN] for use in DOCX/PDF/XLSX.
    """
    all_reps: Dict[str, str] = {}

    # Pass 1: OPF org names (regex, preserves OPF prefix in text)
    text, reps1 = _apply_opf_replacements(text, db_path, session_id)
    all_reps.update(reps1)

    # Pass 2: Structured regex (INN, OGRN, phone, etc.)
    text, reps2 = _apply_regex_replacements(text, db_path, session_id)
    all_reps.update(reps2)

    # Pass 3: NER on already-anonymized text
    text, reps3 = _apply_ner_replacements(text, db_path, session_id)
    all_reps.update(reps3)

    # Pass 4: LLM (optional)
    if use_llm:
        try:
            from core.llm import apply_llm_pass
            text, reps4 = apply_llm_pass(text, db_path, session_id)
            all_reps.update(reps4)
        except Exception as ex:
            print(f'[LLM] Pass failed: {ex}')

    return text, all_reps


def apply_reverse(text: str, reverse_map: dict) -> str:
    """Replace [TOKEN] back to original values. Also handles bare TOKEN for backward compat."""
    if not reverse_map:
        return text
    combined = {}
    for tok, orig in reverse_map.items():
        combined[f'[{tok}]'] = orig
        combined[tok]        = orig
    for token, orig in sorted(combined.items(), key=lambda x: -len(x[0])):
        text = text.replace(token, orig)
    return text
