"""Generated documents with fictional data for the end-to-end test (task 2, §0.2).

Hard cases on purpose: every value split into 2-4 runs at random places, a soft hyphen
(U+00AD) inside a company name, a requisites table, header/footer, OCR-like noise
(«No..», spaces inside numbers). All identifiers are fictional with valid checksums.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bench'))
import ids  # noqa: E402

ids.R.seed(3301)
INN_PERSON, INN_ORG, OGRN, KPP = ids.inn12(), ids.inn10(), ids.ogrn(), ids.kpp()
BIK = ids.bik()
RS, KS = ids.rs(BIK), ids.ks(BIK)
SNILS = ids.snils()

PARAGRAPHS = [
    'ДОГОВОР АРЕНДЫ НЕЖИЛОГО ПОМЕЩЕНИЯ № 14/2026',
    'г. Тверь «02» февраля 2026 г.',
    f'Общество с ограниченной ответственностью «Северная Мануфактура» (ОГРН {OGRN}, ИНН {INN_ORG}, '
    f'КПП {KPP}), именуемое в дальнейшем «Арендодатель», в лице генерального директора '
    'Ракитиной Валентины Олеговны, действующей на основании Устава, с одной стороны, и',
    f'гражданин Российской Федерации Тарханов Глеб Игоревич, 12.05.1984 г.р., паспорт серии 28 15 '
    f'№ 604117, выдан УМВД России по Тверской области 03.06.2015, код подразделения 690-002, '
    f'СНИЛС {SNILS}, ИНН {INN_PERSON}, зарегистрированный по адресу: 170100, г. Тверь, ул. Озёрная, '
    'д. 17, кв. 41, именуемый в дальнейшем «Арендатор», с другой стороны,',
    '1. Арендодатель передаёт Арендатору помещение, кадастровый номер 69:40:0100217:552, '
    'право собственности на которое подтверждается Свидетельством о государственной регистрации '
    'права 69АБ №731905, запись регистрации № 69-01/02-031/2009-1174.',
    '2. Арендная плата вносится не позднее 20 декабря 2026 г. на счёт Арендодателя.',
    'Тарханову Г.И. направляется уведомление; Ракитина В.О. подтверждает получение.',
    'ЦЕНА И ПОРЯДОК РАСЧЕТОВ',
    'Стороной, нарушившей обязательства, возмещается ущерб. Генеральный директор вправе продлить срок.',
    'Тел.: +7 (4822) 55-17-93, e-mail: g.tarkhanov@example.org. ОГРН 10 277393 53685 · КПП No..772201001',
]
REQUISITES = [
    ('Арендодатель', 'ООО «Северная\xadМануфактура»'),
    ('ИНН / КПП', f'{INN_ORG} / {KPP}'),
    ('Р/с', f'{RS[:5]} {RS[5:8]} {RS[8]} {RS[9:13]} {RS[13:]}'),
    ('К/с', KS),
    ('БИК', BIK),
    ('Арендатор', 'Тарханов Глеб Игоревич'),
]
SIGNATURE = '____________ / В.О. Ракитина /           ____________ / Г.И. Тарханов /'

# Ground truth: nothing of this may remain in any output file (body, metadata, file name)
MUST_MASK = [
    'Северная Мануфактура', 'СевернаяМануфактура', 'Ракитиной', 'Ракитина', 'Тарханов', 'Тарханову', 'Глеб Игоревич',
    OGRN, INN_ORG, INN_PERSON, KPP, BIK, KS, RS, f'{RS[:5]} {RS[5:8]} {RS[8]} {RS[9:13]} {RS[13:]}',
    SNILS, '604117', '690-002', '12.05.1984', 'Озёрная, д. 17', '69:40:0100217:552', '731905',
    '69-01/02-031/2009-1174', '55-17-93', 'g.tarkhanov@example.org', '10 277393 53685', '772201001',
]
# Must survive: roles, positions, headings, dates of terms and of the contract
MUST_KEEP = ['Арендодатель', 'Арендатор', 'Арендатору', 'Стороной', 'Генеральный директор',
             'ЦЕНА И ПОРЯДОК РАСЧЕТОВ', '20 декабря 2026', 'февраля 2026']


def _split_runs(paragraph, text, rnd):
    """Write text as several runs: cuts at random places, alternating formatting so that
    Word would never merge them back — values end up split between runs."""
    pos = 0
    while pos < len(text):
        step = rnd.randint(3, 17)
        run = paragraph.add_run(text[pos:pos + step])
        run.bold = rnd.random() < 0.3
        run.italic = rnd.random() < 0.2
        pos += step


def make_docx(path: Path, seed=1):
    from docx import Document
    rnd = random.Random(seed)
    d = Document()
    d.sections[0].header.paragraphs[0].text = 'ООО «Северная Мануфактура» — договор аренды'
    d.sections[0].footer.paragraphs[0].text = 'Арендатор: Тарханов Г.И.'
    for t in PARAGRAPHS:
        _split_runs(d.add_paragraph(), t, rnd)
    table = d.add_table(rows=0, cols=2)
    for k, v in REQUISITES:
        row = table.add_row().cells
        row[0].text = k
        _split_runs(row[1].paragraphs[0], v, rnd)
    _split_runs(d.add_paragraph(), SIGNATURE, rnd)
    d.core_properties.author = 'Ракитина В.О.'
    d.core_properties.title = 'Договор с Тархановым Г.И.'
    d.save(path)
    return path


def make_xlsx(path: Path):
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    wb = Workbook()
    ws = wb.active
    ws.title = 'Реквизиты'
    ws.append(['Контрагент', 'ИНН', 'Сумма'])
    ws.append(['ООО «Северная Мануфактура»', int(INN_ORG), 3060000000])
    ws.append(['Тарханов Глеб Игоревич', int(INN_PERSON), 1500000])
    ws['A3'].comment = Comment('Проверить паспорт 28 15 № 604117', 'Ракитина В.О.')
    wb.save(path)
    return path


def make_rtf(path: Path):
    def u(s):   # \uc0 + \uN: no fallback characters
        return ''.join(f'\\u{ord(c)} ' if ord(c) > 127 else c for c in s)
    body = '\\par '.join(u(p) for p in PARAGRAPHS[2:5])
    path.write_bytes(('{\\rtf1\\ansi\\ansicpg1251\\uc0 {\\info{\\author Ракитина}}' + body + '\\par}')
                     .encode('latin-1', errors='ignore'))
    return path


def make_pdf(path: Path):
    import pymupdf
    from core.handlers import _find_cyrillic_font
    doc = pymupdf.open()
    page = doc.new_page()
    font = _find_cyrillic_font()
    page.insert_font(fontname='F', fontfile=font)
    y = 60
    for t in PARAGRAPHS[2:7]:
        rect = pymupdf.Rect(50, y, 545, y + 120)
        page.insert_textbox(rect, t, fontname='F', fontsize=10)
        y += 125
    doc.set_metadata({'author': 'Ракитина В.О.', 'title': 'Договор аренды'})
    doc.save(path)
    return path


def make_txt(path: Path):
    path.write_text('\n'.join(PARAGRAPHS + [SIGNATURE]), encoding='utf-8')
    return path


def make_all(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    return [make_docx(folder / 'dogovor_arendy.docx'), make_xlsx(folder / 'rekvizity.xlsx'),
            make_rtf(folder / 'pismo.rtf'), make_pdf(folder / 'dogovor.pdf'), make_txt(folder / 'zapiska.txt')]
