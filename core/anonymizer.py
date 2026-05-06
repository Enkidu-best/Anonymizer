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
# These are missing/broken in Python 3.12 venvs with modern setuptools.

def _make_pkg_resources_polyfill():
    import types
    import importlib.metadata as _m

    pkg = types.ModuleType('pkg_resources')

    def _iter_ep(group):
        for dist in _m.distributions():
            eps = dist.entry_points
            if isinstance(eps, dict):
                yield from eps.get(group, [])
            else:
                yield from (ep for ep in eps if getattr(ep, 'group', '') == group)

    class _WorkingSet:
        def __iter__(self):
            return iter(_m.distributions())
        def iter_entry_points(self, group):
            yield from _iter_ep(group)

    pkg.iter_entry_points    = _iter_ep
    pkg.WorkingSet           = _WorkingSet
    pkg.DistributionNotFound = Exception
    pkg.get_distribution     = lambda n: type('D', (), {
        'project_name': n, 'version': _m.version(n)})()
    return pkg


try:
    import pkg_resources
    # Verify it actually works — broken on Python 3.12 + old setuptools
    pkg_resources.WorkingSet()
    list(pkg_resources.iter_entry_points('pymorphy2_dicts'))
except (ModuleNotFoundError, AttributeError, Exception):
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
    r'\[(?:FIO|YUL|INN|OGRN|KPP|RS|KS|BIK|SNILS|PASSPORT|TEL|EMAIL|SWIFT|ДАТАРОЖД)_\d+\]'
)

def _wrap(token: str) -> str:
    return f'[{token}]'

def _is_bracketed_token(text: str) -> bool:
    return bool(TOKEN_INNER_RE.fullmatch(text.strip()))


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
    r'товариществ\w+\s+собственников\s+жиль\w+|'
    r'садов\w+\s+некоммерческ\w+\s+товариществ\w+|'
    r'огородническ\w+\s+некоммерческ\w+\s+товариществ\w+'
    r')'
)

_OPF_SHORT = (
    r'(?:ООО|ПАО|НАО|ЗАО|ОАО|АО|ГУП|МУП|ФГУП|ФГБУ|ФГАОУ|'
    r'АНО|НП|НКО|КБ|ИП|ТСЖ|СНТ|ОНТ|ПК|ПотК)'
)

_QO = r'[«""\u201c\u00ab\']'
_QC = r'[»""\u201d\u00bb\']'

# Group 1 = OPF prefix+quote (to keep), Group 2 = inner name (to replace), Group 3 = closing quote (to keep)
_OPF_RE = _p(
    r'((?:' + _OPF_FULL + r'|' + _OPF_SHORT + r')\s+(?:' + _OPF_FULL + r'\s+|' + _OPF_SHORT + r'\s+)?'
    + _QO + r')([^»""\'"\u201d\u00bb\n]{2,80})(' + _QC + r')'
)


def _apply_opf_replacements(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    """
    Find OPF + «Name» patterns. Replace only the inner name with a token.
    Returns (new_text, {inner_name: [YUL_N]}).
    """
    from core.db import get_or_create_token

    replacements = {}
    result = []
    last = 0

    for m in _OPF_RE.finditer(text):
        prefix    = m.group(1)   # e.g.  "ООО «"
        inner     = m.group(2)   # e.g.  "НОВЫЕ ВЫСТАВКИ"
        suffix    = m.group(3)   # e.g.  "»"
        inner_s   = inner.strip()

        if len(inner_s) < 2 or _is_bracketed_token(inner_s):
            continue

        token = _wrap(get_or_create_token(db_path, session_id, inner_s, 'ЮЛ'))
        replacements[inner_s] = token

        result.append(text[last:m.start(2)])  # everything up to inner name
        result.append(token)
        last = m.end(2)

    result.append(text[last:])
    return ''.join(result), replacements


# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns for structured entities
# ─────────────────────────────────────────────────────────────────────────────

_NUM = r'(?:№|No\.?|N)\s*'

_MONTHS_RU = (
    r'(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[йя]\w*|июн\w*|'
    r'июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)'
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
            r'(?:р(?:асч)?\.?\s*/\s*с(?:ч(?:[её]т)?)?\b|расч[её]тн\w*\s+сч[её]т\w*)'
            r'\s*[:=]?\s*' + _NUM + r'?(\d{20})\b'
        ), 1),
    ]),
    ('КС', [
        (_p(
            r'(?:к(?:ор(?:р)?)?\.?\s*/\s*с(?:ч(?:[её]т)?)?\b|'
            r'корр?\.\s*сч[её]т\w*|корреспондентск\w+\s+сч[её]т\w*)'
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
    ('SWIFT', [
        (_p(r'SWIFT\s*[-:]?\s*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b'), 1),
    ]),
    ('ДАТАРОЖД', [
        (_p(r'\b(\d{1,2}\s+' + _MONTHS_RU + r'\s+\d{4})\s+(?:года?\s+рожд\w+|рожд\w+)'), 1),
        (_p(r'(?:рожд[её]н\w*\s+)(\d{1,2}\s+' + _MONTHS_RU + r'\s+\d{4})\b'), 1),
        (_p(r'дата\s+рождени\w+\s*[:\s]\s*(\d{1,2}[./]\d{1,2}[./]\d{2,4})\b'), 1),
    ]),
]


def _apply_regex_replacements(text: str, db_path, session_id: str) -> Tuple[str, Dict[str, str]]:
    """Run all regex patterns, replace matched values with tokens, return (new_text, reps)."""
    from core.db import get_or_create_token

    # Collect all matches first (avoid modifying text while iterating)
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
                if any(not (e <= us or s >= ue) for us, ue in used):
                    continue

                matches.append((s, e, value, entity_type))
                used.append((s, e))

    if not matches:
        return text, {}

    # Sort by position, build replacement dict
    matches.sort(key=lambda x: x[0])
    replacements = {}
    for _, _, value, etype in matches:
        if value not in replacements:
            token = _wrap(get_or_create_token(db_path, session_id, value, etype))
            replacements[value] = token

    # Apply replacements (longest first to avoid partial matches)
    for orig, tok in sorted(replacements.items(), key=lambda x: -len(x[0])):
        text = text.replace(orig, tok)

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
    Skip spans that overlap with existing tokens.
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
    except Exception:
        return text, {}

    # Find positions of existing tokens so NER doesn't re-process them
    token_spans = [(m.start(), m.end()) for m in TOKEN_INNER_RE.finditer(text)]

    replacements = {}
    for span in doc.spans:
        if span.type not in ('PER', 'ORG'):
            continue

        s, e = span.start, span.stop
        original = span.text.strip()

        # Skip if overlaps with an existing token
        if any(not (e <= ts or s >= te) for ts, te in token_spans):
            continue
        # Skip very short or already-tokenized
        if len(original) < 3 or _is_bracketed_token(original):
            continue

        etype = 'ФИО' if span.type == 'PER' else 'ЮЛ'
        try:
            span.normalize(_morph_vocab)
            canon = span.normal
        except Exception:
            canon = original

        if original not in replacements:
            token = _wrap(get_or_create_token(db_path, session_id, canon, etype))
            replacements[original] = token

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
        except Exception:
            pass

    return text, all_reps


def apply_reverse(text: str, reverse_map: dict) -> str:
    """Replace [TOKEN] back to original values. Also handles bare TOKEN for backward compat."""
    if not reverse_map:
        return text
    # Build both bracketed and bare mappings
    combined = {}
    for tok, orig in reverse_map.items():
        combined[f'[{tok}]'] = orig   # new format
        combined[tok]        = orig   # old format (backward compat)
    for token, orig in sorted(combined.items(), key=lambda x: -len(x[0])):
        text = text.replace(token, orig)
    return text