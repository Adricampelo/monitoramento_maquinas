"""Gateway IoT: recebe leituras e as grava numa fila durável (NATS JetStream).

    POST /telemetria  {"maquina_id": "maq-001", "temp": 61.2, "vibra": 2.3}

O gateway NÃO fala com o InterSCity. Ele valida a leitura, publica no stream
TELEMETRIA (replicado em 3 nós do NATS) e responde 202 assim que o broker
confirma que a gravou. Quem entrega ao InterSCity é o serviço "encaminhador",
que consome a fila em lote e no ritmo que o InterSCity aguenta.

Assim a latência vista pelo ESP32 não depende mais do servidor externo:
se o InterSCity ficar lento ou cair, as leituras esperam na fila.

    202  leitura aceita e persistida na fila (ainda não está no InterSCity)
    404  máquina não cadastrada
    503  fila cheia ou broker indisponível -> o dispositivo deve reenviar

O gateway não guarda estado: qualquer réplica atende qualquer requisição.
"""

import asyncio
import json
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import nats
from fastapi import FastAPI, HTTPException, Response
from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError
from pydantic import BaseModel

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("gateway")

NATS_URL = os.getenv("NATS_URL", "nats://nats:4222")
STREAM = os.getenv("STREAM", "TELEMETRIA")
# Tamanho máximo da fila. Cheia, o gateway recusa (503) em vez de perder dados.
FILA_MAX_BYTES = int(os.getenv("FILA_MAX_BYTES", str(1024 * 1024 * 1024)))
REPLICAS_STREAM = int(os.getenv("REPLICAS_STREAM", "3"))

# Só valida o id do dispositivo; a tradução para uuid é feita no encaminhador.
MAQUINAS = dict(
    item.split("=", 1)
    for item in os.environ["MAQUINAS"].split(",")
    if item.strip()
)
POD = socket.gethostname()

contadores = {"aceitas": 0, "recusadas": 0}
nc = None
js = None


async def garantir_stream(js):
    """Cria o stream se ainda não existir (idempotente; qualquer réplica pode fazer)."""
    config = StreamConfig(
        name=STREAM,
        subjects=["telemetria.>"],
        retention=RetentionPolicy.WORK_QUEUE,  # leitura sai da fila ao ser confirmada
        storage=StorageType.FILE,
        num_replicas=REPLICAS_STREAM,           # sobrevive à queda de 1 nó do NATS
        max_bytes=FILA_MAX_BYTES,
        discard=DiscardPolicy.NEW,              # fila cheia: recusa novas, não apaga antigas
        max_age=24 * 3600,
    )
    for tentativa in range(30):  # o cluster NATS pode ainda estar elegendo o líder
        try:
            try:
                await js.stream_info(STREAM)
            except NotFoundError:
                await js.add_stream(config)
                LOG.info("stream %s criado (R%d)", STREAM, REPLICAS_STREAM)
            return
        except Exception as exc:
            LOG.info("JetStream ainda não está pronto (%s); tentando de novo", exc)
            await asyncio.sleep(2)
    raise RuntimeError("JetStream indisponível")


@asynccontextmanager
async def ciclo_de_vida(app):
    global nc, js
    nc = await nats.connect(
        NATS_URL, name=f"gateway-{POD}", max_reconnect_attempts=-1,
    )
    js = nc.jetstream(timeout=5)
    await garantir_stream(js)
    yield
    await nc.drain()


app = FastAPI(title="Gateway IoT", version="2.0.0", lifespan=ciclo_de_vida)


class Leitura(BaseModel):
    maquina_id: str
    temp: float
    vibra: float
    ts: str | None = None  # ISO-8601; se ausente, usa o horário do gateway


@app.post("/telemetria", status_code=202)
async def receber(leitura: Leitura):
    if leitura.maquina_id not in MAQUINAS:
        raise HTTPException(404, f"máquina '{leitura.maquina_id}' não cadastrada")

    # Milissegundos: várias leituras da mesma máquina viajam no mesmo lote.
    ts = leitura.ts or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    corpo = json.dumps({
        "maquina_id": leitura.maquina_id,
        "temp": leitura.temp,
        "vibra": leitura.vibra,
        "ts": ts,
    }).encode()

    try:
        # Só retorna depois que a maioria das réplicas do stream gravou.
        ack = await js.publish(f"telemetria.{leitura.maquina_id}", corpo, stream=STREAM)
    except Exception as exc:  # fila cheia, sem quórum, timeout
        contadores["recusadas"] += 1
        LOG.warning("publicação recusada: %s: %s", exc.__class__.__name__, exc)
        raise HTTPException(503, "fila indisponível, reenvie",
                            headers={"Retry-After": "2"})

    contadores["aceitas"] += 1
    # "pod" mostra qual réplica atendeu: útil para demonstrar o balanceamento
    return {"status": "aceito", "seq": ack.seq, "pod": POD}


@app.get("/healthz")
async def healthz(response: Response):
    conectado = nc is not None and nc.is_connected
    if not conectado:
        response.status_code = 503
    return {"status": "ok" if conectado else "sem-broker", "pod": POD, **contadores}


@app.get("/maquinas")
async def maquinas():
    return MAQUINAS
