"""General categories of contract language that are never personal data (task 2, §2.1).

Not a list of words from someone's documents: these are the standard roles of
parties, job titles, public bodies and the shape of headings — the external LLM
needs them to understand the document. Words are compared by dictionary form
(pymorphy3), so every case form is covered («Арендатору», «Сторонами»).

A value the user added by hand is never filtered (callers pass manual=True).
"""
import re
from functools import lru_cache

# Roles of parties (dictionary forms)
ROLES = {
    'сторона', 'арендатор', 'арендодатель', 'субарендатор', 'субарендодатель', 'покупатель',
    'продавец', 'заказчик', 'исполнитель', 'подрядчик', 'субподрядчик', 'генподрядчик',
    'займодавец', 'заимодавец', 'заемщик', 'заёмщик', 'кредитор', 'должник', 'поручитель',
    'залогодатель', 'залогодержатель', 'цедент', 'цессионарий', 'агент', 'субагент', 'принципал',
    'комитент', 'комиссионер', 'лицензиар', 'лицензиат', 'сублицензиат', 'управляющий',
    'доверитель', 'поверенный', 'участник', 'общество', 'банк', 'истец', 'ответчик',
    'заявитель', 'гарант', 'бенефициар', 'принципал', 'эмитент', 'инвестор', 'учредитель',
    'акционер', 'страховщик', 'страхователь', 'выгодоприобретатель', 'перевозчик',
    'грузоотправитель', 'грузополучатель', 'поставщик', 'дистрибьютор', 'дилер', 'франчайзер',
    'франчайзи', 'правообладатель', 'наймодатель', 'наниматель', 'работодатель', 'работник',
    'клиент', 'контрагент', 'хранитель', 'поклажедатель', 'ссудодатель', 'ссудополучатель',
    'даритель', 'одаряемый', 'наследодатель', 'наследник', 'партнер', 'партнёр',
    'концессионер', 'концедент', 'оператор', 'посредник', 'покупатель-', 'сторона-',
}
# Job titles and positions (dictionary forms of the head word)
POSITIONS = {
    'директор', 'председатель', 'бухгалтер', 'президент', 'вице-президент', 'член', 'секретарь',
    'представитель', 'нотариус', 'руководитель', 'заместитель', 'начальник', 'управляющий',
    'менеджер', 'юрист', 'адвокат', 'советник', 'консультант', 'аудитор', 'ревизор',
    'казначей', 'администратор', 'ликвидатор', 'конкурсный', 'арбитражный', 'исполняющий',
    'генеральный', 'главный', 'финансовый', 'исполнительный', 'коммерческий', 'технический',
    'правление', 'совет', 'собрание', 'комиссия', 'дирекция', 'единоличный',
}
# Public bodies, courts, state registries (not secret; lower-case lemma phrases)
PUBLIC_BODIES = re.compile(
    r'(?:центральн\w*\s+банк|банк\w*\s+росси|^цб(?:\s+рф)?$|росреестр|федеральн\w*\s+налогов|'
    r'^и?фнс\b|^мифнс\b|министерств|^мин(?:фин|юст|эконом|труд|здрав|обр|цифр|промторг|энерго)\w*$|'
    r'правительств|арбитражн\w*\s+суд|верховн\w*\s+суд|конституционн\w*\s+суд|районн\w*\s+суд|'
    r'городск\w*\s+суд|федеральн\w*\s+служб|управлени\w*\s+федеральн|государственн\w*\s+дум|'
    r'пенсионн\w*\s+фонд|социальн\w*\s+фонд|^пфр$|^сфр$|^фсс$|прокуратур|^мвд\b|^фссп\b|^асв\b|'
    r'агентств\w*\s+по\s+страхованию|^бти$|бюро\s+технической\s+инвентаризации|нотариальн\w*\s+палат|'
    r'^егрюл$|^егрип$|^егрн$|^егрп$|^уфмс\b|^гибдд\b|^фас\b|^роспотребнадзор|^росимуществ|'
    r'^казначейств|^фнс\s+росси|^банк\s+россии$)', re.IGNORECASE)


@lru_cache(maxsize=1)
def _morph():
    try:
        import pymorphy3
        return pymorphy3.MorphAnalyzer()
    except Exception:
        return None


@lru_cache(maxsize=50000)
def lemmas(word: str) -> frozenset:
    m = _morph()
    w = word.lower().replace('ё', 'е')
    if m is None:
        return frozenset({w})
    return frozenset(p.normal_form.replace('ё', 'е') for p in m.parse(w))


@lru_cache(maxsize=50000)
def name_like(word: str) -> bool:
    """Can be part of a person's name: dictionary surname/name/patronymic, an initial,
    or a capitalized word unknown to the dictionary (rare surname)."""
    w = word.strip('.,;:()«»"\'')
    if not w or re.fullmatch(r'[А-ЯЁA-Z]\.?', w):
        return True
    m = _morph()
    if m is None:
        return True
    cap = w.capitalize()
    # «Пастухов», «Кузнецов», «Конев»: a surname form that is also a plural genitive of
    # a common noun — the masculine surname ending decides (not «-ина/-ова»: «Витрина»)
    if w[:1].isupper() and w[1:].islower() and len(w) > 4 and \
            re.search(r'(?:[оеё]в|[иы]н|ск(?:ий|ой)|цк(?:ий|ой))$', w.lower()):
        return True
    for p in m.parse(cap):
        if {'Surn', 'Name', 'Patr'} & set(p.tag.grammemes):
            return True
    return not m.word_is_known(cap.lower())


_WORD = re.compile(r'[A-Za-zА-ЯЁа-яё][A-Za-zА-ЯЁа-яё\-]*')


def is_role_or_position(text: str) -> bool:
    """«Арендатора», «Стороной», «Генеральный директор», «Председатель Совета директоров»."""
    words = _WORD.findall(text)
    if not words or len(words) > 5:
        return False
    for w in words:
        lw = lemmas(w.lower())
        if not (lw & ROLES or lw & POSITIONS or w.lower() in ('и', 'по', 'о', 'в', 'с')):
            # «директоров», «правления» etc. are covered by lemmas of POSITIONS
            return False
    return True


def is_heading(text: str) -> bool:
    """Whole text in capitals without quotes and legal form: «ЦЕНА И ПОРЯДОК РАСЧЕТОВ»."""
    t = text.strip()
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 4 or any(c in t for c in '«»"“”'):
        return False
    if not all(c.isupper() for c in letters):
        return False
    words = _WORD.findall(t)
    # a heading is made of ordinary words; a name in capitals has name-like words
    return len(words) >= 2 and not any(name_like(w) for w in words if len(w) > 2) \
        or len(words) == 1 and not name_like(words[0])


def ordinary_words(text: str) -> bool:
    """Every word is an ordinary dictionary word without name grammemes («Витрина»,
    «Арендодателя Арендная») — cannot be a person."""
    words = [w for w in _WORD.findall(text) if len(w) > 1]
    return bool(words) and not any(name_like(w) for w in words)


def is_public_body(text: str) -> bool:
    t = re.sub(r'\s+', ' ', text.strip().strip('«»"\'').lower())
    return bool(PUBLIC_BODIES.search(t))


def not_pii(value: str, etype: str) -> bool:
    """True when a found value of this type must not be masked (general categories)."""
    if etype in ('ФИО', 'FIO'):
        return is_role_or_position(value) or is_heading(value) or ordinary_words(value)
    if etype in ('ЮЛ', 'YUL'):
        # no heading rule here: «СЕВЕРНЫЙ ТОРГОВЫЙ БАНК» in quotes is a name in capitals
        return is_role_or_position(value) or is_public_body(value)
    return False
