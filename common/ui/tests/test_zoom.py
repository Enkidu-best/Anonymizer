"""Общий блок ui: увеличение картинки (web/zoom.js) - щелчок, Esc и щелчок мимо, ← / →, колесо, фокус, контраст.

    <python с playwright> «_общие модули/ui/tests/test_zoom.py»
"""
import base64
import sys
import zlib
from pathlib import Path

MOD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MOD.parent))


def _png(r, g, b) -> str:
    raw = b''.join(b'\x00' + bytes([r, g, b]) * 40 for _ in range(30))
    def chunk(t, d):
        import struct
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    import struct
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 40, 30, 8, 2, 0, 0, 0)) + \
        chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    return 'data:image/png;base64,' + base64.b64encode(png).decode()


def page_html() -> str:
    imgs = ''.join(f'<img class="zoomable" alt="Страница {i}" src="{_png(*c)}">' for i, c in
                   enumerate([(200, 40, 40), (40, 200, 40), (40, 40, 200)], 1))
    js = (MOD / 'web' / 'zoom.js').read_text(encoding='utf-8')
    return f'<!doctype html><html lang="ru"><body><button id="before">до</button><div id="box">{imgs}</div>' \
           f'<script>{js}</script><script>ZoomImage.bind("#box")</script></body></html>'


def test_zoom():
    from playwright.sync_api import sync_playwright
    from a11y import contrast
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page()
        pg.set_content(page_html())
        pg.focus('#before')
        pg.click('#box img >> nth=1')
        assert pg.is_visible('#zi-ov')
        assert pg.inner_text('.zi-count') == '2 из 3' and pg.inner_text('.zi-name') == 'Страница 2'
        pg.keyboard.press('ArrowRight')
        assert pg.evaluate('ZoomImage.state.index') == 2
        pg.keyboard.press('ArrowRight')
        assert pg.evaluate('ZoomImage.state.index') == 0          # по кругу
        pg.keyboard.press('ArrowLeft')
        assert pg.evaluate('ZoomImage.state.index') == 2
        pg.mouse.move(400, 300)
        pg.mouse.wheel(0, -300)
        pg.wait_for_timeout(100)
        assert pg.evaluate('ZoomImage.state.scale') > 1
        bad = contrast.check(pg)
        assert not bad, contrast.summary(bad)
        pg.mouse.click(5, 5)                                      # щелчок мимо картинки
        assert not pg.is_visible('#zi-ov')
        pg.click('#box img >> nth=0')
        pg.keyboard.press('Escape')
        assert pg.locator('#zi-ov').count() == 0 and pg.evaluate('ZoomImage.state') is None
        pg.focus('#before')
        pg.evaluate('ZoomImage.open([{src: document.images[0].src, name: "одна"}])')
        assert pg.is_hidden('.zi-prev') and pg.inner_text('.zi-count') == ''
        pg.keyboard.press('Escape')
        assert pg.evaluate('document.activeElement.id') == 'before'   # фокус вернулся
        br.close()


if __name__ == '__main__':
    test_zoom()
    print('ui/zoom: все проверки прошли')
