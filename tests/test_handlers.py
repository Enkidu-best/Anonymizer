"""
Real documents of the owner (local folder, git-ignored; see conftest.SAMPLES_DIR).

For every supported file: anonymization succeeds, the leak check of the output is
clean, and deanonymization restores the document text. File names are taken from
the folder at run time and never stored in the repository.
"""
import re

import pytest

from core.extract import extract_text
from core.handlers import process_uploaded_file, ALLOWED_EXTENSIONS, IMAGE_EXT, CONVERT_EXT
from core import ocr
from tests.conftest import sample_files

FILES = [f for f in sample_files(ALLOWED_EXTENSIONS)
         if ocr.available() or f.suffix.lower() not in IMAGE_EXT]


def _norm(s):
    return re.sub(r'\s+', ' ', s.replace('\xad', '-').replace('\xa0', ' ')).strip()


@pytest.mark.skipif(not FILES, reason='no local samples')
@pytest.mark.parametrize('src', FILES, ids=lambda f: f'{f.suffix}-{abs(hash(f.name)) % 10**6}')
def test_real_document(src, tmp_path, tmp_db, session_id):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    r = process_uploaded_file(src, tmp_path / 'a', session_id, tmp_db, 'anonymize', use_spacy=False)
    anon = tmp_path / 'a' / r['output_filename']
    assert anon.exists() and anon.stat().st_size > 0
    assert r['leaks'] == [], r['leaks'][:10]
    ext = src.suffix.lower()
    if ext in IMAGE_EXT or ext == '.pdf' or ext in CONVERT_EXT:
        return  # layout formats: covered by scripts/audit_folder.py
    r2 = process_uploaded_file(anon, tmp_path / 'b', session_id, tmp_db, 'deanonymize')
    back = extract_text(tmp_path / 'b' / r2['output_filename'], meta=False)
    assert _norm(back) == _norm(extract_text(src, meta=False))
