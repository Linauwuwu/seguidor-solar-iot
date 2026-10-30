// Panel "Supervisión PLC": usa los datos que dashboard.js ya obtiene cada 2,5 s (evento solartrack:datos).
(() => {
  'use strict';
  const panel = document.getElementById('plc-supervision');
  if (!panel) return;
  const $ = id => document.getElementById(id);
  const FRESH_MS = 15000; // misma ventana que "Datos recientes"
  let habilitarId = null;

  function item(name, ok, label) {
    const el = panel.querySelector(`[data-plc-item="${name}"]`);
    el.classList.remove('solar-connected', 'solar-disconnected');
    if (ok !== null) el.classList.add(ok ? 'solar-connected' : 'solar-disconnected');
    el.querySelector('[data-plc-label]').textContent = label;
  }

  function render(data) {
    const alerts = [];
    if (!data) {
      alerts.push('Sin conexión con Django: el estado mostrado puede estar desactualizado.');
      return showAlerts(alerts);
    }
    const now = new Date(data.server_time).getTime();
    const plcDev = (data.devices || []).find(d => d.slug === 'plc');
    const sensors = {};
    for (const s of data.sensors || []) {
      if (s.device_slug === 'plc' || (s.device_slug === 'seguidor-solar-iot' && s.variable === 'movimiento_permitido')) sensors[s.variable] = s;
    }
    const val = v => (sensors[v] && sensors[v].value !== null ? Number(sensors[v].value) : null);
    const fresh = s => s && s.timestamp && now - new Date(s.timestamp).getTime() <= FRESH_MS;
    habilitarId = sensors.plc_habilitar ? sensors.plc_habilitar.id : null;

    const bridge = !!(plcDev && plcDev.last_seen_at && now - new Date(plcDev.last_seen_at).getTime() <= FRESH_MS);
    const plcOk = bridge && val('plc_conectado') === 1;
    const known = v => (bridge && val(v) !== null);
    const yesNo = (v, si, no) => (known(v) ? (val(v) === 1 ? si : no) : 'Sin datos');

    item('raspberry', plcDev ? bridge : null, !plcDev ? 'No configurado (preparar_plc)' : bridge ? 'Comunicando' : 'Sin señal reciente');
    item('plc', bridge ? plcOk : null, bridge ? (plcOk ? 'Conectado' : 'Desconectado') : 'Sin datos');
    // El indicador "PLC" de Estado de conexión se basa en last_seen; con el PLC real también exige plc_conectado.
    const top = document.querySelector('[data-solar-device="plc"]');
    if (top && bridge && !plcOk) {
      top.classList.replace('solar-connected', 'solar-disconnected');
      top.querySelector('[data-solar-status-label]').textContent = 'Raspberry sin enlace al PLC';
    }
    item('online', known('plc_raspberry_online') ? val('plc_raspberry_online') === 1 : null, yesNo('plc_raspberry_online', 'TRUE (heartbeat vivo)', 'FALSE'));
    item('habilitar', known('plc_habilitar') ? val('plc_habilitar') === 1 : null, yesNo('plc_habilitar', 'TRUE', 'FALSE'));
    item('bloqueo', known('plc_bloqueo') ? val('plc_bloqueo') !== 1 : null, yesNo('plc_bloqueo', 'ACTIVO', 'Inactivo'));
    const permiso = plcOk && val('plc_permiso') === 1;
    item('permiso', bridge ? permiso : null, bridge ? (permiso ? 'PERMITIDO' : 'BLOQUEADO') : 'Sin datos');
    const esp = sensors.movimiento_permitido;
    item('esp32', fresh(esp) ? esp.value === 1 : null, fresh(esp) ? (esp.value === 1 ? 'Puede moverse' : 'Servos bloqueados') : 'Sin datos de la ESP32');

    const deg = v => (known(v) ? val(v).toFixed(1) + '°' : '--°');
    $('plc-angulo-horizontal').textContent = deg('plc_angulo_horizontal');
    $('plc-angulo-vertical').textContent = deg('plc_angulo_vertical');
    $('plc-escritura').textContent = yesNo('plc_escritura', 'Habilitadas', 'Deshabilitadas (solo lectura)');
    $('plc-actualizado').textContent = plcDev && plcDev.last_seen_at
      ? new Intl.DateTimeFormat('es-CL', {timeZone: 'America/Santiago', dateStyle: 'short', timeStyle: 'medium'}).format(new Date(plcDev.last_seen_at))
      : '--';

    if (plcDev && !bridge) alerts.push('Pérdida de comunicación: la Raspberry no envía datos del PLC hace más de 15 s.');
    if (bridge && !plcOk) alerts.push('La Raspberry no logra comunicarse con el PLC.');
    if (bridge && plcOk && !permiso) {
      const causas = [];
      if (val('plc_habilitar') !== 1) causas.push('Habilitar = FALSE');
      if (val('plc_bloqueo') === 1) causas.push('Bloqueo activo');
      if (val('plc_raspberry_online') !== 1) causas.push('RaspberryOnline = FALSE (sin heartbeat)');
      alerts.push('Movimiento bloqueado por el PLC' + (causas.length ? ': ' + causas.join(', ') + '.' : '.'));
    }
    if (bridge && val('plc_escritura') === 0) alerts.push('PLC_ESCRITURA=false en la Raspberry: no se envía heartbeat ni Habilitar al PLC.');
    if (esp && fresh(esp) && esp.value === 1 && !permiso) alerts.push('Atención: la ESP32 informa movimiento permitido sin permiso vigente del PLC.');
    if (esp && !fresh(esp)) alerts.push('La ESP32 no informa su estado de bloqueo hace más de 15 s.');

    const canCommand = habilitarId !== null && plcOk && val('plc_escritura') === 1;
    panel.querySelectorAll('[data-plc-habilitar]').forEach(b => { b.disabled = !canCommand; });
    showAlerts(alerts);
  }

  function showAlerts(alerts) {
    const ul = $('plc-alerts');
    ul.replaceChildren(...alerts.map(text => Object.assign(document.createElement('li'), {textContent: text})));
  }

  function csrf() {
    return decodeURIComponent(document.cookie.split('; ').find(x => x.startsWith('csrftoken='))?.split('=')[1] || '');
  }

  panel.querySelectorAll('[data-plc-habilitar]').forEach(button => button.addEventListener('click', async () => {
    const root = document.getElementById('dashboard-root');
    if (habilitarId === null || !root) return;
    const url = root.dataset.commandUrlTemplate.replace(/\/0\/command\//, `/${habilitarId}/command/`);
    const state = $('plc-command-state');
    try {
      const response = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrf()}, body: JSON.stringify({value: Number(button.dataset.plcHabilitar)})});
      state.textContent = response.ok ? 'Comando enviado; esperando que el PLC lo confirme…' : 'No se pudo enviar el comando.';
    } catch (e) {
      state.textContent = 'No se pudo enviar el comando.';
    }
  }));

  document.addEventListener('solartrack:datos', e => render(e.detail));
})();
