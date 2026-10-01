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
        return org_key(text)
    return re.sub(r'\s+', ' ', _yo(text)).strip(' .,;:')


def same_person(a: str, b: str) -> bool:
    pa, pb = Person.parse(a), Person.parse(b)
    if not (pa.surname or pa.name) or not (pb.surname or pb.name):
        return False
    return pa.compatible(pb)
