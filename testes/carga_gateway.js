// Teste de carga do gateway (envios da ESP32), com k6.
//
//   make teste-gateway                      # escada de vazão (padrão)
//   make teste-gateway CENARIO=simultaneos  # escada de conexões simultâneas
//   make teste-real                         # InterSCity real, rampa leve até 20 req/s
//   make teste-autorizado                   # InterSCity real, até 1000 req/s (com autorização)
//
// Cada iteração = 1 POST /telemetria, igual ao que a ESP32 faz.
// O teste falha (exit != 0) se os limites de "thresholds" forem estourados.

import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const URL = __ENV.URL || "http://localhost:8000";
const CENARIO = __ENV.CENARIO || "vazao";
const porPod = new Counter("respostas_por_pod");

const cenarios = {
  // Requisições por segundo crescentes, independentemente da latência.
  // Responde: "quantos envios por segundo o sistema aguenta?"
  vazao: {
    executor: "ramping-arrival-rate",
    startRate: 20, timeUnit: "1s",
    preAllocatedVUs: 200, maxVUs: 3000,
    stages: [
      { target: 100, duration: "1m" },
      { target: 250, duration: "1m" },
      { target: 500, duration: "1m" },
      { target: 1000, duration: "1m" },
      { target: 1500, duration: "1m" },
      { target: 2000, duration: "1m" },
      { target: 0, duration: "20s" },
    ],
  },
  // Dispositivos conectados ao mesmo tempo, cada um enviando sem pausa.
  // Responde: "quantos envios simultâneos o sistema aguenta?"
  simultaneos: {
    executor: "ramping-vus",
    startVUs: 10,
    stages: [
      { target: 100, duration: "1m" },
      { target: 250, duration: "1m" },
      { target: 500, duration: "1m" },
      { target: 1000, duration: "1m" },
      { target: 0, duration: "20s" },
    ],
  },
  // InterSCity REAL (servidor compartilhado da UFMA): rampa leve, com freio.
  // Para sozinho se o servidor começar a sofrer (ver thresholds abaixo).
  real: {
    executor: "ramping-arrival-rate",
    startRate: 1, timeUnit: "1s",
    preAllocatedVUs: 20, maxVUs: 100,
    stages: [
      { target: 1, duration: "1m" },
      { target: 2, duration: "1m" },
      { target: 5, duration: "1m" },
      { target: 10, duration: "1m" },
      { target: 20, duration: "1m" },
      { target: 0, duration: "10s" },
    ],
  },
  // Teste AUTORIZADO pelo LSDi/UFMA: até 1000 req/s (≈ 5000 ESP32 enviando a
  // cada 5 s). Sobe em degraus para achar o limite antes de chegar ao topo.
  autorizado: {
    executor: "ramping-arrival-rate",
    startRate: 10, timeUnit: "1s",
    preAllocatedVUs: 200, maxVUs: 2000,
    stages: [
      { target: 50, duration: "1m" },
      { target: 100, duration: "1m" },
      { target: 250, duration: "1m" },
      { target: 500, duration: "1m" },
      { target: 750, duration: "1m" },
      { target: 1000, duration: "1m" },
      { target: 1000, duration: "2m" },   // sustenta o pico
      { target: 0, duration: "20s" },
    ],
  },
};

const limites = CENARIO === "autorizado"
  ? { // freio: para tudo se o InterSCity começar a sofrer
      http_req_failed: [{ threshold: "rate<0.02", abortOnFail: true, delayAbortEval: "30s" }],
      http_req_duration: [{ threshold: "p(95)<2000", abortOnFail: true, delayAbortEval: "30s" }],
    }
  : CENARIO === "real"
  ? { // aborta cedo para não sobrecarregar o servidor da UFMA
      http_req_failed: [{ threshold: "rate<0.05", abortOnFail: true, delayAbortEval: "20s" }],
      http_req_duration: [{ threshold: "p(95)<3000", abortOnFail: true, delayAbortEval: "20s" }],
    }
  : {
      http_req_failed: [{ threshold: "rate<0.01", abortOnFail: false }], // < 1% de erro
      http_req_duration: ["p(95)<1000"],                                  // 95% abaixo de 1 s
    };

export const options = {
  scenarios: { carga: cenarios[CENARIO] },
  thresholds: limites,
  summaryTrendStats: ["avg", "med", "p(90)", "p(95)", "p(99)", "max"],
};

// Máquinas usadas no envio. No teste autorizado, use recursos de TESTE
// (make teste-recursos) para não misturar com o histórico das máquinas reais.
const MAQUINAS = (__ENV.MAQUINAS_TESTE || "maq-001,maq-002").split(",");

export default function () {
  const maquina = MAQUINAS[Math.floor(Math.random() * MAQUINAS.length)];
  const r = http.post(`${URL}/telemetria`, JSON.stringify({
    maquina_id: maquina,
    temp: 60 + Math.random() * 5,
    vibra: 2 + Math.random(),
  }), { headers: { "Content-Type": "application/json" }, timeout: "15s" });

  // 202 = aceito na fila (gateway com NATS); 201 = versão síncrona antiga
  const ok = r.status === 202 || r.status === 201;
  check(r, { "aceito (202)": () => ok });
  if (ok) porPod.add(1, { pod: r.json("pod") });
}
