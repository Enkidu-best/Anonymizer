"""RTF engine: decode visible text with a map back to the raw file.

Cyrillic in RTF is escaped (\\'e8 in the document code page or \\uNNNN?), so
plain string replacement on the raw file never finds it. Here every visible
character remembers its raw span; a replacement deletes the raw spans of the
replaced characters and writes the (ASCII-escaped) value at the first one.
Control words and braces are never touched, so the file structure stays valid.
"""
import re
from typing import Callable, List, Tuple

_SKIP_DEST = {
    'fonttbl', 'colortbl', 'stylesheet', 'listtable', 'listoverridetable', 'rsidtbl',
    'generator', 'xmlnstbl', 'mmathPr', 'themedata', 'colorschememapping', 'datastore',
    'latentstyles', 'pict', 'object', 'objdata', 'filetbl', 'revtbl',
    'pgdsctbl', 'wgrffmtfilter', 'xmlopen', 'listpicture', 'blipuid', 'bkmkstart',
    'bkmkend', 'nonshppict', 'fchars', 'lchars',
}
_INFO_FIELDS = ('author', 'operator', 'company', 'manager', 'title', 'subject',
                'keywords', 'doccomm', 'hlinkbase', 'category')

_TOKEN = re.compile(rb"\\'([0-9a-fA-F]{2})|\\([a-zA-Z]+)(-?\d+)? ?|\\(.)|([{}])|(\r\n|\r|\n)|(.)",
                    re.DOTALL)


def _codepage(raw: bytes) -> str:
    m = re.search(rb'\\ansicpg(\d+)', raw[:2000])
    return f'cp{m.group(1).decode()}' if m else 'cp1252'


def decode(raw: bytes, fields: bool = True) -> Tuple[str, List[Tuple[int, int]]]:
    """Text and, per character, its (start, end) span in raw bytes.

    fields=True also reads field codes (HYPERLINK "mailto:…") — they are searched for
    personal data; fields=False gives only the visible text (preview, comparisons)."""
    enc = _codepage(raw)
    text, spans = [], []
    stack = []          # (skip, uc)
    skip, uc = False, 1
    pending_skip = 0    # fallback characters to skip after \\uN
    star = False        # \\* seen: skip the destination unless it carries text we need
    for m in _TOKEN.finditer(raw):
        hexb, word, num, sym, brace, nl, ch = m.groups()
        s, e = m.span()
        if brace == b'{':
            stack.append((skip, uc))
            continue
        if brace == b'}':
            skip, uc = stack.pop() if stack else (False, 1)
            pending_skip = 0
            continue
        if nl:
            continue
        if pending_skip and (hexb or ch):
            pending_skip -= 1
            if spans and not skip:
                spans[-1] = (spans[-1][0], e)   # fallback belongs to the \\u char
            continue
        if word is not None:
            w = word.decode()
            if star:
                star = False
                if not fields or w not in ('fldinst',):   # field code may hold mailto:/URLs
                    skip = True
                    continue
            if w in _SKIP_DEST or w == 'info':
                skip = True
            elif w == 'uc':
                uc = int(num or 1)
            elif w == 'u':
                code = int(num or 0)
                if code < 0:
                    code += 65536
                if not skip:
                    text.append(chr(code))
                    spans.append((s, e))
                pending_skip = uc
            elif not skip and w in ('par', 'line', 'row', 'sect', 'page'):
                text.append('\n')
                spans.append((s, s))      # zero-width: never deleted
            elif not skip and w in ('tab', 'cell'):
                text.append('\t')
                spans.append((s, s))
            elif not skip and w in ('emdash', 'endash'):
                text.append('—' if w == 'emdash' else '–')
                spans.append((s, e))
            elif not skip and w in ('lquote', 'rquote', 'ldblquote', 'rdblquote'):
                text.append({'lquote': '‘', 'rquote': '’', 'ldblquote': '“', 'rdblquote': '”'}[w])
                spans.append((s, e))
            continue
        if sym is not None:
            c = sym.decode('latin-1')
            if c == '*':
                star = True
            elif not skip and c == '~':
                text.append('\u00a0')
                spans.append((s, e))
            elif not skip and c in '\\{}':
                text.append(c)
                spans.append((s, e))
            elif not skip and c == '_':
                text.append('-')
                spans.append((s, e))
            continue
        if skip:
            continue
        if hexb:
            text.append(bytes([int(hexb, 16)]).decode(enc, errors='replace'))
            spans.append((s, e))
        elif ch:
            text.append(ch.decode(enc, errors='replace'))
            spans.append((s, e))
    return ''.join(text), spans


def encode_text(value: str) -> bytes:
    """RTF-escape a unicode string (ASCII stays as is, others as \\uN?)."""
    out = []
    for c in value:
        o = ord(c)
        if c in '\\{}':
            out.append('\\' + c)
        elif o < 128:
            out.append(c)
        else:
            out.append(f'\\u{o if o < 32768 else o - 65536}?')
    enc = ''.join(out)
    # own group with \\uc1: the document may run with \\uc0 (no fallback char after \\uN)
    return ('{\\uc1 ' + enc + '}').encode('ascii') if '\\u' in enc else enc.encode('ascii')


def apply(raw: bytes, find_spans: Callable[[str], List[Tuple[int, int, str]]]) -> Tuple[bytes, int]:
    text, spans = decode(raw)
    edits = []  # (raw_start, raw_end, replacement bytes)
    count = 0
    for s, e, value in find_spans(text):
        chars = [spans[i] for i in range(s, e) if spans[i][1] > spans[i][0]]
        if not chars:
            continue
        count += 1
        edits.append((chars[0][0], chars[0][1], encode_text(value)))
        edits.extend((a, b, b'') for a, b in chars[1:])
    out, last = [], 0
    for a, b, rep in sorted(edits):
        if a < last:
            continue
        out.append(raw[last:a])
        out.append(rep)
        last = b
    out.append(raw[last:])
    return b''.join(out), count


def scrub_info(raw: bytes) -> bytes:
    """Empty author/company/title… fields of the {\\info} group."""
    pat = rb'(\{\\(?:' + b'|'.join(f.encode() for f in _INFO_FIELDS) + rb')\b)[^{}]*(\})'
    return re.sub(pat, rb'\1 \2', raw)
