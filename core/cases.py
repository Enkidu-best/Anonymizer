"""Case of a person's name inserted into NEW text of an external LLM (task 3, §2.3).

Only persons are declined: company names in quotes, numbers, addresses, dates are not.

* `target_case(left, right)` — the case the place requires, from the word on the left
  (preposition, verb, «в лице») or the subject position; None when the rules can't tell.
* `decline(name, case)` — «Белозёров Аркадий Львович» → «Белозёрову Аркадию Львовичу».
  First names via pymorphy3 (they are in the dictionary); surnames and patronymics by
  their endings (pymorphy3 does not know rare surnames: «Белозёров» → plural of «белозёр»).
* `same_person_form(a, b)` — the LLM answer is accepted only if it is a form of the same name.
"""
import re
from typing import Optional

from core.entities import Person, _morph, surname_like

CASES = ('nomn', 'gent', 'datv', 'accs', 'ablt', 'loct')

# preposition / fixed phrase on the left → case
_PREP = {
    'у': 'gent', 'для': 'gent', 'от': 'gent', 'без': 'gent', 'до': 'gent', 'из': 'gent', 'около': 'gent',
    'против': 'gent', 'кроме': 'gent', 'вместо': 'gent', 'среди': 'gent', 'возле': 'gent',
    'к': 'datv', 'ко': 'datv', 'по': 'datv', 'согласно': 'datv', 'вопреки': 'datv', 'благодаря': 'datv',
    'с': 'ablt', 'со': 'ablt', 'между': 'ablt', 'над': 'ablt', 'перед': 'ablt', 'под': 'ablt',
    'о': 'loct', 'об': 'loct', 'обо': 'loct', 'при': 'loct',
}
_PHRASE = [
    (r'в\s+лице', 'gent'), (r'от\s+имени', 'gent'), (r'со\s+стороны', 'gent'), (r'в\s+пользу', 'gent'),
    (r'в\s+отношении', 'gent'), (r'на\s+имя', 'gent'), (r'по\s+поручению', 'gent'),
    (r'за\s+подписью', 'gent'), (r'в\s+адрес', 'gent'), (r'по\s+доверенности', 'gent'),
    (r'в\s+интересах', 'gent'), (r'на\s+стороне', 'gent'), (r'с\s+участием', 'gent'),
    (r'(?:подпис[ьи]|паспорт\w*|заявлени\w+|согласи\w+|доля|доли|долю|имущества?|наследник\w*|'
     r'представител\w+|супруг\w*|полномочи\w+)', 'gent'),
]
# verbs: whom (dative) / whom (accusative)
_DAT_VERBS = {'передать', 'передавать', 'направить', 'направлять', 'сообщить', 'сообщать', 'предоставить',
              'предоставлять', 'выплатить', 'выплачивать', 'вручить', 'поручить', 'отправить', 'написать',
              'позвонить', 'предложить', 'разъяснить', 'рекомендовать', 'напомнить', 'выдать', 'возвратить',
              'вернуть', 'перечислить', 'адресовать', 'принадлежать', 'отказать', 'ответить', 'продать',
              'подарить', 'уступить', 'доверить', 'компенсировать', 'возместить', 'выставить', 'направляться'}
_ACC_VERBS = {'уведомить', 'уведомлять', 'известить', 'извещать', 'назначить', 'попросить', 'просить',
              'обязать', 'привлечь', 'пригласить', 'проинформировать', 'информировать', 'признать',
              'освободить', 'уполномочить', 'опросить', 'допросить', 'включить', 'исключить',
              'представлять', 'представить', 'заменить', 'указать', 'считать', 'благодарить'}


def _words(s):
    return re.findall(r'[А-ЯЁа-яёA-Za-z\-]+', s)


def _is_finite_verb(word: str) -> bool:
    m = _morph()
    if not m:
        return False
    ps = m.parse(word.lower())
    return bool(ps) and 'VERB' in ps[0].tag and 'INFN' not in ps[0].tag


def _verb_lemma(word: str) -> Optional[str]:
    m = _morph()
    if not m:
        return None
    for p in m.parse(word.lower()):
        if p.tag.POS in ('VERB', 'INFN', 'PRTS', 'GRND'):
            return p.normal_form
    return None


def target_case(left: str, right: str) -> Optional[str]:
    """Case of a name at a place: left/right = text of the sentence around the token."""
    left_s = left.rstrip()
    lw = _words(left_s[-60:])
    rw = _words(right[:40])
    # beginning of a place, a list item, after a colon or dash: a subject / a label
    if not lw or re.search(r'[.:;!?—–•\-\*\(]\s*$', left_s) or re.search(r'(?:^|\n)\s*$', left):
        return 'nomn'
    tail = ' '.join(lw[-3:]).lower()
    for rx, case in _PHRASE:
        if re.search(r'(?:^|\s)' + rx + r'$', tail):
            return case
    last = lw[-1].lower()
    if last in _PREP and not re.search(r',\s*$', left_s):
        return _PREP[last]
    v = _verb_lemma(last)
    if v in _DAT_VERBS:
        return 'datv'
    if v in _ACC_VERBS:
        return 'accs'
    # «…, [FIO_1] подписал» — a subject before its verb
    if re.search(r',\s*$', left_s) and rw and _is_finite_verb(rw[0]):
        return 'nomn'
    if last == 'и' and rw and _is_finite_verb(rw[0]):
        return 'nomn'
    noun = _noun(last)
    if noun is not None and not re.search(r',\s*$', left_s):
        anim, case = noun
        if anim:
            return case          # apposition: «нотариусом [X]», «предпринимателя [X]», «директор [X]»
        if rw and _is_finite_verb(rw[0]):
            return 'nomn'        # «По условиям договора [X] обязуется»
        return 'gent'            # «семьей [X]», «долю [X]» — whose
    return None


_PERSON_NOUNS = {'гражданин', 'гражданка', 'предприниматель', 'директор', 'нотариус', 'представитель',
                 'руководитель', 'фигурант', 'поручитель', 'заемщик', 'заёмщик', 'участник', 'учредитель',
                 'бенефициар', 'супруг', 'супруга', 'наследник', 'ответчик', 'истец', 'заявитель',
                 'управляющий', 'председатель', 'адвокат', 'юрист', 'бухгалтер', 'менеджер', 'сотрудник',
                 'работник', 'покупатель', 'продавец', 'арендатор', 'арендодатель', 'заказчик', 'исполнитель',
                 'подрядчик', 'залогодатель', 'залогодержатель', 'доверитель', 'поверенный', 'собственник'}


def _noun(word: str):
    """(animate, case) of a noun right before a name, or None (not a noun / unclear)."""
    m = _morph()
    if not m:
        return None
    ps = [p for p in m.parse(word.lower()) if p.tag.POS == 'NOUN']
    if not ps or m.parse(word.lower())[0].tag.POS != 'NOUN':
        return None
    p = ps[0]
    case = p.tag.case
    if case not in CASES:
        return None
    anim = 'anim' in p.tag or p.normal_form in _PERSON_NOUNS
    if anim and 'plur' in p.tag:
        return None
    return anim, case


def case_of_form(base: str, form: str) -> Optional[str]:
    """Which case `form` of the name `base` is in — word by word against our own declension
    (the LLM tells the case; the form is built here, so «Ветров Германом» never gets in)."""
    fw = [w.lower() for w in _words(form)]
    best, score = None, 0
    for c in CASES:
        cw = [w.lower() for w in _words(decline(base, c))]
        if len(cw) != len(fw):
            continue
        k = sum(a == b for a, b in zip(cw, fw))
        if k > score:
            best, score = c, k
    return best


# ── declension ────────────────────────────────────────────────────────────────

def _gender(words, roles) -> str:
    for w, r in zip(words, roles):
        lw = w.lower()
        if r == 'patr':
            return 'f' if re.search(r'(?:вн|чн|ичн)[аыеуо]й?$|на$', lw) else 'm'
    m = _morph()
    for w, r in zip(words, roles):
        if r == 'name' and m:
            for p in m.parse(w.lower()):
                if 'Name' in p.tag:
                    return 'f' if 'femn' in p.tag else 'm'
    for w, r in zip(words, roles):
        if r == 'surn' and re.search(r'(?:ова|ева|ёва|ина|ына|ская|цкая|ой)$', w.lower()):
            return 'f'
    return 'm'


def _roles(words):
    """'surn' / 'name' / 'patr' per word (initials are not in `words`)."""
    m = _morph()
    roles = []
    for w in words:
        cap = w[:1].upper() + w[1:].lower()
        ps = m.parse(cap) if m else []
        if 'name' in roles and 'patr' not in roles and (
                any('Patr' in p.tag for p in ps) or re.search(r'(?:ович|евич|ич|овн|евн|ичн)', w.lower())):
            roles.append('patr')
        elif 'name' not in roles and any('Name' in p.tag and 'sing' in p.tag for p in ps[:2]) and \
                not any('Surn' in p.tag for p in ps[:1]) and \
                not ('surn' not in roles and surname_like(cap, [x for x in words if x != w])):
            roles.append('name')
        else:
            roles.append('surn')
    return roles


_ENDINGS = {
    # gender, stem type → endings for nomn, gent, datv, accs, ablt, loct
    ('m', 'ov'): ('', 'а', 'у', 'а', 'ым', 'е'),
    ('f', 'ov'): ('а', 'ой', 'ой', 'у', 'ой', 'ой'),
    ('m', 'sk'): ('ий', 'ого', 'ому', 'ого', 'им', 'ом'),
    ('f', 'sk'): ('ая', 'ой', 'ой', 'ую', 'ой', 'ой'),
    ('m', 'cons'): ('', 'а', 'у', 'а', 'ом', 'е'),
    ('m', 'hush'): ('', 'а', 'у', 'а', 'ем', 'е'),
    ('m', 'patr'): ('', 'а', 'у', 'а', 'ем', 'е'),
    ('f', 'patr'): ('а', 'ы', 'е', 'у', 'ой', 'е'),
}


def _surname_stem(w: str, g: str):
    lw = w.lower()
    mo = re.match(r'(.+(?:ов|ев|ёв|ин|ын))(?:а|у|ым|е|ой|ою|ую)?$', lw)
    if mo and len(mo.group(1)) >= 3:
        return mo.group(1), 'ov'
    mo = re.match(r'(.+(?:ск|цк))(?:ий|ого|ому|им|ом|ая|ой|ую|ою)$', lw)
    if mo:
        return mo.group(1), 'sk'
    if g == 'm':      # instrumental «-ом/-ем» ends in a consonant too: strip it first
        mo = re.match(r'(.+[бвгджзклмнпрстфхцчшщ])(?:ом|ем)$', lw)
        if mo and len(mo.group(1)) >= 4:
            return mo.group(1), 'hush' if re.search(r'[жшчщц]$', mo.group(1)) else 'cons'
    if g == 'm' and re.search(r'[жшчщц]$', lw):
        return lw, 'hush'                   # «Гуревич» → «Гуревичем», not «Гуревичом»
    if g == 'm' and re.search(r'[бвгджзклмнпрстфхцчшщ]$', lw):
        return lw, 'cons'
    if g == 'm':
        mo = re.match(r'(.+[бвгджзклмнпрстфхцчшщ])(?:а|у|ом|ем|е)$', lw)
        if mo and len(mo.group(1)) >= 4:
            return mo.group(1), 'hush' if re.search(r'[жшчщц]$', mo.group(1)) else 'cons'
    return None, None                       # indeclinable («Шевченко», «Черных», female «Кацнельсон»)


def _patr_stem(w: str):
    lw = w.lower()
    mo = re.match(r'(.+(?:ович|евич|ич))(?:а|у|ем|е)?$', lw)
    if mo:
        return mo.group(1), 'm'
    mo = re.match(r'(.+(?:овн|евн|ичн|инич))(?:а|ы|е|у|ой)$', lw)
    if mo:
        return mo.group(1), 'f'
    return None, None


def _like(src: str, dst: str) -> str:
    if src.isupper() and len(src) > 1:
        return dst.upper()
    if src[:1].isupper():
        return dst[:1].upper() + dst[1:]
    return dst


def _decline_word(w: str, role: str, g: str, case: str) -> str:
    i = CASES.index(case)
    if role == 'patr':
        stem, pg = _patr_stem(w)
        return _like(w, stem + _ENDINGS[(pg, 'patr')][i]) if stem else w
    if role == 'surn':
        stem, kind = _surname_stem(w, g)
        if not stem or (g, kind) not in _ENDINGS:
            return w
        return _like(w, stem + _ENDINGS[(g, kind)][i])
    m = _morph()
    if not m:
        return w
    want = {'femn' if g == 'f' else 'masc'}
    for p in m.parse(w.lower()):
        if 'Name' in p.tag and (want & set(str(p.tag).replace(' ', ',').split(','))):
            f = p.inflect({case, 'sing'})
            if f:
                return _like(w, f.word)
    return w


def decline(name: str, case: str) -> str:
    """The name in `case`; initials and unknown words stay as they are."""
    if case not in CASES:
        return name
    parts = re.split(r'([А-ЯЁA-Za-zа-яё][А-ЯЁA-Za-zа-яё\-]+)', name)
    words = [p for i, p in enumerate(parts) if i % 2 == 1]
    if not words:
        return name
    roles = _roles(words)
    g = _gender(words, roles)
    out, k = [], 0
    for i, p in enumerate(parts):
        if i % 2 == 1:
            sub = p.split('-')
            out.append('-'.join(_decline_word(s, roles[k], g, case) if s else s for s in sub))
            k += 1
        else:
            out.append(p)
    return ''.join(out)


def same_person_form(a: str, b: str) -> bool:
    """b is a form of the same name as a: same surname key and compatible name/initials."""
    pa, pb = Person.parse(a), Person.parse(b)
    return bool(pa.surname) and pa.surname == pb.surname and pa.compatible(pb) and \
        len(_words(a)) == len(_words(b))
