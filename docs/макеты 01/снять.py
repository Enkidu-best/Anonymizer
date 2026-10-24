"""Снимки макета 01 (1440×900 и 900×900, светлая и тёмная тема), безголовый Chromium - окна и браузер Андрея не открываются:

    .venv312/bin/python "docs/макеты 01/снять.py"
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
PAGE = (HERE / "Макет - Anonymizer 4.0 (01).html").as_uri()
STATES = {"empty": "00 Обезличить - пусто", "upload": "01 Обезличить - файлы выбраны",
          "processing": "02 Обезличить - обработка", "done": "03 Обезличить - готово",
          "preview": "04 Просмотр с правкой", "unmask": "05 Окно «Не маскировать»", "restore": "06 Вернуть данные",
          "restoretext": "06а Вернуть данные - текст",
          "dict": "07 Словарь", "sessions": "08 Сессии", "delete": "09 Окно удаления сессии", "settings": "10 Настройки"}
WIDTHS = (1440, 900)
with sync_playwright() as pw:
    br = pw.chromium.launch()
    for width in WIDTHS:
        for theme, name in (("light", "светлая"), ("dark", "тёмная")):
            pg = br.new_page(viewport={"width": width, "height": 900})
            for st, title in STATES.items():
                pg.goto(f"{PAGE}?theme={theme}#{st}")
                pg.reload()
                pg.wait_for_timeout(150)
                pg.screenshot(path=str(HERE / f"{title} - {width} - {name}.png"))
            pg.close()
    br.close()
