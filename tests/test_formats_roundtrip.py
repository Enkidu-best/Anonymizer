"""File-level round trip: anonymize → nothing leaks → deanonymize → identical text."""
from docx import Document
from openpyxl import Workbook, load_workbook

from core.handlers import process_uploaded_file
from tests.test_holdout_contract import CONTRACT, MUST_MASK

INN = '1869879737'


def _run(src, tmp_path, tmp_db, session_id, mode):
    out_dir = tmp_path / mode
    out_dir.mkdir(exist_ok=True)
    r = process_uploaded_file(src, out_dir, session_id, tmp_db, mode, use_spacy=False)
    return out_dir / r['output_filename']


def test_xlsx_numeric_inn_is_masked_and_restored(tmp_path, tmp_db, session_id):
    src = tmp_path / 'in.xlsx'
    wb = Workbook()
    ws = wb.active
    ws.append(['ИНН', int(INN)])
    ws.append(['Контакт', 'Белозёров Аркадий Львович'])
    wb.save(src)

    anon = _run(src, tmp_path, tmp_db, session_id, 'anonymize')
    vals = [c.value for row in load_workbook(anon).active.iter_rows() for c in row]
    assert int(INN) not in vals and INN not in map(str, vals)
    assert 'Белозёров Аркадий Львович' not in vals

    back = _run(anon, tmp_path, tmp_db, session_id, 'deanonymize')
    vals = [c.value for row in load_workbook(back).active.iter_rows() for c in row]
    assert vals == ['ИНН', int(INN), 'Контакт', 'Белозёров Аркадий Львович']


def test_docx_contract_round_trip(tmp_path, tmp_db, session_id):
    src = tmp_path / 'contract.docx'
    doc = Document()
    for line in CONTRACT.split('\n'):
        doc.add_paragraph(line)
    doc.save(src)

    anon = _run(src, tmp_path, tmp_db, session_id, 'anonymize')
    text = '\n'.join(p.text for p in Document(anon).paragraphs)
    assert not [m for m in MUST_MASK if m in text]

    back = _run(anon, tmp_path, tmp_db, session_id, 'deanonymize')
    assert '\n'.join(p.text for p in Document(back).paragraphs) == CONTRACT
