# -*- coding: utf-8 -*-
"""Контраст текста в окне - общий модуль (описание - README.md рядом): axe-core (правило color-contrast, WCAG 2.1 AA:
4,5 : 1 для обычного текста, 3 : 1 для крупного) на открытой странице Playwright."""
from __future__ import annotations

from pathlib import Path

AXE = Path(__file__).resolve().parent / "axe.min.js"


def check(page, within: str | None = None) -> list[dict]:
    """Нарушения контраста на странице (или внутри селектора within): [{где, текст, контраст, нужно, цвет, фон}].
    Пустой список - всё читается."""
    if not page.evaluate("typeof window.axe !== 'undefined'"):
        page.add_script_tag(path=str(AXE))
    found = page.evaluate("""async (sel) => {
      const ctx = sel ? document.querySelector(sel) : document;
      if (!ctx) return [];
      const r = await axe.run(ctx, { runOnly: ["color-contrast"], resultTypes: ["violations"] });
      const out = [];
      for (const v of r.violations) for (const n of v.nodes) {
        const d = ((n.any || [])[0] || {}).data || {};
        out.push({ where: n.target.join(" "), text: (n.html || "").replace(/<[^>]+>/g, "").trim().slice(0, 80),
                   ratio: d.contrastRatio, need: d.expectedContrastRatio, fg: d.fgColor, bg: d.bgColor });
      }
      return out;
    }""", within)
    return found


def summary(found: list[dict], limit: int = 8) -> str:
    """Строки для отчёта: «.card b - «Принять» - 2.1 : 1 (нужно 4.5 : 1), #777 на #fff»."""
    return "\n".join(f"{x['where']} - «{x['text']}» - {x['ratio']} : 1 (нужно {x['need']}), {x['fg']} на {x['bg']}"
                     for x in found[:limit])


__all__ = ["check", "summary", "AXE"]
