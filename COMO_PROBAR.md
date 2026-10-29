# Seguidor Solar IoT — para probar con la ESP32 y el seguidor solar

La **ESP32 y el seguidor son reales**; solo el **PLC se simula**, a partir de los datos que
envía la ESP32. Para probar sin hardware usa la carpeta `solo-simulado`.

| Equipo | Origen |
|---|---|
| ESP32 (`seguidor-solar-iot`) | **real**, por MQTT |
| PLC (`plc`) | simulado con `python manage.py simular_seguidor --esp32-real` |

## Qué hace el PLC simulado

En cada ciclo lee los servos (`azimuth`, `zenith`) y los 4 LDR de la ESP32 real:
- inclinación y azimut del panel = ángulos reales de los servos;
- motores encendidos cuando los servos reales se mueven;
- voltaje, corriente y potencia según cuánta luz ven los LDR y qué tan parejos están
  (panel bien apuntado = más potencia).

Si la ESP32 deja de enviar datos por más de 15 s, el PLC informa motores detenidos y 0 W.
Los botones Manual/Automático y los sliders del dashboard envían los comandos por MQTT a la
ESP32 real (`mode`, `servo`, `servo2`).

## Cómo correrlo (SQLite, sin Docker)

Dentro de `backend/`:

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000
```

1. Entrar a http://localhost:8000, ir a **Dispositivos → Agregar** y registrar la ESP32 con
   slug `seguidor-solar-iot`. Copiar el token generado al firmware.
2. Tener corriendo el broker Mosquitto y apuntar la ESP32 a la IP de este PC (puerto 1883).
3. En otras terminales dentro de `backend/` (con `MQTT_BROKER_HOST=localhost`):

```bash
python manage.py run_mqtt_listener
python puente_esp32.py              # solo si la ESP32 publica en los topics lina/esp32/...
python manage.py simular_seguidor --esp32-real
```

En el dashboard, ESP32 y PLC deben aparecer en verde en "Estado de conexión".

## Con Docker

```bash
cp .env.example .env    # completar contraseñas
docker compose up --build
```

El servicio `simulacion_seguidor` arranca el PLC simulado con `--esp32-real`. Registrar la ESP32
en el dashboard (paso 1) y apuntarla al broker del PC.

No levantar esta carpeta y `solo-simulado` al mismo tiempo: ambas usan los puertos 8000 y 1883.
