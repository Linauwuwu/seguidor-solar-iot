"""Puente Raspberry Pi: PLC Siemens S7-1200 (Snap7) <-> MQTT/Mosquitto.

Cada PLC_CICLO segundos:
1. Lee DB1 del PLC (mismas direcciones que leer_plc.py).
2. Publica para la ESP32 el permiso de movimiento en TOPIC_PERMISO ("1" o "0").
   Ante cualquier falla (PLC caído, lectura con error, servicio detenido) publica "0".
3. Publica el JSON completo en TOPIC_ESTADO (el mismo topic que usaba plc_mqtt.py).
4. Publica el estado a Django en <prefijo>/plc/<variable> con PLC_TOKEN, solo cuando
   cambia o cada PUBLICAR_CADA segundos (así PostgreSQL guarda los eventos sin duplicar).
5. Solo si PLC_ESCRITURA=true: escribe el heartbeat (contador), el comando Habilitar del
   dashboard y los ángulos de la ESP32. Por defecto NO escribe nada en el PLC.

El heartbeat solo avanza mientras la Raspberry está conectada al PLC y a MQTT. Si se
detiene, el PLC (con el cambio SCL de LEEME_PLC.md) pone RaspberryOnline = FALSE.

Configuración: variables de entorno o el archivo plc.env junto a este script
(ver plc.env.example). Uso:  ~/solartrack/venv/bin/python plc_mqtt.py
"""

import hmac
import json
import logging
import os
import struct
import threading
import time

log = logging.getLogger("plc_mqtt")

# --- Mapa del DB1 (FB_SolarTrack, acceso no optimizado) ---
BYTES_LECTURA = 11            # DB1.DBB0 .. DB1.DBB10
BYTE_HABILITAR = 0            # DB1.DBX0.0
BYTE_ANGULO_VERTICAL = 2      # DB1.DBD2 REAL
BYTE_ANGULO_HORIZONTAL = 6    # DB1.DBD6 REAL
BYTE_ESTADOS = 10             # DBX10.0 Bloqueo, DBX10.1 Permiso, DBX10.2 RaspberryOnline


def cargar_env(ruta):
    """Carga KEY=valor de un archivo .env sin pisar variables ya definidas (p. ej. por systemd)."""
    if not os.path.exists(ruta):
        return
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                clave, valor = linea.split("=", 1)
                os.environ.setdefault(clave.strip(), valor.strip().strip('"').strip("'"))


def leer_config(env=os.environ):
    si = lambda clave, defecto="false": env.get(clave, defecto).strip().lower() in ("1", "true", "si", "sí", "yes")
    return {
        "plc_ip": env.get("PLC_IP", "10.41.32.1"),
        "plc_rack": int(env.get("PLC_RACK", "0")),
        "plc_slot": int(env.get("PLC_SLOT", "1")),
        "plc_db": int(env.get("PLC_DB", "1")),
        "escritura": si("PLC_ESCRITURA"),
        "byte_heartbeat": int(env.get("PLC_BYTE_HEARTBEAT", "12")),
        "escribir_angulos": si("PLC_ESCRIBIR_ANGULOS"),
        "ciclo": float(env.get("PLC_CICLO", "1")),
        "publicar_cada": float(env.get("PUBLICAR_CADA", "5")),
        "mqtt_host": env.get("MQTT_BROKER_HOST", "localhost"),
        "mqtt_port": int(env.get("MQTT_BROKER_PORT", "1883")),
        "mqtt_user": env.get("MQTT_USERNAME", ""),
        "mqtt_password": env.get("MQTT_PASSWORD", ""),
        "prefijo": env.get("MQTT_TOPIC_PREFIX", "ecs"),
        "plc_token": env.get("PLC_TOKEN", ""),
        "raspberry_token": env.get("RASPBERRY_TOKEN", ""),
        "topic_permiso": env.get("TOPIC_PERMISO", "lina/plc/permiso"),
        "topic_estado": env.get("TOPIC_ESTADO", "solartrack/plc/estado"),
    }


def decodificar(datos):
    """Bytes 0..10 del DB1 -> dict (mismo formato que plc_mqtt.py original)."""
    real = lambda i: struct.unpack(">f", bytes(datos[i:i + 4]))[0]
    return {
        "habilitar": bool(datos[BYTE_HABILITAR] & 0x01),
        "angulo_vertical": round(real(BYTE_ANGULO_VERTICAL), 2),
        "angulo_horizontal": round(real(BYTE_ANGULO_HORIZONTAL), 2),
        "bloqueo": bool(datos[BYTE_ESTADOS] & 0x01),
        "permiso_movimiento": bool(datos[BYTE_ESTADOS] & 0x02),
        "raspberry_online": bool(datos[BYTE_ESTADOS] & 0x04),
    }


# Variable Django (dispositivo "plc") <- clave del estado
VARIABLES_DJANGO = {
    "plc_conectado": "plc_conectado",
    "plc_habilitar": "habilitar",
    "plc_bloqueo": "bloqueo",
    "plc_permiso": "permiso_movimiento",
    "plc_raspberry_online": "raspberry_online",
    "plc_angulo_vertical": "angulo_vertical",
    "plc_angulo_horizontal": "angulo_horizontal",
    "plc_escritura": "escritura_habilitada",
}

# Telemetría de la ESP32 (topics existentes) -> byte del REAL en DB1
ANGULOS_ESP32 = {
    "lina/esp32/zenith": BYTE_ANGULO_VERTICAL,
    "lina/esp32/azimuth": BYTE_ANGULO_HORIZONTAL,
}


class Puente:
    """Lógica del puente. plc (snap7.client.Client) y mqtt (paho Client) se inyectan para poder probarla."""

    def __init__(self, cfg, plc, mqtt, reloj=time.monotonic):
        self.cfg, self.plc, self.mqtt, self.reloj = cfg, plc, mqtt, reloj
        self.mqtt_conectado = False
        self.heartbeat = 0
        self.ultimo_django = {}
        self.ultimo_envio_django = None
        self.proximo_intento_plc = 0.0
        self.espera_plc = 1.0
        self.error_escritura = None
        self._pendientes = {}  # escrituras pedidas por MQTT; se aplican en el hilo principal (snap7 no es thread-safe)
        self._lock = threading.Lock()
        self.topic_habilitar = f"{cfg['prefijo']}/plc/plc_habilitar/set"

    # ---------- MQTT (hilo de paho) ----------
    def suscripciones(self):
        return [self.topic_habilitar] + (list(ANGULOS_ESP32) if self.cfg["escribir_angulos"] else [])

    def al_conectar_mqtt(self, rc):
        self.mqtt_conectado = rc == 0
        if self.mqtt_conectado:
            for topic in self.suscripciones():
                self.mqtt.subscribe(topic, qos=1)
            log.info("MQTT conectado a %s:%s", self.cfg["mqtt_host"], self.cfg["mqtt_port"])
        else:
            log.error("MQTT rechazó la conexión (rc=%s). Revisa MQTT_USERNAME/MQTT_PASSWORD.", rc)

    def al_desconectar_mqtt(self):
        self.mqtt_conectado = False
        log.warning("MQTT desconectado; el heartbeat se detiene hasta reconectar.")

    def al_recibir(self, topic, payload):
        if topic == self.topic_habilitar:
            self._comando_habilitar(payload)
        elif topic in ANGULOS_ESP32:
            try:
                valor = float(payload.decode("utf-8").strip())
            except (UnicodeDecodeError, ValueError):
                return
            with self._lock:
                self._pendientes[ANGULOS_ESP32[topic]] = ("real", valor)

    def _comando_habilitar(self, payload):
        try:
            datos = json.loads(payload.decode("utf-8"))
            valor = float(datos["value"])
            token = str(datos.get("token", ""))
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            log.warning("Comando Habilitar ignorado: JSON inválido")
            return
        if not self.cfg["plc_token"] or not hmac.compare_digest(token, self.cfg["plc_token"]):
            log.warning("Comando Habilitar ignorado: token inválido")
            return
        if valor not in (0.0, 1.0):
            log.warning("Comando Habilitar ignorado: valor %s (solo 0 o 1)", valor)
            return
        if not self.cfg["escritura"]:
            log.warning("Comando Habilitar=%d recibido pero PLC_ESCRITURA=false: NO se escribe en el PLC.", valor)
            return
        with self._lock:
            self._pendientes[BYTE_HABILITAR] = ("bool", valor == 1.0)
        log.info("Comando Habilitar=%d aceptado; se escribe en el próximo ciclo.", valor)

    # ---------- PLC (hilo principal) ----------
    def _asegurar_plc(self):
        if self.plc.get_connected():
            return True
        ahora = self.reloj()
        if ahora < self.proximo_intento_plc:
            return False
        try:
            self.plc.connect(self.cfg["plc_ip"], self.cfg["plc_rack"], self.cfg["plc_slot"])
        except Exception as exc:
            self.proximo_intento_plc = ahora + self.espera_plc
            log.error("No se pudo conectar al PLC %s: %s (reintento en %.0f s)", self.cfg["plc_ip"], exc, self.espera_plc)
            self.espera_plc = min(self.espera_plc * 2, 30)
            return False
        self.espera_plc = 1.0
        log.info("PLC conectado en %s (rack %s, slot %s)", self.cfg["plc_ip"], self.cfg["plc_rack"], self.cfg["plc_slot"])
        return True

    def _leer_plc(self):
        if not self._asegurar_plc():
            return None
        try:
            return decodificar(self.plc.db_read(self.cfg["plc_db"], 0, BYTES_LECTURA))
        except Exception as exc:
            log.error("Error leyendo DB%s: %s. Se reconectará.", self.cfg["plc_db"], exc)
            self._desconectar_plc()
            return None

    def _desconectar_plc(self):
        try:
            self.plc.disconnect()
        except Exception:
            pass

    def _escribir(self):
        """Heartbeat + escrituras pendientes. Solo con PLC_ESCRITURA=true y MQTT conectado."""
        if not self.cfg["escritura"] or not self.mqtt_conectado:
            return
        db = self.cfg["plc_db"]
        with self._lock:
            pendientes, self._pendientes = self._pendientes, {}
        try:
            self.heartbeat = (self.heartbeat + 1) % 32768
            self.plc.db_write(db, self.cfg["byte_heartbeat"], struct.pack(">h", self.heartbeat))
            for byte, (tipo, valor) in pendientes.items():
                if tipo == "bool":  # Habilitar es el único bit del byte 0
                    actual = self.plc.db_read(db, byte, 1)[0]
                    nuevo = actual | 0x01 if valor else actual & ~0x01
                    self.plc.db_write(db, byte, bytearray([nuevo]))
                    log.info("Escrito Habilitar=%s en DB%s.DBX%s.0", valor, db, byte)
                else:
                    self.plc.db_write(db, byte, struct.pack(">f", valor))
            if self.error_escritura:
                log.info("Escritura en el PLC recuperada.")
            self.error_escritura = None
        except Exception as exc:
            if str(exc) != self.error_escritura:  # no repetir el mismo error cada segundo
                log.error("Error escribiendo en el PLC (¿existe DB%s.DBW%s HeartbeatRpi?): %s",
                          db, self.cfg["byte_heartbeat"], exc)
            self.error_escritura = str(exc)

    # ---------- ciclo ----------
    def ciclo(self):
        estado = self._leer_plc()
        if estado is None:
            estado = {"plc_conectado": False, "permiso_movimiento": False}
        else:
            estado["plc_conectado"] = True
            self._escribir()
        estado["escritura_habilitada"] = self.cfg["escritura"]
        permiso = estado["plc_conectado"] and estado["permiso_movimiento"]
        self.mqtt.publish(self.cfg["topic_permiso"], "1" if permiso else "0", qos=0, retain=False)
        self.mqtt.publish(self.cfg["topic_estado"], json.dumps(estado), qos=1, retain=False)
        self._publicar_django(estado)
        return estado

    def _publicar_django(self, estado):
        if not self.cfg["plc_token"]:
            return
        ahora = self.reloj()
        periodico = self.ultimo_envio_django is None or ahora - self.ultimo_envio_django >= self.cfg["publicar_cada"]
        prefijo = self.cfg["prefijo"]
        for variable, clave in VARIABLES_DJANGO.items():
            if clave not in estado:
                continue
            valor = float(estado[clave])
            if periodico or self.ultimo_django.get(variable) != valor:
                self.mqtt.publish(f"{prefijo}/plc/{variable}",
                                  json.dumps({"value": valor, "token": self.cfg["plc_token"]}), qos=1)
                self.ultimo_django[variable] = valor
        if periodico:
            self.ultimo_envio_django = ahora
            if self.cfg["raspberry_token"]:  # mantiene "Raspberry Pi" conectada en el dashboard
                self.mqtt.publish(f"{prefijo}/raspberry/rpi_servicio_plc",
                                  json.dumps({"value": 1, "token": self.cfg["raspberry_token"]}), qos=1)

    def detener(self):
        self.mqtt.publish(self.cfg["topic_permiso"], "0", qos=1, retain=False)
        self.mqtt.publish(self.cfg["topic_estado"],
                          json.dumps({"plc_conectado": False, "permiso_movimiento": False}), qos=1, retain=False)
        self._desconectar_plc()


def crear_cliente_mqtt(cfg, puente):
    import paho.mqtt.client as mqtt

    try:  # paho-mqtt 2.x (el de la Raspberry)
        cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="raspberry-plc")
    except AttributeError:  # paho-mqtt 1.x
        cliente = mqtt.Client(client_id="raspberry-plc")
    if cfg["mqtt_user"]:
        cliente.username_pw_set(cfg["mqtt_user"], cfg["mqtt_password"])
    # Si la Raspberry se cae sin avisar, el broker publica el bloqueo por ella.
    cliente.will_set(cfg["topic_permiso"], "0", qos=1, retain=False)
    cliente.on_connect = lambda c, u, flags, rc, *props: puente.al_conectar_mqtt(rc)
    cliente.on_disconnect = lambda c, u, *args: puente.al_desconectar_mqtt()
    cliente.on_message = lambda c, u, msg: puente.al_recibir(msg.topic, msg.payload)
    cliente.reconnect_delay_set(min_delay=1, max_delay=30)
    return cliente


def main():
    cargar_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), "plc.env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = leer_config()
    import snap7

    if not cfg["plc_token"]:
        log.warning("Sin PLC_TOKEN: no se envía el estado a Django (ejecuta preparar_plc y copia el token).")
    log.warning("Escrituras al PLC: %s", "HABILITADAS" if cfg["escritura"] else "DESHABILITADAS (solo lectura)")

    puente = Puente(cfg, snap7.client.Client(), None)
    puente.mqtt = crear_cliente_mqtt(cfg, puente)
    puente.mqtt.connect_async(cfg["mqtt_host"], cfg["mqtt_port"], keepalive=30)
    puente.mqtt.loop_start()  # reconecta solo si el broker se cae
    try:
        while True:
            inicio = time.monotonic()
            estado = puente.ciclo()
            log.debug("%s", estado)
            time.sleep(max(0.0, cfg["ciclo"] - (time.monotonic() - inicio)))
    except KeyboardInterrupt:
        log.info("Detenido por el usuario")
    finally:
        puente.detener()
        time.sleep(0.5)  # deja salir los últimos mensajes
        puente.mqtt.loop_stop()
        puente.mqtt.disconnect()


if __name__ == "__main__":
    main()
