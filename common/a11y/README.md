# a11y - контраст текста в окне (общий модуль)

Версия 1.0.0 · 24.10.2026 · задание «Понятный документ» 12, пункт А4. Стандарт: «Единый стандарт программ», правило 4.6
(контраст не меньше 4,5 : 1 в светлой и тёмной теме).

## Что делает

`contrast.check(page, within=None)` - запускает axe-core 4.10.2 (правило `color-contrast`, WCAG 2.1 AA: 4,5 : 1 для
обычного текста, 3 : 1 для крупного) на открытой странице Playwright и возвращает нарушения:
`[{where, text, ratio, need, fg, bg}]`. Пустой список - весь текст читается. `within` - CSS-селектор части страницы.
`contrast.summary(found)` - строки для отчёта.

## Как подключить

1. Копия - `scripts/sync_common.sh a11y <проект>` → `<проект>/common/a11y/` (не править на месте).
2. В сценариях окна (Playwright) - после каждого экрана в обеих темах:

   ```python
   from common.a11y import contrast
   bad = contrast.check(page)
   assert not bad, contrast.summary(bad)
   ```

3. axe-core подставляется в страницу самим модулем (`page.add_script_tag`), в саму программу он не попадает - только в
   проверки. Сеть не нужна.

## Лицензия

axe-core - Deque Systems, MPL-2.0 (`LICENSE-axe-core.txt`), файл `axe.min.js` без изменений.

## Проверка модуля

`<python с playwright> _общие модули/a11y/tests/test_contrast.py` - чёрный текст на тёмном фоне найден, светлый и лист
документа - нет.

## Журнал

- 1.0.0 (24.10.2026) - первая версия: `check`, `summary`, axe-core 4.10.2.
