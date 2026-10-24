# -*- coding: utf-8 -*-
"""contrast.check на своей временной странице (безголовый Chromium). Пропускается, если нет Playwright.

    <python с playwright> "_общие модули/a11y/tests/test_contrast.py"
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

try:
    from playwright.sync_api import sync_playwright
except Exception:                       # noqa: BLE001 - нет Playwright - проверка пропускается
    sync_playwright = None

from a11y import contrast  # noqa: E402

PAGE = """<!doctype html><html><body style="background:#171b23;font:16px system-ui">
<p id="bad" style="color:#1b2230">чёрный текст на тёмном фоне</p>
<p id="ok" style="color:#e5e9f0">светлый текст на тёмном фоне</p>
<div id="paper" style="background:#fff;color:#1a1a1a;padding:8px">лист документа</div></body></html>"""


@unittest.skipIf(sync_playwright is None, "нет Playwright")
class Contrast(unittest.TestCase):
    def test_finds_dark_on_dark_only(self):
        with sync_playwright() as pw:
            br = pw.chromium.launch()
            pg = br.new_page()
            pg.set_content(PAGE)
            found = contrast.check(pg)
            self.assertEqual([x["where"] for x in found], ["#bad"])
            self.assertLess(found[0]["ratio"], 4.5)
            self.assertIn("#bad", contrast.summary(found))
            self.assertEqual(contrast.check(pg, "#paper"), [])
            br.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
