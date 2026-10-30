# SolarTrack IoT ☀️

**Seguidor solar con ESP32, sensores LDR, plataforma web Django e integración industrial con Raspberry Pi y PLC Siemens S7-1200.**

Proyecto académico de Internet de las Cosas (IoT), desarrollado para monitorear la iluminación, visualizar la posición solar y controlar el movimiento de un prototipo de seguimiento solar.

## ¿Qué hace el proyecto?

SolarTrack IoT integra un sistema físico con una plataforma web de monitoreo:

- **Adquisición de luz:** cuatro sensores LDR conectados al **ESP32** permiten comparar la iluminación recibida desde izquierda, derecha, arriba y abajo.
- **Seguimiento solar:** el ESP32 gestiona el movimiento de los servomotores del prototipo y publica variables de orientación y posición solar.
- **Monitoreo web:** un dashboard personalizado en **Django** muestra las lecturas y la visualización de la esfera celeste.
- **Mensajería IoT:** **MQTT (Mosquitto)** transporta los datos entre los dispositivos y el backend.
- **Integración industrial:** una **Raspberry Pi** actúa como enlace de comunicación con un **PLC Siemens S7-1200**, mediante Python y Snap7. El PLC interviene en la lógica de habilitación y permiso de movimiento.

> **Importante:** los LDR se conectan al ESP32, **no directamente al PLC**. El PLC se utiliza para supervisión y autorización del movimiento; sus variables internas no deben confundirse con sensores físicos.

## Arquitectura

```text
   4 sensores LDR
         │
         ▼
       ESP32 ─────────────► Servomotores
         │                       ▲
         │ MQTT                  │ Permiso de movimiento
         ▼                       │
  Broker Mosquitto ◄──────► Raspberry Pi ◄──── Snap7 ────► PLC Siemens S7-1200
         │
         ▼
  Listener MQTT (Django)
         │
         ▼
     PostgreSQL
         │
         ▼
 Dashboard SolarTrack IoT
```

La arquitectura separa la **medición de luz**, el **monitoreo web** y la **autorización industrial del movimiento**.

## Tecnologías

| Componente | Tecnología | Uso |
|---|---|---|
| Microcontrolador | ESP32 | Lectura de LDR y control del prototipo |
| Sensores | 4 LDR | Medición relativa de iluminación |
| Actuadores | Servomotores | Orientación del seguidor |
| Equipo de borde | Raspberry Pi | Enlace MQTT–PLC |
| Controlador industrial | Siemens S7-1200 | Habilitación, bloqueo y permiso de movimiento |
| Comunicación industrial | Python / Snap7 | Lectura y escritura de variables del PLC |
| Mensajería | MQTT / Mosquitto | Intercambio de telemetría y comandos |
| Backend | Python / Django | Recepción, almacenamiento y aplicación web |
| Base de datos | PostgreSQL | Historial de lecturas |
| Despliegue local | Docker Compose | Servicios del backend |

## Datos publicados por el ESP32

El ESP32 publica valores numéricos en tópicos MQTT del espacio `lina/esp32/`:

| Tópico | Variable en Django |
|---|---|
| `lina/esp32/light/left` | `light_left` |
| `lina/esp32/light/right` | `light_right` |
| `lina/esp32/light/up` | `light_up` |
| `lina/esp32/light/down` | `light_down` |
| `lina/esp32/azimuth` | `azimuth` |
| `lina/esp32/zenith` | `zenith` |
| `lina/esp32/solar/azimuth` | `solar_azimuth` |
| `lina/esp32/solar/elevation` | `solar_elevation` |
| `lina/esp32/latitude` | `latitude` |
| `lina/esp32/longitude` | `longitude` |

El listener de Django está preparado para recibir directamente `lina/#`, **sin ejecutar un puente adicional para el ESP32**. También contempla el espacio `ecs/+/+` para la telemetría autenticada de los dispositivos integrados, incluida la del PLC.

## Integración Raspberry Pi y PLC

El prototipo incorpora una lógica de autorización industrial. La Raspberry Pi intercambia información con el PLC y publica o recibe mensajes MQTT para coordinar el permiso de movimiento.

Entre las variables utilizadas en el PLC se encuentran:

- `Habilitar`: solicitud de habilitación.
- `Bloqueo`: condición que impide el movimiento.
- `PermisoMovimiento`: autorización de movimiento.
- `RaspberryOnline` y `Heartbeat`: supervisión de la comunicación.
- `AnguloHorizontal` y `AnguloVertical`: variables de orientación.

El dashboard puede enviar comandos de habilitación por MQTT. La autorización efectiva depende de la lógica del PLC y de la comunicación con la Raspberry Pi.

**Estado de integración:** se ha trabajado con un PLC Siemens físico y se ha desarrollado la comunicación con Raspberry Pi. La conexión Ethernet Raspberry–PLC presentó fallas durante las pruebas más recientes; por ello, **no se declara validado el funcionamiento continuo extremo a extremo** de esta integración.

## Estado del proyecto

| Funcionalidad | Estado |
|---|---|
| Dashboard SolarTrack personalizado | Implementado |
| Lectura de cuatro LDR mediante ESP32 | Implementada y recibida en Django |
| Visualización de variables solares | Implementada |
| Recepción MQTT directa en Django | Implementada |
| Base de datos de lecturas | Implementada |
| Control de servomotores del prototipo | Desarrollado |
| Comunicación Raspberry Pi–PLC | Desarrollada; requiere nueva validación de conectividad física |
| Prueba final de permiso y bloqueo con PLC | Pendiente de verificación integral |

## Ejecución del backend (entorno local)

**Requisitos:** Docker y Docker Compose, un broker MQTT accesible y los dispositivos configurados para usar ese broker.

1. Clonar el repositorio:

   ```bash
   git clone https://github.com/Linauwuwu/seguidor-solar-iot.git
   cd seguidor-solar-iot
   ```

2. Preparar las variables de entorno según los archivos de ejemplo disponibles en el repositorio. No usar contraseñas reales en archivos versionados.

3. Iniciar los servicios definidos en Docker Compose:

   ```bash
   docker compose up -d --build
   ```

4. Consultar el estado y los registros:

   ```bash
   docker compose ps
   docker compose logs --tail=50 mqtt_listener
   ```

El broker MQTT debe ser alcanzable tanto desde los contenedores como desde los dispositivos físicos. La configuración de red depende del entorno; `localhost` dentro de un contenedor no representa necesariamente al computador anfitrión.

> La ejecución de Django **no reemplaza** la configuración del ESP32, el programa del PLC ni el proceso de integración ejecutado en la Raspberry Pi.

## Seguridad y publicación

Este repositorio **no debe incluir credenciales reales**. Antes de publicar, revisar especialmente:

- `.env` y archivos de configuración local.
- Contraseñas de Wi-Fi y MQTT.
- Tokens de dispositivos y claves de Django.
- Configuraciones privadas del firmware y de la Raspberry Pi.
- Historial de Git: ignorar un archivo no elimina secretos de commits anteriores.

Utilizar archivos de ejemplo con valores ficticios para documentar la configuración. Si una credencial fue publicada, se debe revocar o cambiar.

## Alcance y limitaciones

SolarTrack IoT es un **prototipo académico**. Su objetivo es demostrar adquisición de datos, comunicación IoT, visualización y una integración de control industrial. No constituye un sistema de seguridad industrial certificado ni debe considerarse apto para operación desatendida sin las validaciones correspondientes.

## Autoría

Proyecto académico **SolarTrack IoT** — repositorio [Linauwuwu/seguidor-solar-iot](https://github.com/Linauwuwu/seguidor-solar-iot).
