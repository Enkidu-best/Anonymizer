"""
File format handlers.

Each document is read as a whole (all textual places incl. headers, footnotes,
text boxes, comments, metadata), the detection pipeline runs ONCE on the full
text to build {original: [TOKEN]}, then replacements are applied place by place
by character position, keeping formatting.

Formats: TXT, DOCX, PPTX, XLSX, PDF (text layer or scan via OCR), RTF,
DOC/ODT (converted to DOCX), images JPG/PNG/HEIC/TIFF (OCR, masked boxes).
"""
import re
import shutil
import unicodedata
import subprocess
import sys
import tempfile
from pathlib import Path

OFFICE_EXT = {'.docx', '.docm', '.pptx'}
SHEET_EXT = {'.xlsx', '.xlsm'}
CONVERT_EXT = {'.doc', '.odt'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.bmp', '.webp'}
ALLOWED_EXTENSIONS = {'.txt', '.pdf', '.rtf'} | OFFICE_EXT | SHEET_EXT | CONVERT_EXT | IMAGE_EXT


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_file(filepath: Path):
    from core import ocr
    ext = filepath.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, (f'Формат «{ext}» не поддерживается. '
                       f'Поддерживаются: {", ".join(sorted(ALLOWED_EXTENSIONS))}')
    if ext in IMAGE_EXT and not ocr.available():
        return False, 'Распознавание изображений доступно только на macOS.'
    if ext == '.pdf' and not _pdf_has_text_layer(filepath) and not ocr.available():
        return False, ('PDF не содержит текстового слоя (скан). '
                       'Распознавание сканов доступно только на macOS.')
    if ext in CONVERT_EXT and not _converter():
        return False, f'Для «{ext}» нужен macOS (textutil) или LibreOffice. Сохраните файл как DOCX.'
    return True, ''


def _pdf_has_text_layer(filepath: Path) -> bool:
    try:
        import pymupdf
        doc = pymupdf.open(str(filepath))
        return any(page.get_text().strip() for page in doc)
    except Exception:
        return False


def _converter():
    if shutil.which('textutil'):
        return 'textutil'
    for name in ('soffice', 'libreoffice'):
        if shutil.which(name):
            return name
    return None


def _convert_to_docx(src: Path, out_dir: Path) -> Path:
    tool = _converter()
    dst = out_dir / (src.stem + '.docx')
    if tool == 'textutil':
        subprocess.run(['textutil', '-convert', 'docx', '-output', str(dst), str(src)],
                       check=True, capture_output=True, timeout=120)
    else:
        subprocess.run([tool, '--headless', '--convert-to', 'docx', '--outdir', str(out_dir), str(src)],
                       check=True, capture_output=True, timeout=180)
    return dst


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

_SUFFIXES = ('_anon', '_restored')
_PREFIXES = ('anonymized_', 'deanonymized_', 'anon_', 'deanon_')


def _clean_stem(stem: str) -> str:
    for pfx in _PREFIXES:
        if stem.lower().startswith(pfx):
            stem = stem[len(pfx):]
    for sfx in _SUFFIXES:
        if stem.endswith(sfx):
            stem = stem[:-len(sfx)]
    return stem


def process_uploaded_file(input_path: Path, output_dir: Path,
                          session_id: str, db_path, mode: str,
                          use_spacy: bool = True,
                          use_llm: bool = False) -> dict:
    valid, err = validate_file(input_path)
    if not valid:
        raise ValueError(err)

    ext = input_path.suffix.lower()
    # macOS stores names decomposed (й = и + ◌̆) — compose before matching
    stem = _clean_stem(unicodedata.normalize('NFC', input_path.stem))
    work = None
    src = input_path
    if ext in CONVERT_EXT:
        work = Path(tempfile.mkdtemp())
        src = _convert_to_docx(input_path, work)
        ext = '.docx'
    out_ext = '.png' if ext in ('.heic', '.bmp', '.webp') else ext

    try:
        from core.db import save_occurrences, get_occurrences
        tmp_out = output_dir / f'.tmp_{session_id[:8]}{out_ext}'
        if mode == 'anonymize':
            _LOG.clear()
            result = _anonymize(src, tmp_out, ext, session_id, db_path, use_spacy, use_llm)
            occ = list(_LOG)
            name = f'{anonymize_filename(stem, session_id, db_path)}_anon{out_ext}'
            final = output_dir / _safe_name(name)
            tmp_out.replace(final)
            save_occurrences(db_path, session_id, final.name, occ)
            result['leaks'] = leak_check(final, session_id, db_path)
        else:
            from core.anonymizer import restore_text
            _OCC.clear()
            _OCC.update(get_occurrences(db_path, session_id, safe_upload_name(input_path.name)))
            result = _deanonymize(src, tmp_out, ext, session_id, db_path)
            name = f'{restore_text(stem, db_path, session_id)}_restored{out_ext}'
            final = output_dir / _safe_name(name)
            tmp_out.replace(final)
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)

    result['output_filename'] = final.name
    return result


def _safe_name(name: str) -> str:
    """File name without path separators or control characters (Cyrillic kept)."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', name).strip(' .')
    return name[:180] or 'document'


def safe_upload_name(name: str) -> str:
    return _safe_name(Path(name.replace('\\', '/')).name)


def anonymize_filename(stem: str, session_id: str, db_path) -> str:
    """Mask PII in a file name — otherwise «Договор_Иванов.docx» leaks the name.

    Rule detectors run on the name; then every person/company already masked in
    the document is searched in the name by dictionary forms of its words
    («Пастухова Олега» ↔ «Пастухов»): the whole name, or a surname / a rare word
    of a company name on its own."""
    from core.anonymizer import anonymize_text_pipeline, morph_normal, ANY_TOKEN_RE
    from core.db import get_session_mappings, get_or_create_token
    from core.detectors import _unknown
    text = stem.replace('_', ' ')
    blocked = []
    words = [m for m in re.finditer(r'[A-Za-zА-ЯЁа-яё][A-Za-zА-ЯЁа-яё\-]*', text)
             if not any(a <= m.start() < b for a, b in blocked)]
    lem = [morph_normal(m.group()) for m in words]
    spans = []
    for mp in get_session_mappings(db_path, session_id):
        if mp['entity_type'] not in ('ФИО', 'ЮЛ'):
            continue
        orig_words = re.findall(r'[A-Za-zА-ЯЁа-яё][A-Za-zА-ЯЁа-яё\-]*', mp['original_form'])
        if not orig_words:
            continue
        seq = [morph_normal(w) for w in orig_words]
        n = len(seq)
        for i in range(len(words) - n + 1):
            if lem[i:i + n] == seq:
                spans.append((words[i].start(), words[i + n - 1].end(), mp['entity_type']))
        singles = set()
        if mp['entity_type'] == 'ФИО' and len(orig_words[0]) > 2:
            singles.add(seq[0])                                   # surname alone
        if mp['entity_type'] == 'ЮЛ':
            singles |= {l for w, l in zip(orig_words, seq) if len(w) > 2 and _unknown(w)}
        for m, l in zip(words, lem):
            if l in singles and m.group()[0].isupper():
                spans.append((m.start(), m.end(), mp['entity_type']))
    out, last = [], 0
    for a, b, etype in sorted(spans, key=lambda x: (x[0], -x[1])):
        if a < last:
            continue
        frag = text[a:b]
        out.append(text[last:a])
        out.append(f'[{get_or_create_token(db_path, session_id, frag, frag, etype)}]')
        last = b
    out.append(text[last:])
    text, _ = anonymize_text_pipeline(''.join(out), db_path, session_id, use_spacy=False, use_llm=False)
    return text


def _detect(text, session_id, db_path, use_spacy, use_llm):
    from core.anonymizer import anonymize_text_pipeline
    _, reps = anonymize_text_pipeline(text, db_path, session_id,
                                      use_spacy=use_spacy, use_llm=use_llm)
    return reps


# Per-call state: forms recorded while anonymizing / forms to restore while deanonymizing.
# Requests are processed one file at a time (Flask dev server, single worker).
_LOG: list = []
_OCC: dict = {}


def _finder(reps):
    from core.anonymizer import make_finder
    return make_finder(reps, _LOG)


def _rev_finder(session_id, db_path):
    from core.anonymizer import make_rev_finder
    return make_rev_finder(db_path, session_id, dict(_OCC))


def _anonymize(src, out, ext, session_id, db_path, use_spacy, use_llm):
    if ext == '.txt':
        return _anon_txt(src, out, session_id, db_path, use_spacy, use_llm)
    if ext in OFFICE_EXT:
        return _anon_office(src, out, session_id, db_path, use_spacy, use_llm)
    if ext in SHEET_EXT:
        return _anon_xlsx(src, out, session_id, db_path, use_spacy, use_llm)
    if ext == '.pdf':
        return _anon_pdf(src, out, session_id, db_path, use_spacy, use_llm)
    if ext == '.rtf':
        return _anon_rtf(src, out, session_id, db_path, use_spacy, use_llm)
    if ext in IMAGE_EXT:
        return _anon_image(src, out, session_id, db_path, use_spacy, use_llm)
    raise ValueError(f'Unsupported: {ext}')


def _deanonymize(src, out, ext, session_id, db_path):
    find = _rev_finder(session_id, db_path)
    if ext == '.txt':
        from core.anonymizer import apply_spans
        text = src.read_text(encoding='utf-8', errors='replace')
        out.write_text(apply_spans(text, find(text)), encoding='utf-8')
        return {}
    if ext in OFFICE_EXT:
        from core.ooxml import Package
        pkg = Package(src)
        pkg.apply(find)
        pkg.save(out)
        return {}
    if ext in SHEET_EXT:
        return _deanon_xlsx(src, out, find)
    if ext == '.pdf':
        return _deanon_pdf(src, out, find)
    if ext == '.rtf':
        from core import rtf
        raw, _ = rtf.apply(src.read_bytes(), find)
        out.write_bytes(raw)
        return {}
    if ext in IMAGE_EXT:
        shutil.copy2(src, out)
        return {'note': 'Изображение восстановить нельзя: закрашенные области не обратимы.'}
    raise ValueError(f'Unsupported: {ext}')


# ─────────────────────────────────────────────────────────────────────────────
# Leak check (CLAUDE_CODE_TASK.md §3.4)
# ─────────────────────────────────────────────────────────────────────────────

def leak_check(path: Path, session_id: str, db_path) -> list:
    """Re-read the output file (text + metadata) and report suspicious places:
    original values still present, or detector hits outside tokens."""
    from core.extract import extract_text
    from core.anonymizer import contains_bounded, ANY_TOKEN_RE
    from core.detectors import find_all
    from core.db import get_session_mappings
    try:
        text = extract_text(path)
    except Exception:
        return []
    leaks = []
    for m in get_session_mappings(db_path, session_id):
        v = m['original_form']
        if len(v) > 3 and contains_bounded(text, v):
            leaks.append({'type': m['entity_type'], 'value': v, 'reason': 'исходное значение'})
    seen = {l['value'] for l in leaks}
    for h in find_all(text):
        frag = text[h.start:h.end]
        if ANY_TOKEN_RE.search(frag) or frag in seen or re.fullmatch(r'[\[\]\w]*_\d+\]?', frag):
            continue
        seen.add(frag)
        leaks.append({'type': h.type, 'value': frag, 'reason': 'найдено повторной проверкой'})
    return leaks[:200]


# ─────────────────────────────────────────────────────────────────────────────
# TXT
# ─────────────────────────────────────────────────────────────────────────────

def _anon_txt(src, out, session_id, db_path, use_spacy, use_llm):
    from core.anonymizer import apply_spans
    text = src.read_text(encoding='utf-8', errors='replace')
    reps = _detect(text, session_id, db_path, use_spacy, use_llm)
    out.write_text(apply_spans(text, _finder(reps)(text)), encoding='utf-8')
    return {'entities_found': len(reps)}


# ─────────────────────────────────────────────────────────────────────────────
# DOCX / PPTX (zip + XML level, every textual place)
# ─────────────────────────────────────────────────────────────────────────────

def _anon_office(src, out, session_id, db_path, use_spacy, use_llm):
    from core.ooxml import Package
    pkg = Package(src)
    pkg.accept_revisions()
    reps = _detect(pkg.text(), session_id, db_path, use_spacy, use_llm)
    pkg.apply(_finder(reps))
    pkg.scrub_metadata()
    pkg.save(out)
    return {'entities_found': len(reps)}


def _replace_para(para, replacements: dict):
    """python-docx paragraph helper (kept for callers working with Document objects)."""
    from core.ooxml import _segments
    from core.anonymizer import replace_spans
    for seg in _segments(para._p):
        seg.apply(replace_spans(seg.text, replacements))


# ─────────────────────────────────────────────────────────────────────────────
# XLSX
# ─────────────────────────────────────────────────────────────────────────────

def _xlsx_cell_text(value):
    """Text of a cell for detection; integers (INN, phones stored as numbers) included."""
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) >= 10**5:
        return str(value)
    return None


def _xlsx_places(wb):
    """(getter, setter) for every textual place of a workbook."""
    places = []
    for ws in wb.worksheets:
        places.append((lambda ws=ws: ws.title, lambda v, ws=ws: setattr(ws, 'title', v[:31])))
        for row in ws.iter_rows():
            for c in row:
                if _xlsx_cell_text(c.value):
                    places.append((lambda c=c: _xlsx_cell_text(c.value),
                                   lambda v, c=c: setattr(c, 'value', v)))
                if c.comment:
                    places.append((lambda c=c: c.comment.text,
                                   lambda v, c=c: setattr(c.comment, 'text', v)))
        for hf in (ws.oddHeader, ws.oddFooter, ws.evenHeader, ws.evenFooter,
                   ws.firstHeader, ws.firstFooter):
            for part in (hf.left, hf.center, hf.right):
                if part.text:
                    places.append((lambda p=part: p.text, lambda v, p=part: setattr(p, 'text', v)))
    p = wb.properties
    for attr in ('title', 'subject', 'description', 'keywords'):
        if getattr(p, attr):
            places.append((lambda a=attr: getattr(p, a), lambda v, a=attr: setattr(p, a, v)))
    return places


def _anon_xlsx(src, out, session_id, db_path, use_spacy, use_llm):
    from openpyxl import load_workbook
    from core.anonymizer import apply_spans
    wb = load_workbook(str(src))
    places = _xlsx_places(wb)
    reps = _detect('\n'.join(g() for g, _ in places), session_id, db_path, use_spacy, use_llm)
    find = _finder(reps)
    for get, put in places:
        t = get()
        spans = find(t)
        if spans:
            put(apply_spans(t, spans))
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if c.comment:
                    c.comment.author = 'Автор'
    wb.properties.creator = ''
    wb.properties.lastModifiedBy = ''
    wb.save(str(out))
    return {'entities_found': len(reps)}


def _deanon_xlsx(src, out, find):
    from openpyxl import load_workbook
    from core.anonymizer import apply_spans
    wb = load_workbook(str(src))
    for get, put in _xlsx_places(wb):
        t = get()
        spans = find(t)
        if spans:
            v = apply_spans(t, spans)
            # numbers that were masked come back as numbers
            if v.isdigit() and not v.startswith('0') and len(v) < 16:
                v = int(v)
            put(v)
    wb.save(str(out))
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# PDF
# ─────────────────────────────────────────────────────────────────────────────

def _detect_avg_fontsize(page) -> float:
    sizes = []
    for block in page.get_text('dict').get('blocks', []):
        for line in block.get('lines', []):
            for span in line.get('spans', []):
                sz = span.get('size', 0)
                if 6 < sz < 30:
                    sizes.append(sz)
    if not sizes:
        return 10.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def _find_cyrillic_font():
    if sys.platform == 'win32':
        candidates = [r'C:\Windows\Fonts\arial.ttf', r'C:\Windows\Fonts\calibri.ttf',
                      r'C:\Windows\Fonts\times.ttf']
    elif sys.platform == 'darwin':
        candidates = ['/System/Library/Fonts/Supplemental/Arial.ttf', '/Library/Fonts/Arial.ttf',
                      '/System/Library/Fonts/Supplemental/Times New Roman.ttf']
    else:
        candidates = ['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                      '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf']
    return next((p for p in candidates if Path(p).exists()), None)


def _page_words(page):
    """Page text built from words + per-char word index. Lines joined by newline,
    so values broken across lines are still one match (whitespace-tolerant)."""
    words = page.get_text('words')
    text, owner = [], []
    prev = None
    for i, w in enumerate(words):
        key = (w[5], w[6])
        if prev is not None:
            # same line → space; next line of the same block (soft wrap) → space; new block → newline
            sep = ' ' if key[0] == prev[0] else '\n'
            text.append(sep)
            owner.append(None)
        text.append(w[4])
        owner.extend([i] * len(w[4]))
        prev = key
    return words, ''.join(text), owner


def _ocr_page_lines(page, dpi=200):
    from core import ocr
    pix = page.get_pixmap(dpi=dpi)
    lines = ocr.recognize(pix.tobytes('png'), (pix.width, pix.height))
    scale = page.rect.width / pix.width
    return lines, scale


def _flex(find):
    """Make matching tolerant to line breaks / repeated spaces inside values."""
    def wrapped(text):
        flat = re.sub(r'[ \t\n]+', ' ', text)
        if len(flat) == len(text):
            return find(flat)
        # map flat offsets back to text offsets
        idx, j = [], 0
        for k, ch in enumerate(text):
            if ch in ' \t\n' and k > 0 and text[k - 1] in ' \t\n':
                continue
            idx.append(k)
        idx.append(len(text))
        return [(idx[a], idx[b - 1] + 1, v) for a, b, v in find(flat)]
    return wrapped


def _token_fontsize(page, token, rect, base):
    import pymupdf
    width = pymupdf.get_text_length(token, fontname='helv', fontsize=1) or 1
    return max(min(base * 0.88, rect.width / width * 0.98, rect.height * 0.9), 3)


def text_layer_ok(text: str) -> bool:
    """False for a broken text layer: a scan whose OCR text uses a wrong font map
    («AoroBopy apeHAbl» instead of «Договору аренды») or is otherwise unreadable."""
    from core.detectors import _unknown
    words = re.findall(r'[A-Za-zА-ЯЁа-яё]{3,}', text)
    if len(words) < 15:
        return True
    camel = sum(1 for w in words if re.search(r'[a-zа-яё][A-ZА-ЯЁ]', w)) / len(words)
    if camel > 0.15:
        return False
    cyr = [w for w in words if re.search('[А-ЯЁа-яё]', w)]
    if len(cyr) >= 15 and sum(1 for w in cyr if not _unknown(w)) / len(cyr) < 0.5:
        return False
    return True


def _drop_text_layer(page, words):
    import pymupdf
    for w in words:
        page.add_redact_annot(pymupdf.Rect(w[:4]), fill=None)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)


def _anon_pdf(src, out, session_id, db_path, use_spacy, use_llm):
    import pymupdf
    doc = pymupdf.open(str(src))
    from core import ocr
    pages = []
    for page in doc:
        words, text, owner = _page_words(page)
        if text.strip() and (text_layer_ok(text) or not ocr.available()):
            pages.append(('text', words, text, owner, None))
        else:
            if text.strip():
                _drop_text_layer(page, words)   # garbage OCR layer of a scan: remove, OCR again
            lines, scale = _ocr_page_lines(page)
            ocr_text, offs = ocr.layout(lines, page.rect.width / scale)
            pages.append(('ocr', offs, ocr_text, None, scale))

    toc = doc.get_toc()
    full = '\n'.join(p[2] for p in pages) + '\n' + '\n'.join(t[1] for t in toc)
    reps = _detect(full, session_id, db_path, use_spacy, use_llm)
    find = _flex(_finder({re.sub(r'\s+', ' ', k): v for k, v in reps.items()}))

    for page, (kind, items, text, owner, scale) in zip(doc, pages):
        base = _detect_avg_fontsize(page)
        if kind == 'text':
            for s, e, token in find(text):
                idxs = sorted({owner[k] for k in range(s, e) if owner[k] is not None})
                by_line = {}
                for i in idxs:
                    w = items[i]
                    by_line.setdefault((w[5], w[6]), []).append(pymupdf.Rect(w[:4]))
                for n, rects in enumerate(by_line.values()):
                    r = rects[0]
                    for x in rects[1:]:
                        r |= x
                    label = token if n == 0 else ''
                    page.add_redact_annot(r, text=label, fontname='helv',
                                          fontsize=_token_fontsize(page, token, r, base),
                                          fill=(1, 1, 1), text_color=(0, 0, 0))
            # pixels too: a scan with an invisible text layer shows the value in the image
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
        else:
            for s, e, token in find(text):
                first = True
                for l, off in items:
                    a, b = max(s, off), min(e, off + len(l.text))
                    if a >= b:
                        continue
                    x0, y0, x1, y1 = l.box_for(a - off, b - off)
                    r = pymupdf.Rect(x0 * scale - 1, y0 * scale - 1, x1 * scale + 1, y1 * scale + 1)
                    page.add_redact_annot(r, text=token if first else '', fontname='helv',
                                          fontsize=_token_fontsize(page, token, r, base),
                                          fill=(0, 0, 0), text_color=(1, 1, 1))
                    first = False
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)

    from core.anonymizer import apply_spans
    if toc:
        doc.set_toc([[lvl, apply_spans(title, find(title)), pg] for lvl, title, pg, *_ in toc])
    for page in doc:
        for a in list(page.annots() or []):
            info = a.info or {}
            content = info.get('content') or ''
            if content:
                a.set_info(content=apply_spans(content, find(content)), title='Автор')
                a.update()
    doc.set_metadata({})
    try:
        doc.del_xml_metadata()
    except Exception:
        pass
    doc.save(str(out), garbage=4, deflate=True, deflate_images=True)
    return {'entities_found': len(reps), 'ocr_pages': sum(p[0] == 'ocr' for p in pages)}


def _deanon_pdf(src, out, find):
    import pymupdf
    doc = pymupdf.open(str(src))
    font = _find_cyrillic_font()
    missing_font = False
    for page in doc:
        words, text, owner = _page_words(page)
        hits = find(text)
        if not hits:
            continue
        base = _detect_avg_fontsize(page)
        todo = []
        for s, e, orig in hits:
            idxs = sorted({owner[k] for k in range(s, e) if owner[k] is not None})
            if not idxs:
                continue
            r = pymupdf.Rect(words[idxs[0]][:4])
            for i in idxs[1:]:
                r |= pymupdf.Rect(words[i][:4])
            page.add_redact_annot(r, fill=(1, 1, 1))
            todo.append((r, orig))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        if not font:
            missing_font = True
            continue
        page.insert_font(fontname='CyrF', fontfile=font)
        cyr = pymupdf.Font(fontfile=font)
        for r, orig in todo:
            # ASCII values (phones, numbers) in Helvetica: Arial maps «-» to a soft hyphen on copy
            ascii_only = orig.isascii()
            fname = 'helv' if ascii_only else 'CyrF'
            width = (pymupdf.get_text_length(orig, fontname='helv', fontsize=1) if ascii_only
                     else cyr.text_length(orig, fontsize=1))
            fs = max(min(base * 0.88, r.width / max(width, 0.01)), 4)
            page.insert_text((r.x0, r.y1 - r.height * 0.2), orig, fontname=fname, fontsize=fs)
    doc.save(str(out), garbage=4, deflate=True)
    return {'_pdf_no_font': missing_font}


# ─────────────────────────────────────────────────────────────────────────────
# RTF (decoded with a map back to raw bytes)
# ─────────────────────────────────────────────────────────────────────────────

def _anon_rtf(src, out, session_id, db_path, use_spacy, use_llm):
    from core import rtf
    raw = rtf.scrub_info(src.read_bytes())
    text, _ = rtf.decode(raw)
    reps = _detect(text, session_id, db_path, use_spacy, use_llm)
    new, _ = rtf.apply(raw, _finder(reps))
    out.write_bytes(new)
    return {'entities_found': len(reps)}


# ─────────────────────────────────────────────────────────────────────────────
# Images (OCR → black boxes with the token; EXIF incl. GPS dropped)
# ─────────────────────────────────────────────────────────────────────────────

_PLATE_OCR = re.compile(r'(?<![0-9A-Za-zА-Яа-я])[АВЕКМНОРСТУХABEKMHOPCTYX][ ]?\d{3}[ ]?[АВЕКМНОРСТУХABEKMHOPCTYX]{2}(?![A-Za-zА-Яа-я])',
                        re.IGNORECASE)


def _anon_image(src, out, session_id, db_path, use_spacy, use_llm):
    import io
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    from core import ocr
    if src.suffix.lower() == '.heic':
        from pillow_heif import register_heif_opener
        register_heif_opener()
    img = ImageOps.exif_transpose(Image.open(src)).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    lines = ocr.recognize(buf.getvalue(), img.size)
    text, offs = ocr.layout(lines, img.size[0])
    reps = _detect(text, session_id, db_path, use_spacy, use_llm)
    # Photos: the small region part of a licence plate is often misread by OCR
    # («Н385ЕУ 777» → «H385EY ML») — the series alone is enough, mask the whole plate line
    from core.db import get_or_create_token
    for l in lines:
        for m in _PLATE_OCR.finditer(l.text):
            plate = l.text[m.start():].strip() if len(l.text) - m.start() <= 14 else m.group()
            reps.setdefault(plate, f"[{get_or_create_token(db_path, session_id, plate, plate, 'ГОСНОМЕР')}]")
    find = _finder(reps)
    draw = ImageDraw.Draw(img)
    boxes = 0
    for s, e, token in find(text):
        first = True
        for l, off in offs:
            a, b = max(s, off), min(e, off + len(l.text))
            if a >= b:
                continue
            x0, y0, x1, y1 = l.box_for(a - off, b - off)
            pad = max(2, (y1 - y0) * 0.15)
            draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], fill=(0, 0, 0))
            if first:
                try:
                    font = ImageFont.load_default(size=max(int((y1 - y0) * 0.8), 8))
                    draw.text((x0, y0), token, fill=(255, 255, 255), font=font)
                except Exception:
                    pass
            first = False
            boxes += 1
    fmt = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.tif': 'TIFF', '.tiff': 'TIFF'}.get(out.suffix.lower(), 'PNG')
    img.save(out, fmt, quality=92) if fmt == 'JPEG' else img.save(out, fmt)
    return {'entities_found': len(reps), 'boxes': boxes}
