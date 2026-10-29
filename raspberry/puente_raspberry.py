"""Puente que corre en la Raspberry Pi: ESP32 -> plataforma.

1. Escucha lo que publica la ESP32 en lina/esp32/... (valor como texto plano).
2. Lo reenvía a <prefijo>/seguidor-solar-iot/<variable> con el token de la ESP32,
   que es el formato que guarda run_mqtt_listener.
3. Cada INTERVALO segundos publica el estado de la propia Raspberry en
   <prefijo>/raspberry/<variable> con su token, para que aparezca conectada.

No necesita Django ni la base de datos: los tokens se copian del dashboard.

Uso (en la Raspberry):
    pip install paho-mqtt==1.6.1
    MQTT_BROKER_HOST=ip  MQTT_USERNAME=user MQTT_PASSWORD=... \
    ESP32_TOKEN=... RASPBERRY_TOKEN=... python3 puente_raspberry.py
"""

import json
import math
import os
import time

import paho.mqtt.client as mqtt

BROKER = os.environ.get("MQTT_BROKER_HOST", "localhost")
PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
USERNAME = os.environ.get("MQTT_USERNAME", "")
PASSWORD = os.environ.get("MQTT_PASSWORD", "")
PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "ecs")
ESP32_TOKEN = os.environ.get("ESP32_TOKEN", "")
RASPBERRY_TOKEN = os.environ.get("RASPBERRY_TOKEN", "")
ESP32_SLUG = "seguidor-solar-iot"
RASPBERRY_SLUG = "raspberry"  # el dashboard reconoce raspberry, raspberry-pi o rpi
INTERVALO = float(os.environ.get("INTERVALO", "5"))

# Topics que publica la ESP32 -> variable en la plataforma (los mismos de backend/puente_esp32.py)
TOPICS = {
    "lina/esp32/light/left": "light_left",
    "lina/esp32/light/right": "light_right",
    "lina/esp32/light/up": "light_up",
    "lina/esp32/light/down": "light_down",
    "lina/esp32/azimuth": "azimuth",
    "lina/esp32/zenith": "zenith",
    "lina/esp32/solar/azimuth": "solar_azimuth",
    "lina/esp32/solar/elevation": "solar_elevation",
    "lina/esp32/latitude": "latitude",
    "lina/esp32/longitude": "longitude",
}

reenviados = 0


def forward(topic, raw_payload):
    """(topic, payload) para la plataforma, o None si el mensaje no corresponde o no es un número."""
    variable = TOPICS.get(topic)
    if variable is None:
        return None
    try:
        value = float(raw_payload.decode("utf-8").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return f"{PREFIX}/{ESP32_SLUG}/{variable}", json.dumps({"value": value, "token": ESP32_TOKEN})


def temp_cpu():
    with open("/sys/class/thermal/thermal_zone0/temp") as f:
        return int(f.read()) / 1000


def uso_cpu():
    return min(100.0, os.getloadavg()[0] / os.cpu_count() * 100)


def uso_memoria():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])
    return 100 * (1 - info["MemAvailable"] / info["MemTotal"])


ESTADO = {
    "rpi_mensajes_reenviados": lambda: reenviados,
    "rpi_temp_cpu": temp_cpu,
    "rpi_uso_cpu": uso_cpu,
    "rpi_uso_memoria": uso_memoria,
}


def status_messages():
    """Estado de la Raspberry; las lecturas que no existan en este equipo se omiten."""
    messages = []
    for variable, read in ESTADO.items():
        try:
            value = round(read(), 2)
        except (OSError, ValueError, KeyError, AttributeError):
            continue
        messages.append((f"{PREFIX}/{RASPBERRY_SLUG}/{variable}", json.dumps({"value": value, "token": RASPBERRY_TOKEN})))
    return messages


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.subscribe("lina/esp32/#", qos=1)
        print("Conectado a Mosquitto; escuchando lina/esp32/#", flush=True)
    else:
        print(f"Error de conexión MQTT: {rc}", flush=True)


def on_message(client, userdata, msg):
    global reenviados
    out = forward(msg.topic, msg.payload)
    if out is None:
        return
    client.publish(*out, qos=1)
    reenviados += 1


def main():
    if not ESP32_TOKEN or not RASPBERRY_TOKEN:
        raise SystemExit("Faltan ESP32_TOKEN y/o RASPBERRY_TOKEN: cópialos desde Dispositivos en el dashboard.")
    client = mqtt.Client()
    if USERNAME:
        client.username_pw_set(USERNAME, PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()
    print(f"Puente Raspberry activo hacia {BROKER}:{PORT} (Ctrl+C para detener)", flush=True)
    try:
        while True:
            for topic, payload in status_messages():
                client.publish(topic, payload, qos=1)
            time.sleep(INTERVALO)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
