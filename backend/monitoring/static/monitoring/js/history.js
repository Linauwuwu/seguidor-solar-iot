// Gráfico del Historial con los colores del tema elegido.
(() => {
  'use strict';
  const canvas = document.getElementById('history-chart');
  const source = document.getElementById('history-data');
  if (!canvas || !source || typeof Chart === 'undefined') return;

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const localTime = (t) => new Date(t).toLocaleString('es-CL', { timeZone: 'America/Santiago', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const points = JSON.parse(source.textContent).map((p) => ({ x: new Date(p.t).getTime(), y: Number(p.v) }));

  const chart = new Chart(canvas, {
    type: 'line',
    data: { datasets: [{ label: canvas.dataset.label, data: points, parsing: false, pointRadius: 0, pointHoverRadius: 4, borderWidth: 2.4, tension: 0.25, fill: true }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'nearest', intersect: false },
      plugins: { legend: { display: false }, tooltip: { callbacks: { title: (items) => (items.length ? localTime(items[0].parsed.x) : '') } } },
      scales: {
        x: { type: 'linear', ticks: { maxTicksLimit: 6, callback: (v) => localTime(v) }, grid: {}, title: { display: true, text: 'Hora (Santiago)' } },
        y: { ticks: {}, grid: {}, title: { display: true, text: canvas.dataset.label } }
      }
    }
  });

  function applyTheme() {
    const ds = chart.data.datasets[0];
    ds.borderColor = cssVar('--st-accent');
    ds.backgroundColor = cssVar('--st-soft');
    for (const axis of Object.values(chart.options.scales)) {
      axis.grid.color = cssVar('--st-border');
      axis.ticks.color = cssVar('--st-muted');
      axis.title.color = cssVar('--st-muted');
    }
    chart.update('none');
  }
  applyTheme();
  document.addEventListener('solartrack:theme', applyTheme);
})();
