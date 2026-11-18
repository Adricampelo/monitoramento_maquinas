// ============================================================
// Monitoramento de máquinas — firmware ESP32
//
// Publica temperatura e vibração em MQTT no contrato que o
// ingestor do cluster espera:
//
//   tópico : maquinas/<MAQUINA_ID>/telemetria
//   payload: {"maquina_id","seq","ts","temp","vibra","fw"}
//
// O `seq` é gravado na NVS: sem isso, cada reboot recomeçaria do
// zero e o banco descartaria as leituras novas como duplicatas.
// ============================================================

#include <WiFi.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <time.h>

// ------------------------------ configuração
const char* WIFI_SSID    = "NOME_DA_REDE";
const char* WIFI_SENHA   = "SENHA_DO_WIFI";

// IP da máquina que roda o cluster (o broker está publicado na porta 1883)
const char* MQTT_HOST    = "192.168.14.111";
const uint16_t MQTT_PORTA = 1883;
const char* MQTT_USUARIO = "sensor";
const char* MQTT_SENHA   = "SENHA_DO_MQTT";

const char* MAQUINA_ID   = "maq-001";
const char* FW_VERSAO    = "1.0.0";
const uint32_t INTERVALO_MS = 1000;   // 1 leitura por segundo

const int PINO_TEMP   = 34;   // ADC — troque pelo seu sensor
const int PINO_VIBRA  = 35;   // ADC — troque pelo seu sensor

// ------------------------------ estado
WiFiClient rede;
PubSubClient mqtt(rede);
Preferences nvs;
uint32_t seq = 0;
uint32_t ultimo_envio = 0;

char topico[64];
char topico_status[64];

// ------------------------------ sensores
// Substitua pelas leituras reais (DS18B20, DHT22, SW-420, MPU6050...).
// Aqui: ADC de 12 bits convertido para uma faixa plausível.
float ler_temperatura() {
  int bruto = analogRead(PINO_TEMP);
  return 20.0 + (bruto / 4095.0) * 70.0;      // 20 °C .. 90 °C
}

float ler_vibracao() {
  int bruto = analogRead(PINO_VIBRA);
  return (bruto / 4095.0) * 10.0;             // 0 .. 10 mm/s
}

// ------------------------------ tempo
// Sem NTP o relógio do ESP32 começa em 1970. O banco usa o horário de
// recebimento como eixo do tempo e guarda `ts` só para diagnóstico.
void sincronizar_relogio() {
  configTime(0, 0, "pool.ntp.org", "a.st1.ntp.br");
  Serial.print("sincronizando relogio");
  for (int i = 0; i < 20; i++) {
    time_t agora = time(nullptr);
    struct tm t;
    gmtime_r(&agora, &t);
    // relógio válido = ano plausível (o ESP32 acorda em 1970)
    if (t.tm_year + 1900 >= 2025) {
      Serial.println(" ok");
      return;
    }
    Serial.print(".");
    delay(500);
  }
  Serial.println(" falhou (segue sem hora confiavel)");
}

String agora_iso() {
  time_t agora = time(nullptr);
  struct tm t;
  gmtime_r(&agora, &t);
  char buffer[32];
  strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%S.000Z", &t);
  return String(buffer);
}

// ------------------------------ NVS
void carregar_seq() {
  nvs.begin("telemetria", false);
  seq = nvs.getUInt("seq", 0);
  Serial.printf("seq retomado em %u\n", seq);
}

void salvar_seq() {
  nvs.putUInt("seq", seq);
}

// ------------------------------ MQTT
void conectar_mqtt() {
  while (!mqtt.connected()) {
    Serial.print("conectando no broker... ");

    // Last Will: se o ESP32 cair, o broker publica online=false (retido)
    bool ok = mqtt.connect(
        MAQUINA_ID, MQTT_USUARIO, MQTT_SENHA,
        topico_status, 1, true, "{\"online\":false}");

    if (ok) {
      Serial.println("ok");
      mqtt.publish(topico_status, "{\"online\":true}", true);
    } else {
      Serial.printf("falhou (rc=%d), tentando de novo\n", mqtt.state());
      delay(2000);
    }
  }
}

void publicar_leitura() {
  float temp  = ler_temperatura();
  float vibra = ler_vibracao();
  seq++;

  char payload[256];
  snprintf(payload, sizeof(payload),
           "{\"maquina_id\":\"%s\",\"seq\":%u,\"ts\":\"%s\","
           "\"temp\":%.2f,\"vibra\":%.2f,\"fw\":\"%s\"}",
           MAQUINA_ID, seq, agora_iso().c_str(), temp, vibra, FW_VERSAO);

  // QoS 1: o broker confirma o recebimento
  bool entregue = mqtt.publish(topico, payload, false);

  if (seq % 10 == 0) {
    salvar_seq();   // grava na flash a cada 10 leituras (poupa a NVS)
  }

  Serial.printf("seq=%u temp=%.1f vibra=%.2f %s\n",
                seq, temp, vibra, entregue ? "" : "(falhou)");
}

// ------------------------------ setup / loop
void setup() {
  Serial.begin(115200);
  delay(200);

  snprintf(topico, sizeof(topico), "maquinas/%s/telemetria", MAQUINA_ID);
  snprintf(topico_status, sizeof(topico_status), "maquinas/%s/status", MAQUINA_ID);

  carregar_seq();

  WiFi.begin(WIFI_SSID, WIFI_SENHA);
  Serial.print("conectando no wifi");
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    delay(500);
  }
  Serial.printf(" ok (%s)\n", WiFi.localIP().toString().c_str());

  sincronizar_relogio();

  mqtt.setServer(MQTT_HOST, MQTT_PORTA);
  mqtt.setKeepAlive(30);
  mqtt.setBufferSize(512);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(1000);
  }

  if (!mqtt.connected()) {
    conectar_mqtt();
  }
  mqtt.loop();

  uint32_t agora = millis();
  if (agora - ultimo_envio >= INTERVALO_MS) {
    ultimo_envio = agora;
    publicar_leitura();
  }
}
