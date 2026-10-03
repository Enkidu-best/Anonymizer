"""«Someone else's» text with the same placeholders (task 3, §2.5): what an external LLM
returns — a summary, a letter, a risk table, questions — built from an anonymized
document of tests/fixtures/generator.py, with the expected restored text.

Tokens stand in new sentences in different case positions («направить [FIO_1]», «у FIO_1»,
«[FIO_1] подписал»), in a table, a list, a heading; some are damaged (`[FIO 1]`, `FIO_1`,
`**[YUL_2]**`); companies with and without legal form and quotes; font sizes differ.
"""
from pathlib import Path

from docx import Document
from docx.shared import Pt

# truth for the generator's fictional people and company
T = {'nomn': 'Тарханов Глеб Игоревич', 'gent': 'Тарханова Глеба Игоревича', 'datv': 'Тарханову Глебу Игоревичу',
     'accs': 'Тарханова Глеба Игоревича', 'ablt': 'Тархановым Глебом Игоревичем', 'loct': 'Тарханове Глебе Игоревиче'}
R = {'nomn': 'Ракитина Валентина Олеговна', 'gent': 'Ракитиной Валентины Олеговны', 'datv': 'Ракитиной Валентине Олеговне',
     'accs': 'Ракитину Валентину Олеговну', 'ablt': 'Ракитиной Валентиной Олеговной', 'loct': 'Ракитиной Валентине Олеговне'}
ORG = 'Северная Мануфактура'
ORG_FULL = 'ООО «Северная Мануфактура»'


def find_tokens(mappings) -> dict:
    """{'T': FIO_x, 'R': FIO_y, 'ORG': YUL_z, 'INN': INN_k, 'ADR': ADR_n} from session mappings."""
    out = {}
    for m in mappings:
        v, t = m['original_form'], m['token']
        if m['entity_type'] == 'ФИО' and 'Тарханов' in v:
            out.setdefault('T', t)
        elif m['entity_type'] == 'ФИО' and 'Ракитин' in v:
            out.setdefault('R', t)
        elif m['entity_type'] == 'ЮЛ' and 'Северная' in v:
            out.setdefault('ORG', t)
        elif m['entity_type'] == 'ИНН' and len(v) == 10:
            out.setdefault('INN', t)
            out.setdefault('INN_VALUE', v)
        elif m['entity_type'] == 'АДРЕС' and 'Озёрная' in v:
            out.setdefault('ADR', t)
            out.setdefault('ADR_VALUE', v)
    return out


def blocks(k: dict):
    """[(kind, size_pt, anonymized text, expected restored text, highlighted names)]."""
    t, r, o = k['T'], k['R'], k['ORG']
    t_bare, t_space = t, t.replace('_', ' ')
    inn, adr = k['INN_VALUE'], k['ADR_VALUE']
    return [
        ('heading', 16, f'Резюме по договору с [{o}]', f'Резюме по договору с {ORG_FULL}', []),
        ('para', 11, f'[{t}] обязуется вносить плату; уведомление направить [{t}] по адресу [{k["ADR"]}].',
         f'{T["nomn"]} обязуется вносить плату; уведомление направить {T["datv"]} по адресу {adr}.',
         [T['nomn'], T['datv']]),
        ('para', 12, f'Просим [{r}] подтвердить получение. Копия хранится у {r}.',
         f'Просим {R["accs"]} подтвердить получение. Копия хранится у {R["gent"]}.', [R['accs'], R['gent']]),
        ('para', 8, f'Мелким шрифтом: договор заключён с [{t_space}], ИНН [{k["INN"]}] и ООО «[{o}]».',
         f'Мелким шрифтом: договор заключён с {T["ablt"]}, ИНН {inn} и {ORG_FULL}.', [T['ablt']]),
        ('list', 10, f'Подписант со стороны [{o}]: [{r}]',
         f'Подписант со стороны {ORG_FULL}: {R["nomn"]}', [R['nomn']]),
        ('list', 10, f'Претензия к **[{o}]** не предъявлялась; компания [{o}] ответила.',
         f'Претензия к **{ORG_FULL}** не предъявлялась; компания «{ORG}» ответила.', []),
        ('question', 11, f'Что известно о {t_bare}?', f'Что известно о {T["loct"]}?', [T['loct']]),
        ('table', 9, f'Ответственный: [{t}]', f'Ответственный: {T["nomn"]}', [T['nomn']]),
        ('table', 9, f'Претензия к [{o}]', f'Претензия к {ORG_FULL}', []),
    ]


def make(folder: Path, mappings) -> dict:
    """Writes reply.docx and reply.txt; returns {'docx', 'txt', 'blocks'}."""
    k = find_tokens(mappings)
    bl = blocks(k)
    d = Document()
    table = None
    for kind, size, text, _, _ in bl:
        if kind == 'heading':
            p = d.add_heading(level=1)
        elif kind == 'table':
            if table is None:
                table = d.add_table(rows=0, cols=2)
            row = table.add_row()
            row.cells[0].paragraphs[0].add_run('Риск').font.size = Pt(size)
            p = row.cells[1].paragraphs[0]
        elif kind == 'list':
            p = d.add_paragraph(style='List Bullet')
        else:
            p = d.add_paragraph()
        run = p.add_run(text)
        run.font.size = Pt(size)
    docx = folder / 'reply.docx'
    d.save(docx)
    txt = folder / 'reply.txt'
    txt.write_text('\n'.join(text for _, _, text, _, _ in bl), encoding='utf-8')
    return {'docx': docx, 'txt': txt, 'blocks': bl, 'tokens': k}
