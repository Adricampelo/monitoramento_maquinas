"""Gateway IoT -> InterSCity.

Recebe as leituras dos dispositivos (ESP32 ou simulador) por HTTP e as
encaminha ao Resource Adaptor da plataforma InterSCity.

    POST /telemetria  {"maquina_id": "maq-001", "temp": 61.2, "vibra": 2.3}

O gateway não guarda estado: qualquer réplica atende qualquer requisição,
então o Kubernetes pode escalar, matar e recriar pods à vontade.
Se o InterSCity falhar, o gateway responde 502 e o dispositivo reenvia.
"""

import logging
import os
import socket
import time
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("gateway")

INTERSCITY_URL = os.getenv(
    "INTERSCITY_URL",
    "https://cidadesinteligentes.lsdi.ufma.br/interscity_lh",
).rstrip("/")

# Mapeia o id do dispositivo para o uuid do recurso no InterSCity.
# Formato: "maq-001=uuid,maq-002=uuid" (vem do ConfigMap).
MAQUINAS = dict(
    item.split("=", 1)
    for item in os.environ["MAQUINAS"].split(",")
    if item.strip()
)

TENTATIVAS = 3
POD = socket.gethostname()

app = FastAPI(title="Gateway InterSCity", version="1.0.0")
sessao = requests.Session()
contadores = {"enviadas": 0, "falhas": 0}


class Leitura(BaseModel):
    maquina_id: str
    temp: float
    vibra: float
    ts: str | None = None  # ISO-8601; se ausente, usa o horário do gateway


def enviar_ao_interscity(uuid, corpo):
    """POST no Resource Adaptor com até 3 tentativas (backoff 0.5s, 1s)."""
    url = f"{INTERSCITY_URL}/adaptor/resources/{uuid}/data"
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            resposta = sessao.post(url, json=corpo, timeout=10)
            if resposta.status_code < 300:
                return
            erro = f"HTTP {resposta.status_code}: {resposta.text[:200]}"
        except requests.RequestException as exc:
            erro = str(exc)
        LOG.warning("tentativa %d/%d falhou (%s)", tentativa, TENTATIVAS, erro)
        if tentativa < TENTATIVAS:
            time.sleep(0.5 * tentativa)
    raise HTTPException(502, f"InterSCity indisponível: {erro}")


@app.post("/telemetria", status_code=201)
def receber(leitura: Leitura):
    uuid = MAQUINAS.get(leitura.maquina_id)
    if uuid is None:
        raise HTTPException(404, f"máquina '{leitura.maquina_id}' não cadastrada")

    ts = leitura.ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    corpo = {
        "data": {
            "temp": [{"value": leitura.temp, "timestamp": ts}],
            "vibra": [{"value": leitura.vibra, "timestamp": ts}],
        }
    }

    try:
        enviar_ao_interscity(uuid, corpo)
    except HTTPException:
        contadores["falhas"] += 1
        raise

    contadores["enviadas"] += 1
    LOG.info("%s -> InterSCity ok (temp=%.1f vibra=%.2f)",
             leitura.maquina_id, leitura.temp, leitura.vibra)
    # "pod" mostra qual réplica atendeu: útil para demonstrar o balanceamento
    return {"status": "enviado", "uuid": uuid, "pod": POD}


@app.get("/healthz")
def healthz():
    return {"status": "ok", "pod": POD, **contadores}


@app.get("/maquinas")
def maquinas():
    return MAQUINAS
