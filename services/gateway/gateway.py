"""Gateway IoT -> InterSCity.

Recebe as leituras dos dispositivos (ESP32 ou simulador) por HTTP e as
encaminha ao Resource Adaptor da plataforma InterSCity.

    POST /telemetria  {"maquina_id": "maq-001", "temp": 61.2, "vibra": 2.3}

O gateway não guarda estado: qualquer réplica atende qualquer requisição,
então o Kubernetes pode escalar, matar e recriar pods à vontade.
Se o InterSCity falhar, o gateway responde 502 e o dispositivo reenvia.

É assíncrono: enquanto espera a resposta do InterSCity, o mesmo processo
atende outras requisições. Assim um pod sustenta centenas de envios em
andamento, em vez de ficar limitado a um pool de threads.
"""

import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiohttp
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

# Nomes das capabilities no Catalog (o dispositivo continua mandando temp/vibra)
CAP_TEMPERATURA = os.getenv("CAP_TEMPERATURA", "temperatura")
CAP_VIBRACAO = os.getenv("CAP_VIBRACAO", "vibração")

TENTATIVAS = 3
# Teto de conexões simultâneas por pod com o InterSCity (protege o servidor externo)
MAX_CONEXOES = int(os.getenv("MAX_CONEXOES", "200"))
POD = socket.gethostname()

contadores = {"enviadas": 0, "falhas": 0}
cliente: aiohttp.ClientSession


@asynccontextmanager
async def ciclo_de_vida(app):
    global cliente
    cliente = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=MAX_CONEXOES),
        timeout=aiohttp.ClientTimeout(total=10),
    )
    yield
    await cliente.close()


app = FastAPI(title="Gateway InterSCity", version="1.1.0", lifespan=ciclo_de_vida)


class Leitura(BaseModel):
    maquina_id: str
    temp: float
    vibra: float
    ts: str | None = None  # ISO-8601; se ausente, usa o horário do gateway


async def enviar_ao_interscity(uuid, corpo):
    """POST no Resource Adaptor com até 3 tentativas (backoff 0.5s, 1s)."""
    url = f"{INTERSCITY_URL}/adaptor/resources/{uuid}/data"
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            async with cliente.post(url, json=corpo) as resposta:
                if resposta.status < 300:
                    return
                erro = f"HTTP {resposta.status}: {(await resposta.text())[:200]}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            erro = f"{exc.__class__.__name__}: {exc}"
        LOG.warning("tentativa %d/%d falhou (%s)", tentativa, TENTATIVAS, erro)
        if tentativa < TENTATIVAS:
            await asyncio.sleep(0.5 * tentativa)
    raise HTTPException(502, f"InterSCity indisponível: {erro}")


@app.post("/telemetria", status_code=201)
async def receber(leitura: Leitura):
    uuid = MAQUINAS.get(leitura.maquina_id)
    if uuid is None:
        raise HTTPException(404, f"máquina '{leitura.maquina_id}' não cadastrada")

    ts = leitura.ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Nomes iguais às capabilities cadastradas no Catalog do InterSCity
    corpo = {
        "data": {
            CAP_TEMPERATURA: [{"value": leitura.temp, "timestamp": ts}],
            CAP_VIBRACAO: [{"value": leitura.vibra, "timestamp": ts}],
        }
    }

    try:
        await enviar_ao_interscity(uuid, corpo)
    except HTTPException:
        contadores["falhas"] += 1
        raise

    contadores["enviadas"] += 1
    LOG.info("%s -> InterSCity ok (temp=%.1f vibra=%.2f)",
             leitura.maquina_id, leitura.temp, leitura.vibra)
    # "pod" mostra qual réplica atendeu: útil para demonstrar o balanceamento
    return {"status": "enviado", "uuid": uuid, "pod": POD}


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "pod": POD, **contadores}


@app.get("/maquinas")
async def maquinas():
    return MAQUINAS
