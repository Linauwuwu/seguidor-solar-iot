import json
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from monitoring.management.commands.run_mqtt_listener import _handle_message
from monitoring.models import Device, Sensor, SensorReading, Widget


# Sin collectstatic en las pruebas: storage simple en vez del manifest.
@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class PlcRealTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "a@a.cl", "1234")

    def preparar(self):
        out = StringIO()
        call_command("preparar_plc", stdout=out)
        return Device.objects.get(slug="plc"), out.getvalue()

    def test_preparar_plc_reemplaza_al_simulado(self):
        sim = Device.objects.create(owner=self.admin, slug="plc", name="PLC (simulado)", tag="simulado")
        Sensor.objects.create(device=sim, variable="plc_potencia")
        device, out = self.preparar()
        self.assertEqual(device.tag, "")
        self.assertIn(f"PLC_TOKEN={device.auth_token}", out)
        self.assertFalse(device.sensors.get(variable="plc_potencia").is_active)  # desactivado, no borrado
        self.assertEqual(device.sensors.get(variable="plc_habilitar").widget.widget_type, Widget.TYPE_BUTTON)
        self.preparar()  # idempotente
        self.assertEqual(device.sensors.filter(is_active=True).count(), 8)

    def test_listener_guarda_estado_del_plc(self):
        device, _ = self.preparar()
        payload = json.dumps({"value": 1.0, "token": device.auth_token}).encode()
        _handle_message("ecs/plc/plc_permiso", payload)
        reading = SensorReading.objects.get(sensor__device=device, sensor__variable="plc_permiso")
        self.assertEqual(reading.value, 1.0)
        _handle_message("ecs/plc/plc_permiso", json.dumps({"value": 1, "token": "falso"}).encode())
        self.assertEqual(SensorReading.objects.filter(sensor__variable="plc_permiso").count(), 1)

    def test_boton_habilitar_publica_por_mqtt(self):
        device, _ = self.preparar()
        sensor = device.sensors.get(variable="plc_habilitar")
        self.client.force_login(self.admin)
        with mock.patch("monitoring.views.publish_command", return_value=True) as publish:
            response = self.client.post(reverse("monitoring:sensor-command", args=[sensor.id]),
                                        data=json.dumps({"value": 1}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        publish.assert_called_once_with(device, "plc_habilitar", 1.0)

    def test_dashboard_muestra_supervision_plc(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("monitoring:dashboard")).content.decode()
        self.assertIn("Supervisión PLC", html)
        self.assertIn("monitoring/js/plc.js", html)
