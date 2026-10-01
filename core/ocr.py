"""Offline OCR for images and scanned PDF pages (macOS Apple Vision).

Returns recognized lines with pixel boxes; `box_for(line, start, end)` gives
the box of a substring so only the sensitive part of a line is covered.
On other platforms `available()` is False and callers reject the file.
"""
import sys
from dataclasses import dataclass
from typing import List, Tuple


def available() -> bool:
    if sys.platform != 'darwin':
        return False
    try:
        import Vision  # noqa: F401
        return True
    except Exception:
        return False


@dataclass
class Line:
    text: str
    box: Tuple[float, float, float, float]   # x0, y0, x1, y1 in pixels, origin top-left
    _cand: object = None
    _size: Tuple[int, int] = (0, 0)

    def box_for(self, start: int, end: int) -> Tuple[float, float, float, float]:
        """Pixel box of text[start:end]; falls back to the whole line."""
        try:
            from Foundation import NSMakeRange
            obs, err = self._cand.boundingBoxForRange_error_(NSMakeRange(start, end - start), None)
            if obs is not None:
                return _to_px(obs.boundingBox(), self._size)
        except Exception:
            pass
        return self.box


def _to_px(bb, size):
    w, h = size
    x0 = bb.origin.x * w
    y1 = (1 - bb.origin.y) * h
    x1 = x0 + bb.size.width * w
    y0 = y1 - bb.size.height * h
    return (x0, y0, x1, y1)


def recognize(png_bytes: bytes, size: Tuple[int, int]) -> List[Line]:
    """OCR an image given as PNG/JPEG bytes. size = (width, height) in pixels."""
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(['ru-RU', 'en-US'])
    req.setUsesLanguageCorrection_(True)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f'OCR failed: {err}')
    lines = []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        c = cands[0]
        lines.append(Line(str(c.string()), _to_px(obs.boundingBox(), size), c, size))
    # reading order: top to bottom, then left to right
    lines.sort(key=lambda l: (round(l.box[1] / 12), l.box[0]))
    return lines


def layout(lines: List[Line], width: float) -> Tuple[str, List[Tuple[Line, int]]]:
    """Reading-order text of OCR lines and each line's offset in it.

    Two-column blocks (requisites «Арендодатель | Арендатор») are read column by
    column; lines of one paragraph are joined with a space (a name or an address
    wrapped to the next line stays one match), paragraphs with a newline."""
    if not lines:
        return '', []
    mid = width * 0.45
    right = [l for l in lines if l.box[0] >= mid]
    # a real right column: several lines start there
    two_cols = len(right) >= 4
    cols = [[l for l in lines if not two_cols or l.box[0] < mid], right if two_cols else []]
    order = []
    for col in cols:
        # rows: pieces of one visual line (OCR may split it) share a vertical band
        rows = []
        for l in sorted(col, key=lambda l: (l.box[1] + l.box[3]) / 2):
            cy = (l.box[1] + l.box[3]) / 2
            h = l.box[3] - l.box[1]
            if rows and abs(rows[-1][0] - cy) < 0.5 * h:
                rows[-1][1].append(l)
            else:
                rows.append([cy, [l]])
        for _, row in rows:
            order.extend(sorted(row, key=lambda l: l.box[0]))
    text, offs = [], []
    pos = 0
    prev = None
    for l in order:
        if prev is not None:
            h = max(prev.box[3] - prev.box[1], 1)
            gap = l.box[1] - prev.box[3]
            same_row = abs((l.box[1] + l.box[3]) / 2 - (prev.box[1] + prev.box[3]) / 2) < 0.5 * h
            same_par = same_row or -0.8 * h <= gap < 1.2 * h and abs(l.box[0] - prev.box[0]) < 6 * h \
                and not prev.text.rstrip().endswith(('.', ':', ';'))
            sep = ' ' if same_par else '\n'
            text.append(sep)
            pos += 1
        offs.append((l, pos))
        text.append(l.text)
        pos += len(l.text)
        prev = l
    return ''.join(text), offs
