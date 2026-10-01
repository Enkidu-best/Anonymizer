"""Full-text extraction of any supported file, including hidden places and metadata.

Used for the leak check of output files and by scripts/audit_folder.py.
"""
from pathlib import Path


def extract_text(path, meta: bool = True) -> str:
    path = Path(path)
    ext = path.suffix.lower()
    if ext in ('.docx', '.pptx'):
        from core.ooxml import Package
        pkg = Package(path)
        pkg.accept_revisions()
        if not meta:
            return '\n'.join(seg.text for seg in pkg.segments())
        return pkg.text()
    if ext == '.xlsx':
        from openpyxl import load_workbook
        wb = load_workbook(str(path))
        parts = []
        for ws in wb.worksheets:
            parts.append(ws.title)
            for row in ws.iter_rows():
                for c in row:
                    if c.value is not None:
                        parts.append(str(c.value))
                    if c.comment:
                        parts.append(c.comment.text)
                        if meta:
                            parts.append(c.comment.author or '')
            for hf in (ws.oddHeader, ws.oddFooter, ws.evenHeader, ws.evenFooter,
                       ws.firstHeader, ws.firstFooter):
                for part in (hf.left, hf.center, hf.right):
                    if part.text:
                        parts.append(part.text)
        if meta:
            p = wb.properties
            parts += [p.creator or '', p.lastModifiedBy or '', p.title or '', p.subject or '']
        return '\n'.join(parts)
    if ext == '.pdf':
        import pymupdf
        doc = pymupdf.open(str(path))
        parts = [page.get_text() for page in doc]
        parts += [v for v in (doc.metadata or {}).values() if v]
        parts += [t[1] for t in doc.get_toc()]
        for page in doc:
            for a in page.annots() or []:
                parts.append((a.info or {}).get('content', ''))
                parts.append((a.info or {}).get('title', ''))
        return '\n'.join(parts)
    if ext == '.rtf':
        from core import rtf
        raw = path.read_bytes()
        text, _ = rtf.decode(raw, fields=meta)   # preview (meta=False): visible text only
        import re
        info = re.findall(rb'\{\\(?:author|operator|company|manager|title)\s*([^{}]*)\}', raw)
        if not meta:
            return text
        return text + '\n' + '\n'.join(i.decode('latin-1') for i in info)
    if ext in ('.txt', '.md', '.csv'):
        return path.read_text(encoding='utf-8', errors='replace')
    raise ValueError(f'Unsupported: {ext}')
