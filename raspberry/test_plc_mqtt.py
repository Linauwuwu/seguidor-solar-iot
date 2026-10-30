"""Pruebas sin hardware del puente PLC <-> MQTT:  python -m unittest test_plc_mqtt -v

FakePLC imita el DB1 y, si se pide, la lógica SCL propuesta (heartbeat con timeout de 3 s).
Es una SIMULACIÓN para probar el diseño; no reemplaza la prueba con el PLC real.
"""

import json
import struct
import unittest

from plc_mqtt import Puente, cargar_env, decodificar, leer_config


class FakePLC:
    def __init__(self, scl=False):
        self.db = bytearray(16)
        self.conectado = False
        self.falla_conexion = False
        self.falla_lectura = False
        self.escrituras = []
        self.scl = scl
        self.hb_anterior, self.ultimo_cambio = 0, None
        self.t = 0.0

    def get_connected(self):
        return self.conectado

    def connect(self, ip, rack, slot):
        if self.falla_conexion:
            raise RuntimeError("TCP : Unreachable peer")
        self.conectado = True

    def disconnect(self):
        self.conectado = False

    def db_read(self, db, start, size):
        if self.falla_lectura:
            raise RuntimeError("ISO : An error occurred during recv TCP")
        if self.scl:
            self._scan()
        return bytearray(self.db[start:start + size])

    def db_write(self, db, start, data):
        self.escrituras.append((start, bytes(data)))
        self.db[start:start + len(data)] = data

    def set_bit(self, byte, bit, valor):
        self.db[byte] = self.db[byte] | (1 << bit) if valor else self.db[byte] & ~(1 << bit)

    def _scan(self):
        """Lógica SCL propuesta en LEEME_PLC.md (TOF de 3 s re-disparado por cada cambio del heartbeat)."""
        hb = struct.unpack(">h", bytes(self.db[12:14]))[0]
        if hb != self.hb_anterior:
            self.hb_anterior, self.ultimo_cambio = hb, self.t
        online = self.ultimo_cambio is not None and self.t - self.ultimo_cambio < 3.0
        self.set_bit(10, 2, online)
        if not online:
            self.set_bit(0, 0, False)  # Habilitar se cae: hay que volver a habilitar a mano
        permiso = bool(self.db[0] & 1) and not bool(self.db[10] & 1) and online
        self.set_bit(10, 1, permiso)


class FakeMQTT:
    def __init__(self):
        self.publicados = []
        self.suscritos = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.publicados.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.suscritos.append(topic)

    def de(self, topic):
        return [p for t, p, _ in self.publicados if t == topic]


def cfg(**cambios):
    c = leer_config({"PLC_TOKEN": "tok-plc", "RASPBERRY_TOKEN": "tok-rpi"})
    c.update(cambios)
    return c


class Reloj:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def comando(valor, token="tok-plc"):
    return json.dumps({"value": valor, "token": token}).encode()


class PruebasPuente(unittest.TestCase):
    def setUp(self):
        self.plc, self.mqtt, self.reloj = FakePLC(), FakeMQTT(), Reloj()

    def puente(self, **cambios):
        p = Puente(cfg(**cambios), self.plc, self.mqtt, self.reloj)
        p.al_conectar_mqtt(0)
        return p

    def test_decodifica_db1_igual_que_leer_plc(self):
        datos = bytearray(11)
        datos[0] = 0x01
        datos[2:6] = struct.pack(">f", 45.5)
        datos[6:10] = struct.pack(">f", 120.0)
        datos[10] = 0b110
        self.assertEqual(decodificar(datos), {
            "habilitar": True, "angulo_vertical": 45.5, "angulo_horizontal": 120.0,
            "bloqueo": False, "permiso_movimiento": True, "raspberry_online": True,
        })

    def test_permiso_del_plc_llega_a_la_esp32(self):
        p = self.puente()
        p.ciclo()
        self.plc.set_bit(10, 1, True)
        p.ciclo()
        self.assertEqual(self.mqtt.de("lina/plc/permiso"), ["0", "1"])

    def test_plc_caido_bloquea_y_reintenta(self):
        self.plc.falla_conexion = True
        p = self.puente()
        estado = p.ciclo()
        self.assertFalse(estado["plc_conectado"])
        self.assertEqual(self.mqtt.de("lina/plc/permiso"), ["0"])
        self.plc.falla_conexion = False
        p.ciclo()  # aún dentro de la espera de reintento
        self.assertFalse(self.plc.conectado)
        self.reloj.t = 1.5
        self.assertTrue(p.ciclo()["plc_conectado"])

    def test_error_de_lectura_con_permiso_activo_publica_0(self):
        p = self.puente()
        self.plc.set_bit(10, 1, True)
        p.ciclo()
        self.plc.falla_lectura = True
        p.ciclo()
        self.assertEqual(self.mqtt.de("lina/plc/permiso"), ["1", "0"])
        self.assertFalse(self.plc.conectado)

    def test_sin_escritura_no_toca_el_plc(self):
        p = self.puente()
        p.al_recibir("ecs/plc/plc_habilitar/set", comando(1))
        for _ in range(3):
            p.ciclo()
        self.assertEqual(self.plc.escrituras, [])
        self.assertEqual(self.mqtt.suscritos, ["ecs/plc/plc_habilitar/set"])

    def test_con_escritura_heartbeat_y_habilitar(self):
        p = self.puente(escritura=True)
        p.ciclo()
        p.al_recibir("ecs/plc/plc_habilitar/set", comando(1))
        p.ciclo()
        self.assertEqual(self.plc.escrituras[0], (12, struct.pack(">h", 1)))
        self.assertIn((12, struct.pack(">h", 2)), self.plc.escrituras)
        self.assertTrue(self.plc.db[0] & 1)
        p.al_recibir("ecs/plc/plc_habilitar/set", comando(0))
        p.ciclo()
        self.assertFalse(self.plc.db[0] & 1)

    def test_comando_con_token_invalido_o_valor_raro_se_ignora(self):
        p = self.puente(escritura=True)
        p.al_recibir("ecs/plc/plc_habilitar/set", comando(1, token="otro"))
        p.al_recibir("ecs/plc/plc_habilitar/set", comando(5))
        p.al_recibir("ecs/plc/plc_habilitar/set", b"no-json")
        p.ciclo()
        self.assertFalse(self.plc.db[0] & 1)

    def test_heartbeat_se_detiene_sin_mqtt(self):
        p = self.puente(escritura=True)
        p.al_desconectar_mqtt()
        p.ciclo()
        self.assertEqual(self.plc.escrituras, [])

    def test_angulos_esp32_solo_si_se_habilita(self):
        p = self.puente(escritura=True, escribir_angulos=True)
        self.assertIn("lina/esp32/azimuth", self.mqtt.suscritos)
        p.al_recibir("lina/esp32/azimuth", b"120")
        p.al_recibir("lina/esp32/zenith", b"45")
        estado = p.ciclo()
        estado = p.ciclo()
        self.assertEqual((estado["angulo_horizontal"], estado["angulo_vertical"]), (120.0, 45.0))

    def test_django_solo_cambios_y_cada_5_s(self):
        p = self.puente()
        p.ciclo()
        primeros = len(self.mqtt.de("ecs/plc/plc_permiso"))
        p.ciclo()
        self.assertEqual(len(self.mqtt.de("ecs/plc/plc_permiso")), primeros)  # sin cambios: no repite
        self.plc.set_bit(10, 1, True)
        p.ciclo()
        self.assertEqual(len(self.mqtt.de("ecs/plc/plc_permiso")), primeros + 1)  # cambio: publica
        self.reloj.t = 5
        p.ciclo()
        self.assertEqual(len(self.mqtt.de("ecs/plc/plc_permiso")), primeros + 2)  # periódico
        msg = json.loads(self.mqtt.de("ecs/plc/plc_permiso")[-1])
        self.assertEqual(msg, {"value": 1.0, "token": "tok-plc"})
        self.assertTrue(self.mqtt.de("ecs/raspberry/rpi_servicio_plc"))

    def test_detener_publica_bloqueo(self):
        p = self.puente()
        p.detener()
        self.assertEqual(self.mqtt.de("lina/plc/permiso"), ["0"])


class PruebasLogicaSCL(unittest.TestCase):
    """Diseño completo con el PLC simulado: heartbeat -> RaspberryOnline -> Permiso."""

    def correr(self, p, plc, reloj, desde, hasta):
        for s in range(desde, hasta):
            plc.t = reloj.t = float(s)
            p.ciclo()

    def test_permiso_solo_con_heartbeat_vivo(self):
        plc, mqtt, reloj = FakePLC(scl=True), FakeMQTT(), Reloj()
        p = Puente(cfg(escritura=True), plc, mqtt, reloj)
        p.al_conectar_mqtt(0)
        self.correr(p, plc, reloj, 0, 2)
        p.al_recibir("ecs/plc/plc_habilitar/set", comando(1))  # operador habilita desde el dashboard
        self.correr(p, plc, reloj, 2, 4)
        self.assertEqual(mqtt.de("lina/plc/permiso")[-1], "1")
        p.al_desconectar_mqtt()  # se cae MQTT: el heartbeat se congela
        self.correr(p, plc, reloj, 4, 9)
        self.assertFalse(plc.db[10] & 0b100, "RaspberryOnline debe caer a FALSE tras 3 s sin heartbeat")
        self.assertFalse(plc.db[0] & 1, "Habilitar debe caer con la comunicación")
        self.assertEqual(mqtt.de("lina/plc/permiso")[-1], "0")
        p.al_conectar_mqtt(0)  # vuelve MQTT: no recupera el permiso solo
        self.correr(p, plc, reloj, 9, 12)
        self.assertEqual(mqtt.de("lina/plc/permiso")[-1], "0")

    def test_plc_recien_encendido_sin_heartbeat_no_da_permiso(self):
        plc, mqtt, reloj = FakePLC(scl=True), FakeMQTT(), Reloj()
        p = Puente(cfg(), plc, mqtt, reloj)  # solo lectura: no hay heartbeat
        p.al_conectar_mqtt(0)
        plc.set_bit(0, 0, True)
        self.correr(p, plc, reloj, 0, 5)
        self.assertEqual(set(mqtt.de("lina/plc/permiso")), {"0"})

    def test_bloqueo_anula_permiso(self):
        plc, mqtt, reloj = FakePLC(scl=True), FakeMQTT(), Reloj()
        p = Puente(cfg(escritura=True), plc, mqtt, reloj)
        p.al_conectar_mqtt(0)
        plc.set_bit(10, 0, True)  # BloqueoMovimiento
        self.correr(p, plc, reloj, 0, 2)
        p.al_recibir("ecs/plc/plc_habilitar/set", comando(1))
        self.correr(p, plc, reloj, 2, 4)
        self.assertTrue(plc.db[0] & 1)
        self.assertEqual(mqtt.de("lina/plc/permiso")[-1], "0")


class PruebasConfig(unittest.TestCase):
    def test_escritura_deshabilitada_por_defecto(self):
        self.assertFalse(leer_config({})["escritura"])
        self.assertTrue(leer_config({"PLC_ESCRITURA": "true"})["escritura"])

    def test_cargar_env_no_pisa_variables(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8") as f:
            f.write("# comentario\nPRUEBA_PLC_A=1\nPRUEBA_PLC_B='dos'\n")
        os.environ["PRUEBA_PLC_A"] = "systemd"
        try:
            cargar_env(f.name)
            self.assertEqual(os.environ["PRUEBA_PLC_A"], "systemd")
            self.assertEqual(os.environ["PRUEBA_PLC_B"], "dos")
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
