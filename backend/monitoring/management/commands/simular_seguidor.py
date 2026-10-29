"""Simula el sistema completo del seguidor solar sin hardware.

Una luz imaginaria (el "sol") recorre el cielo de Este a Oeste en un ciclo
corto. Los 4 LDR de la ESP32 leen esa luz según hacia dónde apunta el panel,
la ESP32 corrige sus servos para igualar los LDR (seguimiento real en lazo
cerrado), el PLC informa ángulos, motores y energía generada, y la Raspberry
Pi actúa como gateway. Todo se guarda en la BD igual que si llegara por MQTT.

Con --esp32-real (seguidor real conectado) solo se simula el PLC, que toma
los ángulos de los servos y los LDR de las lecturas reales de la ESP32.
"""

import math
import random
import time
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from monitoring.models import Device, Sensor, SensorReading, Widget

ESP32_SLUG = "seguidor-solar-iot"
REAL_MAX_AGE = timedelta(seconds=15)  # misma ventana que el indicador "Datos recientes"
SIM_TAG = "simulado"  # sensor_command guarda los comandos de estos dispositivos en la BD en vez de MQTT

# slug -> (nombre, {variable: (unidad, widget, min, max)})
DEVICES = {
    ESP32_SLUG: (
        "ESP32 Seguidor Solar (simulado)",
        {
            "light_left": ("", Widget.TYPE_LINE_CHART, 0, 4095),
            "light_right": ("", Widget.TYPE_LINE_CHART, 0, 4095),
            "light_up": ("", Widget.TYPE_LINE_CHART, 0, 4095),
            "light_down": ("", Widget.TYPE_LINE_CHART, 0, 4095),
            "azimuth": ("°", Widget.TYPE_METER, 0, 180),
            "zenith": ("°", Widget.TYPE_METER, 0, 180),
            "solar_azimuth": ("°", Widget.TYPE_NUMERIC, 0, 360),
            "solar_elevation": ("°", Widget.TYPE_NUMERIC, -90, 90),
            "mode": ("", Widget.TYPE_BUTTON, 0, 1),
            "servo": ("°", Widget.TYPE_SLIDER, 0, 180),
            "servo2": ("°", Widget.TYPE_SLIDER, 0, 180),
        },
    ),
    "plc": (
        "PLC Seguidor Solar (simulado)",
        {
            "plc_inclinacion": ("°", Widget.TYPE_METER, 0, 90),
            "plc_azimut_panel": ("°", Widget.TYPE_METER, 0, 360),
            "plc_motor_azimut": ("", Widget.TYPE_LED, 0, 1),
            "plc_motor_elevacion": ("", Widget.TYPE_LED, 0, 1),
            "plc_voltaje_panel": ("V", Widget.TYPE_LINE_CHART, 0, 40),
            "plc_corriente_panel": ("A", Widget.TYPE_LINE_CHART, 0, 10),
            "plc_potencia": ("W", Widget.TYPE_LINE_CHART, 0, 350),
            "plc_temperatura_panel": ("°C", Widget.TYPE_NUMERIC, 0, 80),
        },
    ),
    "raspberry": (
        "Raspberry Pi Gateway (simulado)",
        {
            "rpi_temp_cpu": ("°C", Widget.TYPE_NUMERIC, 0, 90),
            "rpi_uso_cpu": ("%", Widget.TYPE_METER, 0, 100),
            "rpi_energia_generada": ("Wh", Widget.TYPE_NUMERIC, 0, 10000),
            "rpi_lecturas_reenviadas": ("", Widget.TYPE_NUMERIC, 0, 1e9),
        },
    ),
}

LDR_MAX = 4095
LDR_GAIN = 0.015  # cuánto cambia cada LDR por grado de error de apuntamiento
TRACK_DEADBAND = 40  # diferencia mínima entre LDR para mover un servo
MAX_STEP = 3.0  # grados por ciclo que puede girar un servo


def light_position(t, cycle):
    """Luz imaginaria como (ángulo de recorrido 0=Este..180=Oeste, elevación); 80% del ciclo es 'día'."""
    p = (t % cycle) / cycle
    if p < 0.8:
        d = p / 0.8
        return 180 * d, 65 * math.sin(math.pi * d)
    return 180 - 180 * (p - 0.8) / 0.2, -10.0  # de noche vuelve bajo el horizonte hacia el Este


def compass_azimuth(track_angle):
    """En Chile (hemisferio sur) el sol pasa por el Norte: 0=Este(90°), 90=Norte(0°), 180=Oeste(270°)."""
    return (90 - track_angle) % 360


def ldr_readings(sun_track, sun_el, servo_az, servo_zen):
    """Los LDR ven más luz del lado hacia donde está la luz respecto al panel."""
    intensity = 3800 * math.sin(math.radians(sun_el)) + 150 if sun_el > 0 else 150
    err_az = sun_track - servo_az
    err_el = sun_el - (90 - servo_zen)

    def ldr(sign, err):
        noisy = intensity * (1 + sign * LDR_GAIN * err) + random.uniform(-15, 15)
        return round(max(0.0, min(LDR_MAX, noisy)))

    return {
        "light_left": ldr(-1, err_az),
        "light_right": ldr(+1, err_az),
        "light_up": ldr(+1, err_el),
        "light_down": ldr(-1, err_el),
    }


def _step(diff):
    if abs(diff) < TRACK_DEADBAND:
        return 0.0
    return max(-MAX_STEP, min(MAX_STEP, diff / 100))


def track(ldr, servo_az, servo_zen):
    """Lógica de la ESP32: girar hacia el LDR más iluminado. De noche vuelve al Este."""
    if max(ldr.values()) < 300:
        return max(0.0, servo_az - MAX_STEP), max(0.0, servo_zen - MAX_STEP)
    servo_az += _step(ldr["light_right"] - ldr["light_left"])
    servo_zen -= _step(ldr["light_up"] - ldr["light_down"])  # más luz arriba = subir el panel
    return max(0.0, min(180.0, servo_az)), max(0.0, min(90.0, servo_zen))


def toward(current, target):
    return current + max(-MAX_STEP, min(MAX_STEP, target - current))


def real_esp32_state():
    """Últimos valores de la ESP32 real, o None si no ha enviado datos en los últimos 15 s."""
    device = Device.objects.filter(slug=ESP32_SLUG).first()
    if device is None or device.last_seen_at is None or timezone.now() - device.last_seen_at > REAL_MAX_AGE:
        return None
    return {s.variable: s.readings.values_list("value", flat=True).first() for s in device.sensors.all()}


def light_from_ldr(ldr):
    """(sol 0..1, apuntamiento 0..1) estimados desde los LDR: si están parejos, el panel apunta a la luz."""
    avg = sum(ldr.values()) / 4
    if avg <= 0:
        return 0.0, 0.0
    imbalance = abs(ldr["light_left"] - ldr["light_right"]) + abs(ldr["light_up"] - ldr["light_down"])
    return min(1.0, avg / LDR_MAX), max(0.0, 1 - imbalance / (2 * avg))


class Command(BaseCommand):
    help = (
        "Simula ESP32 + PLC + Raspberry Pi siguiendo una luz imaginaria. "
        "Con --esp32-real solo simula el PLC, a partir de los datos de la ESP32 real."
    )

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=float, default=1.0, help="Segundos entre lecturas.")
        parser.add_argument("--ciclo", type=float, default=180.0, help="Segundos que dura un 'día' completo.")
        parser.add_argument(
            "--esp32-real",
            action="store_true",
            help="Seguidor real conectado: solo se simula el PLC, que lee ángulos y LDR de la ESP32 real.",
        )

    def handle(self, *args, interval, ciclo, esp32_real, **options):
        owner = User.objects.filter(is_superuser=True).order_by("id").first()
        if owner is None:
            self.stderr.write("Crea primero un superusuario: python manage.py createsuperuser")
            return

        sensors = {}
        for slug, (name, variables) in DEVICES.items():
            if esp32_real and slug != "plc":
                continue
            device, _ = Device.objects.get_or_create(slug=slug, defaults={"name": name, "owner": owner, "tag": SIM_TAG})
            for variable, (unit, widget_type, vmin, vmax) in variables.items():
                sensor, created = Sensor.objects.get_or_create(device=device, variable=variable, defaults={"unit": unit})
                if created:
                    Widget.objects.update_or_create(
                        sensor=sensor,
                        defaults={"widget_type": widget_type, "min_value": vmin, "max_value": vmax, "slider_step": 1},
                    )
                sensors[variable] = sensor

        mode_sensor = sensors.get("mode")
        if mode_sensor and not mode_sensor.readings.exists():
            SensorReading.objects.create(sensor=mode_sensor, value=1, timestamp=timezone.now())

        if esp32_real:
            self.stdout.write(self.style.SUCCESS(
                f"Solo PLC simulado, leyendo la ESP32 real '{ESP32_SLUG}' cada {interval}s. Ctrl+C para detener."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Simulación activa: luz imaginaria con ciclo de {ciclo:.0f}s, lecturas cada {interval}s. Ctrl+C para detener."
            ))

        servo_az, servo_zen = 0.0, 0.0
        energy_wh, forwarded = 0.0, 0
        start = time.monotonic()
        try:
            while True:
                now = timezone.now()
                prev_az, prev_zen = servo_az, servo_zen
                esp32_values = {}
                if esp32_real:
                    real = real_esp32_state()
                    if real is None:  # ESP32 sin datos recientes: el PLC informa motores detenidos y sin generación
                        sunlight, pointing = 0.0, 0.0
                    else:
                        servo_az = real["azimuth"] if real.get("azimuth") is not None else servo_az
                        servo_zen = real["zenith"] if real.get("zenith") is not None else servo_zen
                        ldr = {k: real.get(k) or 0 for k in ("light_left", "light_right", "light_up", "light_down")}
                        sunlight, pointing = light_from_ldr(ldr)
                else:
                    sun_track, sun_el = light_position(time.monotonic() - start, ciclo)
                    ldr = ldr_readings(sun_track, sun_el, servo_az, servo_zen)
                    servo_az, servo_zen = self._move_servos(ldr, servo_az, servo_zen, mode_sensor, sensors)
                    if sun_el > 0:
                        sunlight = math.sin(math.radians(sun_el))
                        error = math.hypot(sun_track - servo_az, sun_el - (90 - servo_zen))
                        pointing = math.cos(math.radians(min(90, error)))
                    else:
                        sunlight, pointing = 0.0, 0.0
                    esp32_values = {
                        **ldr,
                        "azimuth": servo_az,
                        "zenith": servo_zen,
                        "solar_azimuth": compass_azimuth(sun_track),
                        "solar_elevation": sun_el,
                    }

                voltage = 36.5 * min(1, 4 * sunlight) * pointing
                current = 8.2 * sunlight * pointing
                power = voltage * current
                energy_wh += power * interval / 3600

                values = {
                    **esp32_values,
                    "plc_inclinacion": servo_zen,
                    "plc_azimut_panel": compass_azimuth(servo_az),
                    "plc_motor_azimut": float(abs(servo_az - prev_az) > 0.1),
                    "plc_motor_elevacion": float(abs(servo_zen - prev_zen) > 0.1),
                    "plc_voltaje_panel": voltage,
                    "plc_corriente_panel": current,
                    "plc_potencia": power,
                    "plc_temperatura_panel": 18 + 30 * sunlight + random.uniform(-0.3, 0.3),
                    "rpi_temp_cpu": 47 + random.uniform(-2, 2),
                    "rpi_uso_cpu": random.uniform(12, 30),
                    "rpi_energia_generada": energy_wh,
                    "rpi_lecturas_reenviadas": forwarded,
                }
                readings = [
                    SensorReading(sensor=sensors[var], value=round(val, 2), timestamp=now)
                    for var, val in values.items()
                    if var in sensors
                ]
                SensorReading.objects.bulk_create(readings)
                forwarded += len(readings)
                Device.objects.filter(pk__in={r.sensor.device_id for r in readings}).update(last_seen_at=now)
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("Simulación detenida.")

    @staticmethod
    def _move_servos(ldr, servo_az, servo_zen, mode_sensor, sensors):
        """ESP32 simulada: en automático sigue la luz; en manual va al ángulo pedido desde el dashboard."""
        if mode_sensor:
            last = lambda var: sensors[var].readings.values_list("value", flat=True).first()
            if last("mode") == 0:
                target_az, target_zen = last("servo"), last("servo2")
                return (
                    toward(servo_az, servo_az if target_az is None else target_az),
                    toward(servo_zen, servo_zen if target_zen is None else target_zen),
                )
        return track(ldr, servo_az, servo_zen)
