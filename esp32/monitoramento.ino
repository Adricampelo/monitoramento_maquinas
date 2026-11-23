// ============================================================
// Monitoramento de máquinas — firmware ESP32
//
// Envia temperatura e vibração por HTTP ao gateway do cluster,
// que repassa os dados à plataforma InterSCity.
//
//   POST http://<ip-do-pc>:8000/telemetria
//   {"maquina_id":"maq-001","temp":61.2,"vibra":2.3,"ts":"..."}
// ============================================================

#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>

// ------------------------------ configuração
const char* WIFI_SSID    = "NOME_DA_REDE";
const char* WIFI_SENHA   = "SENHA_DO_WIFI";

// IP da máquina que roda o cluster (gateway publicado na porta 8000)
const char* GATEWAY_URL  = "http://192.168.14.111:8000/telemetria";

const char* MAQUINA_ID   = "maq-001";   // precisa existir no ConfigMap do gateway
const uint32_t INTERVALO_MS = 5000;     // 1 leitura a cada 5 s

const int PINO_TEMP   = 34;   // ADC — troque pelo seu sensor
const int PINO_VIBRA  = 35;   // ADC — troque pelo seu sensor

uint32_t ultimo_envio = 0;

// ------------------------------ sensores
// Substitua pelas leituras reais (DS18B20, DHT22, SW-420, MPU6050...).
float ler_temperatura() {
  int bruto = analogRead(PINO_TEMP);
  return 20.0 + (bruto / 4095.0) * 70.0;      // 20 °C .. 90 °C
}

float ler_vibracao() {
  int bruto = analogRead(PINO_VIBRA);
  return (bruto / 4095.0) * 10.0;             // 0 .. 10 mm/s
}

// ------------------------------ tempo (NTP)
void sincronizar_relogio() {
  configTime(0, 0, "pool.ntp.org", "a.st1.ntp.br");
  Serial.print("sincronizando relogio");
  for (int i = 0; i < 20; i++) {
    struct tm t;
    time_t agora = time(nullptr);
    gmtime_r(&agora, &t);
    if (t.tm_year + 1900 >= 2025) { Serial.println(" ok"); return; }
    Serial.print(".");
    delay(500);
  }
  Serial.println(" falhou (o gateway usa a propria hora)");
}

String agora_iso() {
  time_t agora = time(nullptr);
  struct tm t;
  gmtime_r(&agora, &t);
  if (t.tm_year + 1900 < 2025) return "";     // sem hora confiável
  char buffer[32];
  strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &t);
  return String(buffer);
}

// ------------------------------ envio
void enviar_leitura() {
  float temp  = ler_temperatura();
  float vibra = ler_vibracao();
  String ts = agora_iso();

  char payload[192];
  if (ts.length()) {
    snprintf(payload, sizeof(payload),
             "{\"maquina_id\":\"%s\",\"temp\":%.2f,\"vibra\":%.2f,\"ts\":\"%s\"}",
             MAQUINA_ID, temp, vibra, ts.c_str());
  } else {
    snprintf(payload, sizeof(payload),
             "{\"maquina_id\":\"%s\",\"temp\":%.2f,\"vibra\":%.2f}",
             MAQUINA_ID, temp, vibra);
  }

  // até 3 tentativas: se um pod do gateway cair, o Service manda para outro
  for (int tentativa = 1; tentativa <= 3; tentativa++) {
    HTTPClient http;
    http.begin(GATEWAY_URL);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(15000);
    int codigo = http.POST(payload);
    http.end();

    Serial.printf("temp=%.1f vibra=%.2f -> HTTP %d\n", temp, vibra, codigo);
    if (codigo >= 200 && codigo < 300) return;
    delay(1000 * tentativa);
  }
}

// ------------------------------ setup / loop
void setup() {
  Serial.begin(115200);
  delay(200);

  WiFi.begin(WIFI_SSID, WIFI_SENHA);
  Serial.print("conectando no wifi");
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    delay(500);
  }
  Serial.printf(" ok (%s)\n", WiFi.localIP().toString().c_str());

  sincronizar_relogio();
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    WiFi.reconnect();
    delay(1000);
    return;
  }

  uint32_t agora = millis();
  if (agora - ultimo_envio >= INTERVALO_MS) {
    ultimo_envio = agora;
    enviar_leitura();
  }
}
