// Selector de apariencia: color (data-palette) y modo claro/oscuro (data-theme).
// La elección se guarda en el navegador; el <head> de base.html la aplica antes de pintar.
(() => {
  'use strict';
  const root = document.documentElement;

  function sync() {
    document.querySelectorAll('[data-set-palette]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.setPalette === root.dataset.palette)));
    document.querySelectorAll('[data-set-theme]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.setTheme === root.dataset.theme)));
  }

  function apply(palette, theme) {
    root.dataset.palette = palette;
    root.dataset.theme = theme;
    try {
      localStorage.setItem('st-palette', palette);
      localStorage.setItem('st-theme', theme);
    } catch (e) { /* sin almacenamiento: el cambio dura hasta recargar */ }
    sync();
    document.dispatchEvent(new CustomEvent('solartrack:theme'));
  }

  document.addEventListener('click', (e) => {
    const palette = e.target.closest('[data-set-palette]');
    if (palette) { apply(palette.dataset.setPalette, root.dataset.theme); return; }
    const theme = e.target.closest('[data-set-theme]');
    if (theme) apply(root.dataset.palette, theme.dataset.setTheme);
  });

  sync();
})();
