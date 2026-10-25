// Общий блок ui (стандарт 1.6, «_Единый вид 02», п. 2): в узком окне (до 1100 px, в том числе 900 px) меню слева
// сворачивается в значки, а не уезжает; у значка - подсказка с названием пункта. Эталон - Anonymizer 4.0.
//   NarrowMenu.bind("#side", {cls: "collapsed", items: ".mi", width: 1100, keep: () => ручной выбор, onChange})
// cls - класс «меню значками» программы (её CSS прячет подписи); items - пункты меню (подсказка - их текст);
// keep() - true, если человек сам свернул меню кнопкой: в широком окне оно остаётся свёрнутым.
// На <html> - класс ui-narrow, пока окно узкое.
(function () {
  'use strict';
  let bound = null;

  function label(it) {
    return (it.getAttribute('aria-label') || it.dataset.label || it.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function tips(side, sel, on) {
    side.querySelectorAll(sel).forEach(it => {
      if (on) {
        const l = label(it);
        if (l && (!it.title || it.dataset.nmTitle)) { it.title = l; it.dataset.nmTitle = '1'; }
      } else if (it.dataset.nmTitle) {
        it.removeAttribute('title');
        delete it.dataset.nmTitle;
      }
    });
  }

  window.NarrowMenu = {
    bind(target, o) {
      const side = typeof target === 'string' ? document.querySelector(target) : target;
      const opt = Object.assign({ cls: 'collapsed', items: 'a, button', width: 1100 }, o || {});
      const mq = matchMedia(`(max-width: ${opt.width}px)`);
      const apply = () => {
        const narrow = mq.matches;
        document.documentElement.classList.toggle('ui-narrow', narrow);
        if (narrow) side.classList.add(opt.cls);
        else if (!(opt.keep && opt.keep())) side.classList.remove(opt.cls);
        tips(side, opt.items, side.classList.contains(opt.cls));
        if (opt.onChange) opt.onChange(narrow);
      };
      // подписи меняются при смене языка - подсказку берём при наведении и фокусе
      const lazy = e => {
        const it = e.target.closest && e.target.closest(opt.items);
        if (it && side.contains(it) && side.classList.contains(opt.cls)) tips(side, opt.items, true);
      };
      side.addEventListener('pointerover', lazy);
      side.addEventListener('focusin', lazy);
      mq.addEventListener('change', apply);
      apply();
      bound = { side, opt, mq, apply };
      return { apply, get narrow() { return mq.matches; } };
    },
    get narrow() { return bound ? bound.mq.matches : false; },
    apply() { if (bound) bound.apply(); },
  };
})();
