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
