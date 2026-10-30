#include <WiFi.h>

#include <PubSubClient.h>
#include <ArduinoJson.h>
#include "PosicionSolar.h"

#include <ESP32Servo.h>

// ---------- Credenciales WiFi ----------

#define WIFI_SSID          "wifi"

#define WIFI_PASSWORD      "contraseña"

// ---------- Broker MQTT ----------

#define MQTT_BROKER        "ip"

#define MQTT_PORT          1883

#define MQTT_USER          "user"

#define MQTT_PASSWORD      "contraseña"

#define MQTT_CLIENT_ID "esp32-lina"

// === Integración con Django ===
// Deben coincidir con MQTT_TOPIC_PREFIX, slug y auth_token del dispositivo Django.
// No compartas el archivo una vez rellenado el token real.
#define DJANGO_MQTT_PREFIX "ecs"
#define DJANGO_DEVICE_SLUG "seguidor-solar-iot"
#define DJANGO_DEVICE_TOKEN "token_esp32"


const int DESFASE_PRUEBA_HORAS = 0;

// =====================================================

// TÓPICOS MQTT

// =====================================================

#define MQTT_TOPIC_STATUS          "lina/esp32/status"

#define MQTT_TOPIC_LIGHT_LEFT      "lina/esp32/light/left"

#define MQTT_TOPIC_LIGHT_RIGHT     "lina/esp32/light/right"

#define MQTT_TOPIC_LIGHT_UP        "lina/esp32/light/up"

#define MQTT_TOPIC_LIGHT_DOWN      "lina/esp32/light/down"

#define MQTT_TOPIC_SERVO           "lina/esp32/servo"

#define MQTT_TOPIC_SERVO2          "lina/esp32/servo2"

#define MQTT_TOPIC_MODE            "lina/esp32/mode"

#define MQTT_TOPIC_AZIMUTH         "lina/esp32/azimuth"

#define MQTT_TOPIC_ZENITH          "lina/esp32/zenith"

#define MQTT_TOPIC_SOLAR_AZIMUTH       "lina/esp32/solar/azimuth"

#define MQTT_TOPIC_SOLAR_ELEVATION     "lina/esp32/solar/elevation"

#define MQTT_TOPIC_DATE                "lina/esp32/date"

#define MQTT_TOPIC_TIME                "lina/esp32/time"

#define MQTT_TOPIC_LATITUDE            "lina/esp32/latitude"

#define MQTT_TOPIC_LONGITUDE           "lina/esp32/longitude"

// Permiso de movimiento calculado por el PLC; lo publica la Raspberry ("1" o "0") cada segundo.
#define MQTT_TOPIC_PLC_PERMISO         "lina/plc/permiso"

// Temas Django: <prefijo>/<slug>/<variable>/set (comandos)
//                <prefijo>/<slug>/<variable>     (estado/lecturas)
char djangoModeSetTopic[160];
char djangoServoSetTopic[160];
char djangoServo2SetTopic[160];

// =====================================================

// MQTT

// =====================================================

WiFiClient espClient;

PubSubClient mqttClient(espClient);

// =====================================================

// SERVOS

// =====================================================

// Servo horizontal → AZIMUT

#define SERVO_AZIMUTH_PIN 18

// Servo vertical → ZENITH

#define SERVO_ZENITH_PIN 19

Servo servoAzimuth;

Servo servoZenith;

int anguloAzimuth = 90;

int anguloZenith     = 90;

bool modoAutomatico = false;

// =====================================================

// BLOQUEO POR PERMISO DEL PLC (software: NO reemplaza una parada de emergencia)

// =====================================================

// Sin un permiso "1" recibido hace menos de este tiempo, los servos no se mueven.
const unsigned long PERMISO_TIMEOUT_MS = 5000;

bool permisoPLC = false;            // bloqueado al arrancar

unsigned long ultimoPermisoMs = 0;

bool servosActivos = false;         // los servos se adjuntan con el primer permiso

bool movimientoPermitido() {
    return mqttClient.connected() &&
           permisoPLC &&
           millis() - ultimoPermisoMs < PERMISO_TIMEOUT_MS;
}

void activarServos() {
    // attach() ya mueve el servo a su posición inicial, por eso se espera al permiso del PLC.
    servoAzimuth.attach(SERVO_AZIMUTH_PIN, 500, 2400);
    servoAzimuth.write(anguloAzimuth);
    servoZenith.attach(SERVO_ZENITH_PIN, 500, 2400);
    servoZenith.write(anguloZenith);
    servosActivos = true;
    Serial.println("Permiso del PLC recibido: servos activados en 90 grados");
}

void publishDjangoValue(const char* variable, float value) {
    if (!mqttClient.connected()) return;
    char topic[160];
    snprintf(topic, sizeof(topic), "%s/%s/%s",
             DJANGO_MQTT_PREFIX, DJANGO_DEVICE_SLUG, variable);
    StaticJsonDocument<192> doc;
    doc["value"] = value;
    doc["token"] = DJANGO_DEVICE_TOKEN;
    char payload[192];
    serializeJson(doc, payload, sizeof(payload));
    mqttClient.publish(topic, payload);
}

void publishModeState() {
    publishDjangoValue("mode", modoAutomatico ? 1.0f : 0.0f);
}




// =====================================================

// CONTROL HÍBRIDO

// =====================================================

// Límites físicos de servos

const int SERVO_MIN = 10;

const int SERVO_MAX = 170;

// -----------------------------------------------------

// Rango solar que representa el movimiento horizontal

// -----------------------------------------------------

const double AZIMUT_SOL_MIN = 90.0;

const double AZIMUT_SOL_MAX = 270.0;

// -----------------------------------------------------

// Elevación solar

// -----------------------------------------------------

const double ELEVACION_SOL_MIN = 0.0;

const double ELEVACION_SOL_MAX = 90.0;

// -----------------------------------------------------

// Corrección máxima permitida por los LDR

// -----------------------------------------------------

const int CORRECCION_LDR_MAX = 12;

// Cantidad que corrige cada vez

const int PASO_CORRECCION_LDR = 1;

// Luz mínima para permitir corrección LDR

const int LUZ_MINIMA = 300;

// Intervalo de corrección

const unsigned long intervaloControl = 250;

unsigned long ultimoControl = 0;

// Correcciones acumuladas

int correccionAzimuth = 0;

int correccionZenith = 0;

// =====================================================

// LDR

// =====================================================

// Horizontal

#define LDR_LEFT_PIN   34

#define LDR_RIGHT_PIN 35

// Vertical

#define LDR_UP_PIN     32

#define LDR_DOWN_PIN   33

const int toleranciaLuz = 150;

const int pasoServo = 2;

// =====================================================

// UBICACIÓN FIJA - SANTIAGO DE CHILE

// =====================================================

const double LATITUD = -33.4489;

const double LONGITUD = -70.6693;

// =====================================================

// NTP

// =====================================================

const char* ntpServer1 = "pool.ntp.org";

const char* ntpServer2 = "time.google.com";

const char* timezoneSantiago =

     "CLT4CLST,M9.1.6/24,M4.1.6/24";

// PosicionSolar se declara en PosicionSolar.h para evitar problemas de prototipos Arduino.

// =====================================================

// CONEXIÓN WIFI

// =====================================================

void connectWiFi() {

    Serial.printf("Conectando a WiFi %s", WIFI_SSID);

    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

    while (WiFi.status() != WL_CONNECTED) {

        delay(500);

        Serial.print(".");

    }

    Serial.println();

    Serial.print("WiFi conectado. IP: ");

    Serial.println(WiFi.localIP());

}

// ===========================

// MOV. SUAVE

// =============================

void moverServoSuave(

    Servo &servo,

      int &anguloActual,

      int anguloObjetivo

) {

      anguloObjetivo = constrain(

           anguloObjetivo,

           SERVO_MIN,

           SERVO_MAX

      );

      // Grado a grado; se detiene en cuanto el PLC deja de autorizar (auto y manual).
      while (anguloActual != anguloObjetivo) {

           if (!movimientoPermitido()) {

                 Serial.println("Movimiento BLOQUEADO: sin permiso vigente del PLC");

                 return;

           }

           anguloActual += (anguloObjetivo > anguloActual) ? 1 : -1;

           servo.write(anguloActual);

           delay(20);

      }

}

// =====================================================

// RECIBIR MENSAJES MQTT

// =====================================================

void mqttCallback(char* topic, byte* payload, unsigned int length) {

    String mensaje = "";

    for (unsigned int i = 0; i < length; i++) {

        mensaje += (char)payload[i];

    }

    // Permiso del PLC (llega cada segundo; no se imprime para no saturar el monitor serie)
    if (strcmp(topic, MQTT_TOPIC_PLC_PERMISO) == 0) {
        bool nuevo = (mensaje == "1");
        if (nuevo != permisoPLC) {
            Serial.println(nuevo ? "PLC: movimiento PERMITIDO" : "PLC: movimiento BLOQUEADO");
        }
        permisoPLC = nuevo;
        ultimoPermisoMs = millis();
        if (permisoPLC && !servosActivos) {
            activarServos();
        }
        return;
    }

    Serial.print("Mensaje recibido en ");

    Serial.print(topic);

Serial.print(": ");

Serial.println(mensaje);

// Comandos desde Django: JSON {value: 0|1|angulo, token: "..."}.
if (strcmp(topic, djangoModeSetTopic) == 0 ||
    strcmp(topic, djangoServoSetTopic) == 0 ||
    strcmp(topic, djangoServo2SetTopic) == 0) {
    StaticJsonDocument<256> doc;
    if (deserializeJson(doc, payload, length)) {
        Serial.println("Django: JSON invalido");
        return;
    }
    const char* token = doc["token"] | "";
    if (strcmp(token, DJANGO_DEVICE_TOKEN) != 0) {
        Serial.println("Django: token invalido");
        return;
    }
    if (!doc["value"].is<float>() && !doc["value"].is<int>()) {
        Serial.println("Django: value invalido");
        return;
    }
    float value = doc["value"].as<float>();
    if (strcmp(topic, djangoModeSetTopic) == 0) {
        if (value != 0.0f && value != 1.0f) return;
        modoAutomatico = (value == 1.0f);
        Serial.println(modoAutomatico ? "Django: AUTOMATICO" : "Django: MANUAL");
        publishModeState();
    } else if (!modoAutomatico && value >= SERVO_MIN && value <= SERVO_MAX) {
        int angle = (int)roundf(value);
        if (strcmp(topic, djangoServoSetTopic) == 0) {
            moverServoSuave(servoAzimuth, anguloAzimuth, angle);
            publishDjangoValue("azimuth", anguloAzimuth);
        } else {
            moverServoSuave(servoZenith, anguloZenith, angle);
            publishDjangoValue("zenith", anguloZenith);
        }
    }
    return;
}

// MODO MANUAL / AUTOMÁTICO

if (String(topic) == MQTT_TOPIC_MODE) {

    if (mensaje == "AUTO") {

        modoAutomatico = true;

        Serial.println("Modo AUTOMATICO");

    } else if (mensaje == "MANUAL") {

        modoAutomatico = false;

        Serial.println("Modo MANUAL");

    }

}

// CONTROL MANUAL SERVO HORIZONTAL

if (String(topic) == MQTT_TOPIC_SERVO) {

    if (!modoAutomatico) {

        int nuevoAngulo = mensaje.toInt();

        nuevoAngulo = constrain(nuevoAngulo, 10, 170);

        if (abs(nuevoAngulo - anguloAzimuth) >= 2) {

                 moverServoSuave(

                 servoAzimuth,

                 anguloAzimuth,

                 nuevoAngulo

            );

                 Serial.print("Servo horizontal: ");

                 Serial.print(nuevoAngulo);

                 Serial.println(" grados");

        }

    }

}

// CONTROL MANUAL SERVO VERTICAL

if (String(topic) == MQTT_TOPIC_SERVO2) {

    if (!modoAutomatico) {

        int nuevoAngulo2 = mensaje.toInt();

            nuevoAngulo2 = constrain(nuevoAngulo2, 10, 170);

            if (abs(nuevoAngulo2 - anguloZenith) >= 2) {

                     moverServoSuave(

                     servoZenith,

                     anguloZenith,

                     nuevoAngulo2

                );

                     Serial.print("Servo vertical: ");

                     Serial.print(nuevoAngulo2);

                     Serial.println(" grados");

            }

        }

    }

}

// =====================================================

// CONEXIÓN MQTT

// =====================================================

void connectMQTT() {

    while (!mqttClient.connected()) {

Serial.print("Conectando a broker MQTT...");

bool connected;

if (strlen(MQTT_USER) > 0) {

    connected = mqttClient.connect(

           MQTT_CLIENT_ID,

           MQTT_USER,

           MQTT_PASSWORD,

           MQTT_TOPIC_STATUS,

           0,

           true,

           "OFF"

    );

} else {

    connected = mqttClient.connect(

           MQTT_CLIENT_ID,

           MQTT_TOPIC_STATUS,

           0,

           true,

           "OFF"

    );

}

if (connected) {

    Serial.println(" conectado.");

    permisoPLC = false;  // tras una reconexión se exige un permiso nuevo del PLC

    mqttClient.publish(

           MQTT_TOPIC_STATUS,

           "ON",

           true

    );

    Serial.println("Estado publicado: ON");

    mqttClient.subscribe(MQTT_TOPIC_SERVO);

    mqttClient.subscribe(MQTT_TOPIC_SERVO2);

    mqttClient.subscribe(MQTT_TOPIC_MODE);
    mqttClient.subscribe(djangoModeSetTopic, 1);
    mqttClient.subscribe(djangoServoSetTopic, 1);
    mqttClient.subscribe(djangoServo2SetTopic, 1);
    mqttClient.subscribe(MQTT_TOPIC_PLC_PERMISO);
    publishModeState();

    Serial.println("Suscrito a control de servos y modo");

} else {

    Serial.printf(

           " fallo, rc=%d. Reintentando en 3s\n",

           mqttClient.state()

    );

            delay(3000);

        }

    }

}

// =====================================================

// CALCULAR POSICIÓN DEL SOL

// =====================================================

PosicionSolar calcularPosicionSolar() {

    PosicionSolar resultado;

    time_t ahora = time(nullptr) + (DESFASE_PRUEBA_HORAS * 3600);

    if (ahora < 100000) {

        resultado.azimut = 0;

        resultado.elevacion = 0;

        return resultado;

    }

    struct tm tiempoUTC;

    gmtime_r(&ahora, &tiempoUTC);

double julianDay =

    2440587.5 +

    ((double)ahora / 86400.0);

double T =

    (julianDay - 2451545.0)

    / 36525.0;

double L0 =

    280.46646 +

    T * (

         36000.76983 +

         T * 0.0003032

    );

L0 = fmod(L0, 360.0);

if (L0 < 0) {

    L0 += 360.0;

}

double M =

    357.52911 +

    T * (

         35999.05029 -

         0.0001537 * T

    );

double e =

    0.016708634 -

    T * (

         0.000042037 +

         0.0000001267 * T

    );

double M_rad = radians(M);

double C =

    sin(M_rad) *

    (

         1.914602 -

         T * (

             0.004817 +

             0.000014 * T

         )

    )

    +

    sin(2 * M_rad) *

    (

        0.019993 -

        0.000101 * T

    )

    +

    sin(3 * M_rad) *

    0.000289;

double longitudVerdadera =

    L0 + C;

double omega =

    125.04 -

    1934.136 * T;

double longitudAparente =

    longitudVerdadera -

    0.00569 -

    0.00478 *

    sin(radians(omega));

double oblicuidadMedia =

    23.0 +

    (

        26.0 +

         (

               21.448 -

               T *

               (

                     46.815 +

                     T *

                     (

                           0.00059 -

                           T * 0.001813

                     )

               )

         ) / 60.0

    ) / 60.0;

double oblicuidad =

    oblicuidadMedia +

    0.00256 *

    cos(radians(omega));

double declinacion =

    degrees(

         asin(

               sin(radians(oblicuidad)) *

               sin(radians(longitudAparente))

         )

    );

double y =

    tan(

           radians(oblicuidad / 2.0)

    );

y *= y;

double ecuacionTiempo =

    4.0 *

    degrees(

           y * sin(2.0 * radians(L0))

           -

           2.0 * e * sin(M_rad)

           +

           4.0 * e * y *

           sin(M_rad) *

           cos(2.0 * radians(L0))

           -

         0.5 * y * y *

         sin(4.0 * radians(L0))

         -

         1.25 * e * e *

         sin(2.0 * M_rad)

    );

double minutosUTC =

    tiempoUTC.tm_hour * 60.0 +

    tiempoUTC.tm_min +

    tiempoUTC.tm_sec / 60.0;

double tiempoSolarVerdadero =

    minutosUTC +

    ecuacionTiempo +

    4.0 * LONGITUD;

tiempoSolarVerdadero =

    fmod(

         tiempoSolarVerdadero,

         1440.0

    );

if (tiempoSolarVerdadero < 0) {

    tiempoSolarVerdadero += 1440;

}

double anguloHorario =

    tiempoSolarVerdadero / 4.0 -

    180.0;

double latRad =

    radians(LATITUD);

double declRad =

    radians(declinacion);

double horaRad =

    radians(anguloHorario);

double cosZenit =

    sin(latRad) *

    sin(declRad)

    +

    cos(latRad) *

    cos(declRad) *

    cos(horaRad);

cosZenit =

    constrain(

         cosZenit,

         -1.0,

         1.0

    );

double zenit =

    degrees(

         acos(cosZenit)

    );

double elevacion =

    90.0 -

    zenit;

double azimut =

    degrees(

         atan2(

               sin(horaRad),

                   cos(horaRad) *

                   sin(latRad)

                   -

                   tan(declRad) *

                   cos(latRad)

            )

        )

        + 180.0;

    if (azimut < 0) {

        azimut += 360;

    }

    if (azimut >= 360) {

        azimut -= 360;

    }

    resultado.azimut = azimut;

    resultado.elevacion = elevacion;

    return resultado;

}

// =====================================================

// CONVERTIR AZIMUT SOLAR A ÁNGULO DEL SERVO

// =====================================================

int azimutSolarAServo(double azimutSolar) {

   azimutSolar = constrain(

        azimutSolar,

        AZIMUT_SOL_MIN,

        AZIMUT_SOL_MAX

   );

   double posicion =

        SERVO_MAX -

        (

            (azimutSolar - AZIMUT_SOL_MIN)

            /

            (AZIMUT_SOL_MAX - AZIMUT_SOL_MIN)

        )

        *

        (SERVO_MAX - SERVO_MIN);

    return constrain(

         (int)round(posicion),

         SERVO_MIN,

         SERVO_MAX

    );

}

// =====================================================

// CONVERTIR ELEVACIÓN SOLAR A SERVO VERTICAL

// =====================================================

int elevacionSolarAServo(double elevacionSolar) {

    elevacionSolar = constrain(

         elevacionSolar,

         ELEVACION_SOL_MIN,

         ELEVACION_SOL_MAX

    );

    double posicion =

         SERVO_MIN +

         (

             (elevacionSolar - ELEVACION_SOL_MIN)

             /

               (ELEVACION_SOL_MAX - ELEVACION_SOL_MIN)

           )

           *

           (SERVO_MAX - SERVO_MIN);

    return constrain(

           (int)round(posicion),

           SERVO_MIN,

           SERVO_MAX

    );

}

// =====================================================

// SETUP

// =====================================================

void setup() {

    Serial.begin(115200);

    analogReadResolution(12);

    Serial.println("LDR izquierdo: GPIO 34");

    Serial.println("LDR derecho: GPIO 35");

Serial.println("LDR arriba: GPIO 32");

Serial.println("LDR abajo: GPIO 33");

// SERVO HORIZONTAL

servoAzimuth.setPeriodHertz(50);

// SERVO VERTICAL

servoZenith.setPeriodHertz(50);

// Bloqueo por defecto: los servos se adjuntan (y van a 90 grados) solo con el primer permiso del PLC.
Serial.println("Servos BLOQUEADOS hasta recibir permiso del PLC");

connectWiFi();

// NTP - HORA SANTIAGO

Serial.println("Sincronizando hora con NTP...");

configTzTime(

      timezoneSantiago,

      ntpServer1,

      ntpServer2

);

struct tm tiempo;

int intentosNTP = 0;

while (

      !getLocalTime(&tiempo) &&

      intentosNTP < 20

) {

      Serial.println(

           "Esperando hora NTP..."

      );

      delay(500);

      intentosNTP++;

}

if (intentosNTP < 20) {

          Serial.println(

               "Hora NTP sincronizada"

          );

    } else {

          Serial.println(

               "No se pudo sincronizar NTP"

          );

    }

    snprintf(djangoModeSetTopic, sizeof(djangoModeSetTopic), "%s/%s/mode/set", DJANGO_MQTT_PREFIX, DJANGO_DEVICE_SLUG);
    snprintf(djangoServoSetTopic, sizeof(djangoServoSetTopic), "%s/%s/servo/set", DJANGO_MQTT_PREFIX, DJANGO_DEVICE_SLUG);
    snprintf(djangoServo2SetTopic, sizeof(djangoServo2SetTopic), "%s/%s/servo2/set", DJANGO_MQTT_PREFIX, DJANGO_DEVICE_SLUG);
    mqttClient.setServer(MQTT_BROKER, MQTT_PORT);

    mqttClient.setCallback(mqttCallback);

}

// =====================================================

// LOOP

// =====================================================

unsigned long lastPublish = 0;

const unsigned long publishInterval = 1000;

void loop() {

if (WiFi.status() != WL_CONNECTED) {

       connectWiFi();

}

if (!mqttClient.connected()) {

       connectMQTT();

}

mqttClient.loop();

// =====================================================

// MODO AUTOMÁTICO HÍBRIDO

// =====================================================

if (

       modoAutomatico &&

       movimientoPermitido() &&

       millis() - ultimoControl >= intervaloControl

) {

       ultimoControl = millis();

       //PRUEBA DE NOCHE

       // Cordillera

       //PosicionSolar sol;

//sol.azimut = 90.0;

//sol.elevacion = 45.0;

 //Oceano

//sol.azimut = 270.0;

//sol.elevacion = 45.0;

//REAL / DIA

PosicionSolar sol =

    calcularPosicionSolar();

if (sol.elevacion > 0) {

    int azimuthBase =

        azimutSolarAServo(sol.azimut);

    int zenithBase =

        elevacionSolarAServo(sol.elevacion);

    int luzIzquierda =

        analogRead(LDR_LEFT_PIN);

    int luzDerecha =

        analogRead(LDR_RIGHT_PIN);

int luzArriba =

    analogRead(LDR_UP_PIN);

int luzAbajo =

    analogRead(LDR_DOWN_PIN);

int luzPromedio =

    (

           luzIzquierda +

           luzDerecha +

           luzArriba +

           luzAbajo

    )

    / 4;

if (luzPromedio >= LUZ_MINIMA) {

    int diferenciaHorizontal =

           luzIzquierda -

           luzDerecha;

    if (

           diferenciaHorizontal >

                        toleranciaLuz

                  ) {

                  correccionAzimuth -=

                  PASO_CORRECCION_LDR;

              }

else if (

      diferenciaHorizontal <

      -toleranciaLuz

) {

          correccionAzimuth +=

              PASO_CORRECCION_LDR;

              }

                  else {

                        if (correccionAzimuth > 0) {

                            correccionAzimuth--;

                        }

                        else if (

                            correccionAzimuth < 0

       ) {

             correccionAzimuth++;

       }

}

correccionAzimuth =

       constrain(

             correccionAzimuth,

             -CORRECCION_LDR_MAX,

             CORRECCION_LDR_MAX

       );

int diferenciaVertical =

       luzArriba -

       luzAbajo;

if (

       diferenciaVertical >

       toleranciaLuz

) {

correccionZenith -=

PASO_CORRECCION_LDR;

}

else if (

      diferenciaVertical <

      -toleranciaLuz

) {

correccionZenith +=

PASO_CORRECCION_LDR;

}

else {

      if (

             correccionZenith > 0

      ) {

             correccionZenith--;

      }

      else if (

             correccionZenith < 0

      ) {

             correccionZenith++;

      }

    }

    correccionZenith =

        constrain(

             correccionZenith,

             -CORRECCION_LDR_MAX,

             CORRECCION_LDR_MAX

        );

}

int azimuthFinal =

    azimuthBase +

    correccionAzimuth;

int zenithFinal =

    zenithBase +

    correccionZenith;

azimuthFinal =

    constrain(

        azimuthFinal,

        SERVO_MIN,

        SERVO_MAX

         );

zenithFinal =

         constrain(

                zenithFinal,

                SERVO_MIN,

                SERVO_MAX

         );

if (

         abs(

                azimuthFinal -

                anguloAzimuth

         ) >= 1

) {

         moverServoSuave(

         servoAzimuth,

         anguloAzimuth,

         azimuthFinal

    );

}

if (

         abs(

                zenithFinal -

              anguloZenith

          ) >= 1

) {

          moverServoSuave(

          servoZenith,

          anguloZenith,

          zenithFinal

     );

}

Serial.println(

          "========== CONTROL HIBRIDO =========="

);

Serial.print("Sol Azimut: ");

Serial.print(sol.azimut, 2);

Serial.print("° | Elevacion: ");

Serial.print(sol.elevacion, 2);

Serial.println("°");

Serial.print("Base servo AZ: ");

Serial.print(azimuthBase);

Serial.print("° | correccion LDR: ");

Serial.print(correccionAzimuth);

Serial.println("°");

Serial.print("Base servo vertical: ");

Serial.print(zenithBase);

Serial.print("° | correccion LDR: ");

Serial.print(correccionZenith);

Serial.println("°");

Serial.print("FINAL -> AZ: ");

Serial.print(anguloAzimuth);

Serial.print("° | ZEN: ");

Serial.print(anguloZenith);

Serial.println("°");

Serial.print("LDR -> ");

Serial.print(luzIzquierda);

Serial.print(" / ");

        Serial.print(luzDerecha);

        Serial.print(" | ");

        Serial.print(luzArriba);

        Serial.print(" / ");

        Serial.println(luzAbajo);

    }

}

// =================================================

// PUBLICAR MQTT CADA 1 SEGUNDO

// =================================================

if (millis() - lastPublish >= publishInterval) {

    lastPublish = millis();
    publishModeState();
    publishDjangoValue("azimuth", anguloAzimuth);
    publishDjangoValue("zenith", anguloZenith);
    publishDjangoValue("movimiento_permitido", movimientoPermitido() ? 1.0f : 0.0f);

    struct tm tiempoLocal;

    char fechaActual[20];

    char horaActual[20];

    if (getLocalTime(&tiempoLocal)) {

    strftime(

         fechaActual,

         sizeof(fechaActual),

         "%d-%m-%Y",

         &tiempoLocal

    );

    strftime(

         horaActual,

         sizeof(horaActual),

         "%H:%M:%S",

         &tiempoLocal

    );

    mqttClient.publish(

         MQTT_TOPIC_DATE,

         fechaActual

    );

    mqttClient.publish(

         MQTT_TOPIC_TIME,

         horaActual

    );

}

PosicionSolar sol =

     calcularPosicionSolar();

char mensajeAzimutSolar[20];

char mensajeElevacionSolar[20];

snprintf(

     mensajeAzimutSolar,

     sizeof(mensajeAzimutSolar),

     "%.2f",

     sol.azimut

);

snprintf(

     mensajeElevacionSolar,

     sizeof(mensajeElevacionSolar),

     "%.2f",

     sol.elevacion

);

mqttClient.publish(

     MQTT_TOPIC_SOLAR_AZIMUTH,

     mensajeAzimutSolar

);

mqttClient.publish(

     MQTT_TOPIC_SOLAR_ELEVATION,

     mensajeElevacionSolar

);

char mensajeLatitud[20];

char mensajeLongitud[20];

snprintf(

     mensajeLatitud,

     sizeof(mensajeLatitud),

     "%.6f",

     LATITUD

);

snprintf(

     mensajeLongitud,

     sizeof(mensajeLongitud),

     "%.6f",

     LONGITUD

);

mqttClient.publish(

     MQTT_TOPIC_LATITUDE,

     mensajeLatitud

);

mqttClient.publish(

     MQTT_TOPIC_LONGITUDE,

     mensajeLongitud

);

int luzIzquierda = analogRead(LDR_LEFT_PIN);

int luzDerecha    = analogRead(LDR_RIGHT_PIN);

int luzArriba     = analogRead(LDR_UP_PIN);

int luzAbajo      = analogRead(LDR_DOWN_PIN);

char mensajeIzq[10];

char mensajeDer[10];

char mensajeUp[10];

char mensajeDown[10];

sprintf(mensajeIzq, "%d", luzIzquierda);

sprintf(mensajeDer, "%d", luzDerecha);

sprintf(mensajeUp, "%d", luzArriba);

sprintf(mensajeDown, "%d", luzAbajo);

mqttClient.publish(

     MQTT_TOPIC_LIGHT_LEFT,

     mensajeIzq

);

mqttClient.publish(

     MQTT_TOPIC_LIGHT_RIGHT,

     mensajeDer

);

mqttClient.publish(

     MQTT_TOPIC_LIGHT_UP,

     mensajeUp

);

mqttClient.publish(

     MQTT_TOPIC_LIGHT_DOWN,

     mensajeDown

);

char mensajeAzimut[10];

sprintf(

     mensajeAzimut,

     "%d",

     anguloAzimuth

);

mqttClient.publish(

     MQTT_TOPIC_AZIMUTH,

     mensajeAzimut

);

char mensajeZenith[10];

sprintf(

     mensajeZenith,

     "%d",

     anguloZenith

);

mqttClient.publish(

     MQTT_TOPIC_ZENITH,

     mensajeZenith

);

Serial.print("Azimut: ");

Serial.print(anguloAzimuth);

Serial.print("° | Zenith: ");

Serial.print(anguloZenith);

Serial.println("°");

Serial.print("Fecha: ");

Serial.print(fechaActual);

Serial.print(" | Hora Santiago: ");

Serial.println(horaActual);

         Serial.print("Sol -> Azimut: ");

         Serial.print(sol.azimut, 2);

         Serial.print("° | Elevacion: ");

         Serial.print(sol.elevacion, 2);

         Serial.println("°");

    }

}
