# Integración ESP32 ↔ MQTT ↔ Raspberry ↔ PLC S7-1200 ↔ Django

```
ESP32 ──lina/esp32/*──► Mosquitto ◄──solartrack/plc/estado, ecs/plc/*── Raspberry (plc_mqtt.py) ◄─Snap7─► PLC DB1
  ▲                        │  ▲                                              │
  └──lina/plc/permiso──────┘  └──ecs/plc/plc_habilitar/set── Django ◄────────┘ (run_mqtt_listener → PostgreSQL)
```

- El **PLC** decide: `PermisoMovimiento := Habilitar AND NOT Bloqueo AND RaspberryOnline`.
- La **Raspberry** lee DB1 cada 1 s y publica `lina/plc/permiso` = `1`/`0`. Ante cualquier falla publica `0`.
  Si se cae sin avisar, el broker publica `0` por ella (mensaje "last will").
- La **ESP32** solo mueve los servos si recibió `1` hace menos de 5 s y MQTT está conectado.
  Vale para los modos automático y manual. Al arrancar no activa los servos hasta el primer permiso.
- **Django** muestra el panel *Supervisión PLC* y guarda en PostgreSQL los cambios de estado, más una lectura cada 5 s.

> El bloqueo es por **software** y **no reemplaza una parada de emergencia** cableada.

## 1. Cambio en el PLC (TIA Portal V19): heartbeat real

Ahora mismo nadie hace pasar `RaspberryOnline` a TRUE, por eso el permiso siempre queda en FALSE.
Con este cambio el **PLC lo calcula solo**: la Raspberry incrementa un contador cada segundo, y si ese
contador deja de cambiar durante 3 s, el PLC pone `RaspberryOnline = FALSE`. Nadie escribe `RaspberryOnline`.

**a) En `FB_SolarTrack`, sección *Static*, agrega al final** (al final, para no mover los offsets que ya existen):

| Nombre | Tipo | Valor inicial |
|---|---|---|
| `HeartbeatRpi` | `Int` | 0 |
| `HeartbeatAnterior` | `Int` | 0 |
| `TimerHeartbeat` | `TOF_TIME` | — |

**b) Reemplaza la línea del permiso por:**

```scl
// Heartbeat de la Raspberry: cada cambio del contador reinicia un TOF de 3 s.
#TimerHeartbeat(IN := #HeartbeatRpi <> #HeartbeatAnterior,
                PT := T#3S);
#HeartbeatAnterior := #HeartbeatRpi;
#RaspberryOnline := #TimerHeartbeat.Q;

// Recomendado: si se pierde la comunicación, Habilitar se cae y hay que volver
// a habilitar desde el dashboard (evita que el seguidor reanude solo).
IF NOT #RaspberryOnline THEN
    #Habilitar := FALSE;
END_IF;

#PermisoMovimiento := #Habilitar
                     AND NOT #BloqueoMovimiento
                     AND #RaspberryOnline;
```

**c) Comprobaciones antes de cargar el programa:**
1. En las propiedades de DB1, **"Acceso optimizado al bloque" debe seguir desactivado**.
2. Mira en DB1 el **offset** de `HeartbeatRpi`. Si no es `12.0`, cambia `PLC_BYTE_HEARTBEAT` en `plc.env`.
3. En la llamada a `FB_SolarTrack` dentro de OB1, **`Habilitar` y `RaspberryOnline` no deben tener nada conectado**.
   Si tienen un tag o una constante conectada, el OB1 sobrescribe en cada ciclo lo que escriba la Raspberry.
   Lo mismo vale para `AnguloVertical`/`AnguloHorizontal` si vas a usar `PLC_ESCRIBIR_ANGULOS=true`.
4. Compila y **carga en el dispositivo**. TIA puede pedir reinicializar DB1: los valores vuelven a su valor inicial.

Mientras no hagas este cambio, la Raspberry igual **lee** el PLC y lo muestra en el dashboard, pero el permiso
quedará en FALSE y la ESP32 seguirá bloqueada (es el estado seguro).

## 2. Raspberry: instalar el servicio

En el PC, copia la carpeta `raspberry/` a la Raspberry (con `scp -r raspberry pi@IP:~/solartrack-repo` o un pendrive). Después, en la Raspberry:

```bash
cd ~/solartrack
cp plc_mqtt.py plc_mqtt_original.py.bak        # respaldo del script anterior
cp ~/solartrack-repo/plc_mqtt.py ~/solartrack-repo/plc.env.example ~/solartrack-repo/solartrack-plc.service .
cp plc.env.example plc.env
nano plc.env          # MQTT_BROKER_HOST, MQTT_USERNAME, MQTT_PASSWORD, PLC_TOKEN, RASPBERRY_TOKEN
chmod 600 plc.env
```

> **Ojo:** el `plc_mqtt.py` anterior se conectaba a Mosquitto **sin usuario ni contraseña**, y el broker
> tiene `allow_anonymous false`. Lo más probable es que por eso no se viera llegar nada al broker.
> El nuevo script usa `MQTT_USERNAME`/`MQTT_PASSWORD`; deben ser los mismos del `.env` de Docker.

Prueba manual (Ctrl+C para salir):
```bash
~/solartrack/venv/bin/python ~/solartrack/plc_mqtt.py
```
Debe mostrar `MQTT conectado a ...`, `PLC conectado en 10.41.32.1` y `Escrituras al PLC: DESHABILITADAS`.

Servicio automático:
```bash
sudo cp ~/solartrack/solartrack-plc.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now solartrack-plc
journalctl -u solartrack-plc -f
```
Si tu usuario no es `pi`, cambia `User=` y las rutas `/home/pi` en el `.service` antes de copiarlo.

Habilitar las escrituras, **solo con autorización y con el cambio SCL ya cargado**:
```bash
sed -i 's/^PLC_ESCRITURA=.*/PLC_ESCRITURA=true/' ~/solartrack/plc.env
sudo systemctl restart solartrack-plc
```
Para volver a solo lectura, pon `PLC_ESCRITURA=false` y reinicia el servicio.

Pruebas sin hardware (no necesitan snap7 ni paho):
```bash
cd ~/solartrack-repo && python3 -m unittest test_plc_mqtt -v
```

## 3. PC (Docker / Django)

```bash
docker compose stop simulacion_seguidor        # el PLC simulado deja de escribir en "plc"
docker compose up -d --build web mqtt_listener
docker compose exec web python manage.py preparar_plc
```
`preparar_plc` convierte el dispositivo `plc` en el PLC real: le quita la marca *simulado*, desactiva los sensores
simulados (no los borra) y muestra `PLC_TOKEN=...` (y `RASPBERRY_TOKEN=...` si existe). Copia esas líneas a `plc.env`.

La simulación sigue disponible, claramente separada, para practicar sin el PLC:
`docker compose --profile simulacion up -d simulacion_seguidor`. No la uses al mismo tiempo que el PLC real.

Si la Raspberry o la ESP32 no llegan al broker, abre el puerto 1883 en el firewall de Windows, solo para la red local:
```powershell
New-NetFirewallRule -DisplayName "Mosquitto LAN" -Direction Inbound -Protocol TCP -LocalPort 1883 -Action Allow -Profile Private
```

## 4. Validación física mínima

| # | Prueba | Resultado esperado |
|---|---|---|
| 1 | En el PC: `docker compose exec mosquitto mosquitto_sub -u USUARIO -P CLAVE -t 'lina/plc/#' -t 'solartrack/plc/#' -v` | Llega `lina/plc/permiso 0` y el JSON del PLC cada 1 s |
| 2 | Dashboard → *Supervisión PLC* | Raspberry ↔ PLC "Comunicando", PLC "Conectado", escrituras "Deshabilitadas" |
| 3 | ESP32 con el firmware nuevo, monitor serie | `Servos BLOQUEADOS hasta recibir permiso del PLC`; en el dashboard, ESP32 obedece = "Servos bloqueados" |
| 4 | Carga el SCL, pon `PLC_ESCRITURA=true` y reinicia el servicio | En TIA (vigilar DB1), `HeartbeatRpi` sube 1 por segundo y `RaspberryOnline = TRUE` |
| 5 | Dashboard → **Habilitar movimiento** | `Habilitar = TRUE`, `Permiso = PERMITIDO`, la ESP32 muestra `PLC: movimiento PERMITIDO` y se mueve |
| 6 | Activa `BloqueoMovimiento` (o pulsa **Deshabilitar**) | Permiso BLOQUEADO; la ESP32 no inicia más movimientos. Un movimiento que ya está en curso termina (máx. ~3 s), porque `moverServoSuave` bloquea la lectura MQTT |
| 7 | `sudo systemctl stop solartrack-plc` | En ≤ 3 s `RaspberryOnline = FALSE`, `Habilitar = FALSE`; la ESP32 se bloquea; el dashboard muestra la alerta de pérdida de comunicación |
| 8 | Desconecta el cable Ethernet del PLC | El dashboard muestra "Raspberry sin enlace al PLC"; la ESP32 se bloquea |

## Topics

| Topic | Quién publica | Contenido |
|---|---|---|
| `lina/plc/permiso` (nuevo) | Raspberry, cada 1 s | `1` / `0` (sin retain) |
| `solartrack/plc/estado` | Raspberry, cada 1 s | JSON con el formato del `plc_mqtt.py` original |
| `ecs/plc/plc_*` (nuevo) | Raspberry, al cambiar y cada 5 s | `{"value", "token"}` → PostgreSQL |
| `ecs/plc/plc_habilitar/set` (nuevo) | Django (botones) | `{"value": 0/1, "token"}`; la Raspberry valida el token |
| `ecs/seguidor-solar-iot/movimiento_permitido` (nuevo) | ESP32, cada 1 s | `1` / `0` |
| `lina/esp32/*`, `ecs/seguidor-solar-iot/*` | ESP32 | **Sin cambios** |
