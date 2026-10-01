"""Office Open XML (DOCX, PPTX) text engine working on the zip/XML level.

Every textual place of the package is visited: body, tables (nested), text
boxes, headers/footers of all kinds, footnotes, endnotes, comments, charts,
SmartArt, slides and notes, document properties, hyperlink targets.

A paragraph (w:p or a:p) is a *segment*: its own text nodes in order (text of
nested paragraphs, e.g. inside a text box, belongs to those paragraphs).
Replacements are applied by character position across text nodes, so the
formatting of runs is kept and an entity split between runs is still found.
"""
import io
import re
import zipfile
from urllib.parse import unquote
from typing import Callable, Dict, List, Tuple

from lxml import etree

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
CP = 'http://schemas.openxmlformats.org/package/2006/metadata/core-properties'
DC = 'http://purl.org/dc/elements/1.1/'
EP = 'http://schemas.openxmlformats.org/officeDocument/2006/extended-properties'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'

_P_TAGS = {f'{{{W}}}p', f'{{{A}}}p'}
_T_TAGS = {f'{{{W}}}t', f'{{{A}}}t', f'{{{W}}}instrText'}
_BREAK = {f'{{{W}}}tab': '\t', f'{{{W}}}br': '\n', f'{{{W}}}cr': '\n',
          f'{{{W}}}noBreakHyphen': '-', f'{{{A}}}br': '\n'}

# Parts that never carry document text
_SKIP_PART = re.compile(r'(styles|fontTable|settings|webSettings|theme\d*|numbering|'
                        r'stylesWithEffects|tableStyles|presProps|viewProps|'
                        r'commentsIds|people)\.xml$|^\[Content_Types\]\.xml$|customXml/')
# Author attributes (comments, revisions) — replaced by a neutral value
_AUTHOR_ATTRS = (f'{{{W}}}author', f'{{{W}}}initials')


class Segment:
    __slots__ = ('pieces',)

    def __init__(self):
        self.pieces: List[Tuple[object, str]] = []   # (text element or None, text)

    @property
    def text(self) -> str:
        return ''.join(t for _, t in self.pieces)

    def apply(self, spans: List[Tuple[int, int, str]]) -> bool:
        """Replace [s, e) with value for each non-overlapping span.

        The left-most text node of a span receives the value (and keeps its
        run formatting); the rest of the span is cut from following nodes.
        Spans are processed right to left so earlier offsets stay valid.
        """
        if not spans:
            return False
        offsets, pos = [], 0
        for _, t in self.pieces:
            offsets.append(pos)
            pos += len(t)
        changed = False
        for s, e, value in sorted(spans, key=lambda x: -x[0]):
            hit = [i for i, (node, t) in enumerate(self.pieces)
                   if node is not None and offsets[i] < e and offsets[i] + len(t) > s]
            if not hit:
                continue
            for i in reversed(hit):
                node = self.pieces[i][0]
                ls = max(s - offsets[i], 0)
                le = min(e - offsets[i], len(self.pieces[i][1]))
                cur = node.text or ''
                node.text = cur[:ls] + (value if i == hit[0] else '') + cur[le:]
                if node.text != node.text.strip():
                    node.set(XML_SPACE, 'preserve')
            changed = True
        self.pieces = [(n, (n.text or '') if n is not None else t) for n, t in self.pieces]
        return changed


def _own_paragraph(el, p):
    """True if the nearest paragraph ancestor of el is p."""
    cur = el.getparent()
    while cur is not None:
        if cur.tag in _P_TAGS:
            return cur is p
        cur = cur.getparent()
    return False


def _segments(root) -> List[Segment]:
    segs = []
    for p in root.iter(*_P_TAGS):
        seg = Segment()
        for el in p.iter():
            if el is p:
                continue
            if el.tag in _T_TAGS:
                if _own_paragraph(el, p):
                    seg.pieces.append((el, el.text or ''))
            elif el.tag in _BREAK and _own_paragraph(el, p):
                seg.pieces.append((None, _BREAK[el.tag]))
        if seg.pieces:
            segs.append(seg)
    return segs


def _accept_revisions(root):
    """Accept tracked changes: keep insertions, drop deletions and change history."""
    for tag in ('ins', 'moveTo'):
        for el in list(root.iter(f'{{{W}}}{tag}')):
            parent = el.getparent()
            if parent is None:
                continue
            # w:ins inside w:rPr only marks a paragraph mark — just drop the marker
            if parent.tag == f'{{{W}}}rPr':
                parent.remove(el)
                continue
            idx = parent.index(el)
            for child in list(el):
                parent.insert(idx, child)
                idx += 1
            parent.remove(el)
    for tag in ('del', 'moveFrom', 'rPrChange', 'pPrChange', 'sectPrChange', 'tblPrChange',
                'trPrChange', 'tcPrChange', 'tblGridChange', 'numberingChange',
                'moveFromRangeStart', 'moveFromRangeEnd', 'moveToRangeStart', 'moveToRangeEnd'):
        for el in list(root.iter(f'{{{W}}}{tag}')):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)


class Package:
    """Loaded OOXML package with parsed XML parts."""

    def __init__(self, path):
        with zipfile.ZipFile(path) as z:
            self.infos = z.infolist()
            self.raw = {i.filename: z.read(i.filename) for i in self.infos}
        self.xml: Dict[str, etree._Element] = {}
        for name, data in self.raw.items():
            if not name.endswith('.xml') or _SKIP_PART.search(name):
                continue
            if not (name.startswith(('word/', 'ppt/', 'docProps/', 'xl/charts/', 'xl/drawings/'))):
                continue
            try:
                self.xml[name] = etree.fromstring(data)
            except etree.XMLSyntaxError:
                pass
        self.rels: Dict[str, etree._Element] = {}
        for name, data in self.raw.items():
            if name.endswith('.rels'):
                try:
                    self.rels[name] = etree.fromstring(data)
                except etree.XMLSyntaxError:
                    pass

    # ── reading ──────────────────────────────────────────────────────────────
    def accept_revisions(self):
        for name, root in self.xml.items():
            if root.nsmap and W in root.nsmap.values():
                _accept_revisions(root)

    def segments(self) -> List[Segment]:
        out = []
        for name in sorted(self.xml):
            if name.startswith('docProps/'):
                continue
            out.extend(_segments(self.xml[name]))
        return out

    def property_nodes(self) -> List[etree._Element]:
        nodes = []
        for name in ('docProps/core.xml', 'docProps/app.xml', 'docProps/custom.xml'):
            root = self.xml.get(name)
            if root is None:
                continue
            for el in root.iter():
                if isinstance(el.tag, str) and el.text and el.text.strip() and len(el) == 0:
                    nodes.append(el)
        return nodes

    def external_targets(self) -> List[etree._Element]:
        out = []
        for root in self.rels.values():
            for rel in root:
                if rel.get('TargetMode') == 'External' and rel.get('Target'):
                    out.append(rel)
        return out

    def text(self) -> str:
        """All text of the package (for detection and leak checks)."""
        parts = [s.text for s in self.segments()]
        parts += [n.text for n in self.property_nodes()]
        parts += [unquote(r.get('Target')) for r in self.external_targets()]
        return '\n'.join(parts)

    # ── writing ──────────────────────────────────────────────────────────────
    def apply(self, find_spans: Callable[[str], List[Tuple[int, int, str]]]) -> int:
        """Apply replacements everywhere. find_spans(text) → [(start, end, value)]."""
        n = 0
        for seg in self.segments():
            if seg.apply(find_spans(seg.text)):
                n += 1
        for node in self.property_nodes():
            node.text = _apply_str(node.text, find_spans)
        for rel in self.external_targets():
            target = unquote(rel.get('Target'))   # mailto%3Aivan%40x.ru → mailto:ivan@x.ru
            new = _apply_str(target, find_spans)
            if new != target:
                rel.set('Target', new)
        return n

    def scrub_metadata(self, author='Автор'):
        """Remove personal metadata: creator, last editor, company, manager, authors."""
        core = self.xml.get('docProps/core.xml')
        if core is not None:
            for tag in (f'{{{DC}}}creator', f'{{{CP}}}lastModifiedBy'):
                for el in core.iter(tag):
                    el.text = ''
        app = self.xml.get('docProps/app.xml')
        if app is not None:
            for tag in (f'{{{EP}}}Company', f'{{{EP}}}Manager'):
                for el in app.iter(tag):
                    el.text = ''
        for root in self.xml.values():
            for el in root.iter():
                for attr in _AUTHOR_ATTRS:
                    if el.get(attr) is not None:
                        el.set(attr, author if attr.endswith('author') else 'А')
        # people.xml lists comment authors by name — drop the authors
        for name in [n for n in self.raw if n.endswith('people.xml')]:
            try:
                root = etree.fromstring(self.raw[name])
                for el in list(root):
                    root.remove(el)
                self.raw[name] = etree.tostring(root, xml_declaration=True, encoding='UTF-8',
                                                standalone=True)
            except etree.XMLSyntaxError:
                pass

    def save(self, path):
        for name, root in self.xml.items():
            self.raw[name] = etree.tostring(root, xml_declaration=True, encoding='UTF-8',
                                            standalone=True)
        for name, root in self.rels.items():
            self.raw[name] = etree.tostring(root, xml_declaration=True, encoding='UTF-8',
                                            standalone=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            for info in self.infos:
                z.writestr(info, self.raw[info.filename])
        with open(path, 'wb') as f:
            f.write(buf.getvalue())


def _apply_str(s: str, find_spans) -> str:
    out, last = [], 0
    for a, b, v in sorted(find_spans(s)):
        if a < last:
            continue
        out.append(s[last:a])
        out.append(v)
        last = b
    out.append(s[last:])
    return ''.join(out)
