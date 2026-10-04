"""A PDF with a text layer becomes Word (task 3, §4). Fictional data.

digital PDF → anonymized DOCX: values masked, a ruled table stays a table, lines of one
paragraph are one paragraph; a scan (page picture + OCR text layer) stays a PDF with black
boxes; «also save the PDF» gives both; the DOCX restores back.
"""
from docx import Document

from core.handlers import pdf_is_digital, process_uploaded_file
from tests.e2e.independent import places
from tests.fixtures import generator

NAME = 'Ракитина Валентина Олеговна'
LONG = ('Арендатор обязуется вносить арендную плату ежемесячно не позднее двадцатого числа текущего '
        'месяца на расчётный счёт Арендодателя, указанный в разделе реквизитов настоящего договора, '
        'а также возмещать коммунальные расходы по счетам снабжающих организаций.')


def _digital_pdf(path):
    import pymupdf
    from core.handlers import _find_cyrillic_font
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_font(fontname='F', fontfile=_find_cyrillic_font())
    page.insert_textbox(pymupdf.Rect(50, 50, 545, 140), f'Договор заключён с гражданкой {NAME}, ИНН {generator.INN_PERSON}.',
                        fontname='F', fontsize=11)
    page.insert_textbox(pymupdf.Rect(50, 150, 545, 260), LONG, fontname='F', fontsize=11)
    # a ruled 2×3 table
    x0, y0, w, h = 50, 300, 240, 24
    cells = [('Сторона', 'Арендодатель'), ('ИНН', generator.INN_ORG), ('Представитель', NAME)]
    for i, (a, b) in enumerate(cells):
        for j, t in enumerate((a, b)):
            r = pymupdf.Rect(x0 + j * w, y0 + i * h, x0 + (j + 1) * w, y0 + (i + 1) * h)
            page.draw_rect(r, color=(0, 0, 0), width=0.8)
            page.insert_textbox(r + (4, 5, -4, 0), t, fontname='F', fontsize=10)
    doc.save(path)
    return path


def _scan_pdf(path):
    """A page that is a picture with an invisible OCR text layer on top."""
    import pymupdf
    from core.handlers import _find_cyrillic_font
    src = pymupdf.open()
    p = src.new_page()
    p.insert_font(fontname='F', fontfile=_find_cyrillic_font())
    p.insert_textbox(pymupdf.Rect(50, 50, 545, 400), f'Договор заключён с гражданкой {NAME}. ' + LONG * 2,
                     fontname='F', fontsize=12)
    pix = p.get_pixmap(dpi=100)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pix)
    page.insert_font(fontname='F', fontfile=_find_cyrillic_font())
    page.insert_textbox(pymupdf.Rect(50, 50, 545, 400), f'Договор заключён с гражданкой {NAME}. ' + LONG * 2,
                        fontname='F', fontsize=12, render_mode=3)
    doc.save(path)
    return path


def test_digital_pdf_becomes_word(tmp_path, tmp_db, session_id):
    src = _digital_pdf(tmp_path / 'contract.pdf')
    assert pdf_is_digital(src)
    (tmp_path / 'o').mkdir()
    r = process_uploaded_file(src, tmp_path / 'o', session_id, tmp_db, 'anonymize', use_spacy=False,
                              pdf_to_word=True, keep_pdf=False)
    out = tmp_path / 'o' / r['output_filename']
    assert out.suffix == '.docx' and r['converted_from'] == 'pdf' and 'extra_output' not in r
    body = '\n'.join(places(out)['body'])
    for v in ('Ракитин', generator.INN_PERSON, generator.INN_ORG):
        assert v not in body, v
    d = Document(out)
    assert d.tables, 'the ruled table must stay a table'
    cells = [c.text.strip() for row in d.tables[0].rows for c in row.cells]
    assert 'Сторона' in cells and 'Арендодатель' in cells
    # the long paragraph (3 lines in the PDF) is one paragraph, not three
    long_paras = [p.text for p in d.paragraphs if 'ежемесячно' in p.text]
    assert len(long_paras) == 1 and 'снабжающих организаций' in long_paras[0], long_paras
    # and it restores
    (tmp_path / 'b').mkdir()
    rr = process_uploaded_file(out, tmp_path / 'b', session_id, tmp_db, 'deanonymize')
    back = '\n'.join(places(tmp_path / 'b' / rr['output_filename'])['body'])
    assert NAME in back and generator.INN_ORG in back


def test_keep_pdf_too(tmp_path, tmp_db, session_id):
    src = _digital_pdf(tmp_path / 'contract.pdf')
    (tmp_path / 'o').mkdir()
    r = process_uploaded_file(src, tmp_path / 'o', session_id, tmp_db, 'anonymize', use_spacy=False,
                              pdf_to_word=True, keep_pdf=True)
    pdf = tmp_path / 'o' / r['extra_output']
    assert pdf.suffix == '.pdf' and r['output_filename'].endswith('.docx')
    assert 'Ракитин' not in '\n'.join(places(pdf)['body'])


def test_scan_stays_pdf(tmp_path, tmp_db, session_id):
    src = _scan_pdf(tmp_path / 'scan.pdf')
    assert not pdf_is_digital(src)
    (tmp_path / 'o').mkdir()
    r = process_uploaded_file(src, tmp_path / 'o', session_id, tmp_db, 'anonymize', use_spacy=False,
                              pdf_to_word=True)
    assert r['output_filename'].endswith('.pdf') and 'converted_from' not in r


def test_option_off_keeps_pdf(tmp_path, tmp_db, session_id):
    src = _digital_pdf(tmp_path / 'contract.pdf')
    (tmp_path / 'o').mkdir()
    r = process_uploaded_file(src, tmp_path / 'o', session_id, tmp_db, 'anonymize', use_spacy=False,
                              pdf_to_word=False)
    assert r['output_filename'].endswith('.pdf')


def test_value_cut_by_paragraph_break_elsewhere(tmp_path, tmp_db, session_id):
    """Found whole in one paragraph, cut by a paragraph break in another — both masked."""
    d = Document()
    d.add_paragraph('Поручитель зарегистрирован по адресу: г. Тверь, ул. Озёрная, д. 17, кв. 41.')
    d.add_paragraph('Уведомления направляются по адресу: г. Тверь, ул. Озёрная,')
    d.add_paragraph('д. 17, кв. 41 заказным письмом.')
    src = tmp_path / 'c.docx'
    d.save(src)
    (tmp_path / 'o').mkdir()
    r = process_uploaded_file(src, tmp_path / 'o', session_id, tmp_db, 'anonymize', use_spacy=False)
    body = '\n'.join(places(tmp_path / 'o' / r['output_filename'])['body'])
    assert 'Озёрная' not in body and 'кв. 41' not in body, body
    assert r['leaks'] == []
    (tmp_path / 'b').mkdir()
    rr = process_uploaded_file(tmp_path / 'o' / r['output_filename'], tmp_path / 'b', session_id, tmp_db, 'deanonymize')
    back = [p.text for p in Document(tmp_path / 'b' / rr['output_filename']).paragraphs]
    assert back == [p.text for p in d.paragraphs]


def test_scan_restore_fills_the_black_box(tmp_path, tmp_db, session_id):
    """A restored value in a scan is written into the whole black box, not shrunk to the
    width of the small «[FIO_1]» label (owner, v3.4.0: «очень маленький шрифт»)."""
    import pymupdf
    import pytest
    from core import ocr
    if not ocr.available():
        pytest.skip('OCR is macOS only')
    src = _scan_pdf(tmp_path / 'scan.pdf')
    (tmp_path / 'o').mkdir()
    r = process_uploaded_file(src, tmp_path / 'o', session_id, tmp_db, 'anonymize', use_spacy=False)
    anon = tmp_path / 'o' / r['output_filename']
    (tmp_path / 'b').mkdir()
    rr = process_uploaded_file(anon, tmp_path / 'b', session_id, tmp_db, 'deanonymize')
    page = pymupdf.open(str(tmp_path / 'b' / rr['output_filename']))[0]
    spans = [sp for b in page.get_text('dict')['blocks'] for l in b.get('lines', []) for sp in l['spans']
             if 'Ракитин' in sp['text']]
    assert spans, 'restored name not found'
    assert min(sp['size'] for sp in spans) >= 8, [sp['size'] for sp in spans]
    black = [d for d in page.get_drawings() if d.get('fill') is not None and max(d['fill']) < 0.2]
    assert not black, f'{len(black)} black boxes left'
