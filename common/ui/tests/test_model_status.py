"""Общий блок ui: строка модели (web/model-status.js) и меню значками в узком окне (web/narrow-menu.js).

    <python с playwright> -m pytest «_общие модули/ui/tests/test_model_status.py»
"""
import sys
from pathlib import Path

MOD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MOD.parent))

TOKENS = """
:root { --bg:#f5f6f8; --surface:#fff; --border:#dfe3e8; --text:#1b2230; --faint:#636c79; --accent:#1f3864;
  --accent-text:#fff; --info:#2450c8; --ok:#276b43; --warn:#9a6200; --bad:#b3261e; }
:root[data-theme=dark] { --bg:#0f1218; --surface:#171b23; --border:#2a3140; --text:#e5e9f0; --faint:#8a94a3;
  --accent:#8fb0e8; --accent-text:#0f1218; --info:#8fb0e8; --ok:#6cc28f; --warn:#e3b25a; --bad:#f08b84; }
body { background:var(--bg); color:var(--text); font:17px/1.5 sans-serif; margin:0; display:flex; height:100vh; }
.side { width:220px; background:var(--surface); } .side.collapsed { width:60px; } .side.collapsed .lt { display:none; }
.mi { display:flex; gap:8px; padding:8px; color:var(--text); background:none; border:0; text-decoration:none; }
header { padding:10px; }
"""


def page_html(lang='ru') -> str:
    js = ''.join((MOD / 'web' / f).read_text(encoding='utf-8') for f in ('model-status.js', 'narrow-menu.js'))
    return (f'<!doctype html><html lang="{lang}"><head><style>{TOKENS}</style></head><body>'
            '<nav class="side" id="side"><a class="mi" href="#a"><svg width="16" height="16"></svg><span class="lt">Документы</span></a>'
            '<button class="mi" id="b2"><span class="lt">Настройки</span></button></nav>'
            '<main><header><button id="model"></button></header></main>'
            f'<script>{js}</script><script>window.retried = 0; ModelStatus.mount("#model", {{onRetry: () => window.retried++}});'
            'NarrowMenu.bind("#side", {items: ".mi"});</script></body></html>')


def _color(pg, sel='#model .ms-dot'):
    return pg.evaluate(f"getComputedStyle(document.querySelector('{sel}')).backgroundColor")


def test_states_words_colors_and_error_popup():
    from playwright.sync_api import sync_playwright
    from a11y import contrast
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={'width': 1440, 'height': 900})
        pg.set_content(page_html())
        want = {'ready': ('Модель готова', 'rgb(39, 107, 67)'), 'working': ('Работает · 40 %', 'rgb(36, 80, 200)'),
                'loading': ('Модель загружается · 10 %', 'rgb(99, 108, 121)'),
                'queued': ('Занята «Herald» - в очереди', 'rgb(154, 98, 0)'),
                'off': ('Нет модели', 'rgb(99, 108, 121)'), 'error': ('Ошибка модели', 'rgb(179, 38, 30)')}
        for theme in ('light', 'dark'):
            pg.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
            for st, (text, rgb) in want.items():
                pg.evaluate("s => ModelStatus.set(s)", {'state': st, 'program': 'Herald',
                                                         'progress': {'working': 40, 'loading': 10}.get(st)})
                assert pg.inner_text('#model') == text
                assert pg.get_attribute('#model', 'data-state') == st
                if theme == 'light':
                    assert _color(pg) == rgb, (st, _color(pg))
                bad = contrast.check(pg)
                assert not bad, (theme, st, contrast.summary(bad))
        pg.evaluate("ModelStatus.set({state: 'working', text: 'Работает: файл 1 из 3'})")   # свои слова программы
        assert pg.inner_text('#model') == 'Работает: файл 1 из 3'
        pg.evaluate("ModelStatus.set({state: 'error', detail: 'Ollama не отвечает (порт 11434).'})")
        pg.click('#model')
        assert pg.is_visible('#ms-pop') and 'Ollama не отвечает' in pg.inner_text('#ms-pop')
        assert not contrast.check(pg, '#ms-pop')
        pg.keyboard.press('Escape')
        assert pg.locator('#ms-pop').count() == 0
        pg.click('#model')
        pg.click('#ms-pop .ms-retry')
        assert pg.evaluate('window.retried') == 1 and pg.locator('#ms-pop').count() == 0
        pg.click('#model')
        pg.mouse.click(700, 600)                                  # щелчок мимо
        assert pg.locator('#ms-pop').count() == 0
        try:
            pg.evaluate("ModelStatus.set({state: 'busy'})")
            raise AssertionError('неизвестное состояние принято')
        except Exception as e:
            assert 'неизвестное состояние' in str(e)
        br.close()


def test_english_words():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page()
        pg.set_content(page_html('en'))
        pg.evaluate("ModelStatus.set({state: 'queued', program: 'Herald'})")
        assert pg.inner_text('#model') == 'Busy: «Herald» - queued'
        pg.evaluate("ModelStatus.set({state: 'ready'})")
        assert pg.inner_text('#model') == 'Model ready'
        br.close()


def test_narrow_menu_icons_with_tooltips():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={'width': 900, 'height': 800})
        pg.set_content(page_html())
        assert pg.evaluate("document.getElementById('side').classList.contains('collapsed')")
        assert pg.evaluate("document.documentElement.classList.contains('ui-narrow')")
        assert pg.get_attribute('#side a.mi', 'title') == 'Документы'
        assert pg.get_attribute('#b2', 'title') == 'Настройки'
        assert pg.evaluate("document.getElementById('side').getBoundingClientRect().width") == 60
        pg.set_viewport_size({'width': 1440, 'height': 900})
        pg.wait_for_function("!document.getElementById('side').classList.contains('collapsed')")
        assert pg.get_attribute('#b2', 'title') is None             # подсказка - только у значков
        pg.set_viewport_size({'width': 900, 'height': 800})
        pg.wait_for_function("document.getElementById('side').classList.contains('collapsed')")
        br.close()
