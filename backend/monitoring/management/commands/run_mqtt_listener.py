
import json
import logging
import math
import secrets

import paho.mqtt.client as mqtt

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from monitoring.models import (
    Device,
    Sensor,
    SensorReading,
    UnregisteredReading,
)

logger = logging.getLogger(__name__)

DEFAULT_UNITS = {
    "resistencia_tierra": "Ω",
    "voltaje": "V",
    "continuidad": "Ω",
    "temperatura": "°C",
    "humedad": "%",
    "presion": "hPa",
    "gas": "kΩ",
}

ESP32_TOPICS = {
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


class Command(BaseCommand):
    help = "Recibe telemetría MQTT del ESP32, Raspberry y PLC."

    def handle(self, *args, **options):
        topic_filters = [
            "lina/#",
            f"{settings.MQTT_TOPIC_PREFIX}/+/+",
        ]

        client = mqtt.Client()

        if settings.MQTT_USERNAME:
            client.username_pw_set(
                settings.MQTT_USERNAME,
                settings.MQTT_PASSWORD,
            )

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                self.stdout.write(
                    self.style.SUCCESS(
                        "Connected to MQTT broker at "
                        f"{settings.MQTT_BROKER_HOST}:"
                        f"{settings.MQTT_BROKER_PORT}"
                    )
                )

                for topic_filter in topic_filters:
                    client.subscribe(topic_filter, qos=1)
                    self.stdout.write(
                        f"Subscribed to {topic_filter}"
                    )
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"MQTT connect failed with rc={rc}"
                    )
                )

        def on_disconnect(client, userdata, rc):
            logger.warning(
                "Disconnected from MQTT broker (rc=%s)",
                rc,
            )

        def on_message(client, userdata, msg):
            try:
                _handle_message(msg.topic, msg.payload)
            except Exception:
                logger.exception(
                    "Failed to process MQTT message on %s",
                    msg.topic,
                )

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        client.reconnect_delay_set(
            min_delay=1,
            max_delay=30,
        )

        client.connect(
            settings.MQTT_BROKER_HOST,
            settings.MQTT_BROKER_PORT,
            keepalive=60,
        )

        client.loop_forever()


def _save_reading(device, variable, value, timestamp=None):
    if not math.isfinite(value):
        logger.warning(
            "Ignoring non-finite value for %s",
            variable,
        )
        return

    sensor, created = Sensor.objects.get_or_create(
        device=device,
        variable=variable,
        defaults={
            "unit": DEFAULT_UNITS.get(variable, ""),
        },
    )

    if not created and not sensor.unit:
        default_unit = DEFAULT_UNITS.get(variable)

        if default_unit:
            sensor.unit = default_unit
            sensor.save(update_fields=["unit"])

    now = timezone.now()

    SensorReading.objects.create(
        sensor=sensor,
        value=float(value),
        timestamp=timestamp or now,
    )

    Device.objects.filter(pk=device.pk).update(
        last_seen_at=now,
    )


def _handle_esp32_message(topic, payload):
    variable = ESP32_TOPICS.get(topic)

    # Ignorar status, permisos y otros tópicos.
    if variable is None:
        return

    device = Device.objects.filter(
        slug="seguidor-solar-iot",
        is_active=True,
    ).first()

    if device is None:
        logger.warning(
            "ESP32 device not registered or inactive"
        )
        return

    try:
        value = float(
            payload.decode("utf-8").strip()
        )
    except (UnicodeDecodeError, ValueError):
        logger.warning(
            "Invalid ESP32 payload on %s",
            topic,
        )
        return

    _save_reading(
        device=device,
        variable=variable,
        value=value,
    )


def _handle_ecs_message(topic, payload):
    parts = topic.split("/")

    if len(parts) != 3:
        logger.warning(
            "Ignoring unexpected ECS topic: %s",
            topic,
        )
        return

    _, device_slug, variable = parts

    device = Device.objects.filter(
        slug=device_slug
    ).first()

    if device is None:
        UnregisteredReading.objects.create(
            topic=topic,
            raw_payload=payload.decode(
                "utf-8",
                errors="replace",
            ),
            reason=(
                UnregisteredReading.REASON_UNKNOWN_DEVICE
            ),
        )

        logger.info(
            "Unknown device slug: %s",
            device_slug,
        )
        return

    try:
        data = json.loads(
            payload.decode("utf-8")
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "Invalid JSON on %s",
            topic,
        )
        return

    if not isinstance(data, dict):
        logger.warning(
            "Expected JSON object on %s",
            topic,
        )
        return

    token = str(data.get("token", ""))

    if not secrets.compare_digest(
        token,
        device.auth_token,
    ):
        UnregisteredReading.objects.create(
            topic=topic,
            raw_payload=payload.decode(
                "utf-8",
                errors="replace",
            ),
            reason=(
                UnregisteredReading.REASON_INVALID_TOKEN
            ),
        )

        logger.warning(
            "Invalid token for device %s",
            device_slug,
        )
        return

    value = data.get("value")

    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
    ):
        logger.warning(
            "Non-numeric value on %s",
            topic,
        )
        return

    ts = None
    raw_ts = data.get("timestamp")

    if raw_ts:
        ts = parse_datetime(str(raw_ts))

    if ts is None:
        ts = timezone.now()
    elif timezone.is_naive(ts):
        ts = timezone.make_aware(
            ts,
            timezone.get_current_timezone(),
        )

    _save_reading(
        device=device,
        variable=variable,
        value=float(value),
        timestamp=ts,
    )


def _handle_message(topic, payload):
    if topic.startswith("lina/"):
        _handle_esp32_message(topic, payload)
        return

    ecs_prefix = f"{settings.MQTT_TOPIC_PREFIX}/"

    if topic.startswith(ecs_prefix):
        _handle_ecs_message(topic, payload)
        return
