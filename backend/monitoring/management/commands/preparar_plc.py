"""Registra el PLC Siemens REAL (vía la Raspberry) como dispositivo "plc" y muestra su token.

Reemplaza al PLC simulado de `simular_seguidor`: reutiliza el mismo slug "plc", le quita la
marca "simulado" (así los comandos van por MQTT) y desactiva, sin borrarlos, los sensores
simulados. Se puede ejecutar varias veces.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from monitoring.models import Device, Sensor, Widget

PLC_SLUG = "plc"
PLC_NAME = "PLC Siemens S7-1200"

# variable -> (unidad, widget, min, max); las publica raspberry/plc_mqtt.py
VARIABLES = {
    "plc_conectado": ("", Widget.TYPE_LED, 0, 1),
    "plc_habilitar": ("", Widget.TYPE_BUTTON, 0, 1),
    "plc_bloqueo": ("", Widget.TYPE_LED, 0, 1),
    "plc_permiso": ("", Widget.TYPE_LED, 0, 1),
    "plc_raspberry_online": ("", Widget.TYPE_LED, 0, 1),
    "plc_angulo_vertical": ("°", Widget.TYPE_METER, 0, 180),
    "plc_angulo_horizontal": ("°", Widget.TYPE_METER, 0, 180),
    "plc_escritura": ("", Widget.TYPE_LED, 0, 1),
}


class Command(BaseCommand):
    help = "Crea o actualiza el dispositivo 'plc' real con sus sensores y muestra el token para plc.env."

    def handle(self, *args, **options):
        owner = User.objects.filter(is_superuser=True).order_by("id").first()
        if owner is None:
            self.stderr.write("Crea primero un superusuario (admin).")
            return

        device, created = Device.objects.get_or_create(
            slug=PLC_SLUG, defaults={"name": PLC_NAME, "owner": owner}
        )
        if device.tag == "simulado" or not device.is_active:
            device.tag, device.name, device.is_active = "", PLC_NAME, True
            device.save(update_fields=["tag", "name", "is_active"])
            self.stdout.write("El dispositivo 'plc' simulado pasa a ser el PLC real.")

        for variable, (unit, widget_type, vmin, vmax) in VARIABLES.items():
            sensor, _ = Sensor.objects.update_or_create(
                device=device, variable=variable, defaults={"unit": unit, "is_active": True}
            )
            Widget.objects.update_or_create(
                sensor=sensor,
                defaults={"widget_type": widget_type, "min_value": vmin, "max_value": vmax,
                          "led_on_threshold": 1, "button_on_value": 1, "button_off_value": 0},
            )
        old = device.sensors.exclude(variable__in=VARIABLES).filter(is_active=True)
        if old.exists():
            self.stdout.write(f"Sensores simulados desactivados (no borrados): {', '.join(old.values_list('variable', flat=True))}")
            old.update(is_active=False)

        self.stdout.write(self.style.SUCCESS(f"{'Creado' if created else 'Actualizado'}: {device.name} (slug '{PLC_SLUG}')"))
        self.stdout.write(f"PLC_TOKEN={device.auth_token}")
        raspberry = Device.objects.filter(slug__in=["raspberry", "raspberry-pi", "rpi"]).first()
        if raspberry:
            self.stdout.write(f"RASPBERRY_TOKEN={raspberry.auth_token}  (dispositivo '{raspberry.slug}')")
        self.stdout.write("Copia estas líneas en raspberry/plc.env de la Raspberry (no las subas a GitHub).")
