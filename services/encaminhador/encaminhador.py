"""Encaminhador: consome a fila TELEMETRIA e entrega ao InterSCity em lote.

Várias réplicas compartilham o mesmo consumidor durável do JetStream, então
cada leitura é entregue a uma só réplica (fila de trabalho). Cada réplica:

  1. busca até LOTE_FETCH leituras, esperando no máximo JANELA segundos;
  2. agrupa por máquina (uuid do recurso no InterSCity);
  3. faz UM POST por máquina com várias leituras (o Resource Adaptor aceita
     listas), em pedaços de no máximo LEITURAS_POR_POST;
  4. confirma (ack) as leituras entregues; as que falharam voltam à fila
     (nak com atraso) e são reentregues depois, possivelmente por outra réplica.

MAX_EM_VOO limita os POSTs simultâneos por réplica: é o freio que protege o
InterSCity. O excesso espera na fila, não no servidor externo.

Entrega "pelo menos uma vez": se a réplica cair entre o POST e o ack, a
leitura é reentregue após ACK_WAIT e pode ser gravada em duplicidade.
"""

import asyncio
import json
import logging
import os
import socket
import time
from collections import defaultdict

import aiohttp
import nats
from aiohttp import web
from nats.js.api import AckPolicy, ConsumerConfig
from nats.js.errors import NotFoundError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("encaminhador")

INTERSCITY_URL = os.getenv(
    "INTERSCITY_URL",
    "https://cidadesinteligentes.lsdi.ufma.br/interscity_lh",
).rstrip("/")
NATS_URL = os.getenv("NATS_URL", "nats://nats:4222")
STREAM = os.getenv("STREAM", "TELEMETRIA")
CONSUMIDOR = os.getenv("CONSUMIDOR", "encaminhador")

MAQUINAS = dict(
    item.split("=", 1)
    for item in os.environ["MAQUINAS"].split(",")
    if item.strip()
)
CAP_TEMPERATURA = os.getenv("CAP_TEMPERATURA", "temperatura")
CAP_VIBRACAO = os.getenv("CAP_VIBRACAO", "vibração")

JANELA = float(os.getenv("JANELA", "1.0"))                 # s esperando juntar um lote
LOTE_FETCH = int(os.getenv("LOTE_FETCH", "2000"))          # leituras por busca na fila
LEITURAS_POR_POST = int(os.getenv("LEITURAS_POR_POST", "500"))
MAX_EM_VOO = int(os.getenv("MAX_EM_VOO", "4"))             # POSTs simultâneos por réplica
BUSCADORES = int(os.getenv("BUSCADORES", "2"))             # laços de busca por réplica
ATRASO_REENVIO = float(os.getenv("ATRASO_REENVIO", "2"))
INTERVALO_LOG = float(os.getenv("INTERVALO_LOG", "5"))
POD = socket.gethostname()

total = {"entregues": 0, "posts": 0, "falhas": 0, "descartadas": 0}
janela = {"entregues": 0, "posts": 0, "falhas": 0, "lat": []}
em_voo = 0


def corpo_interscity(leituras):
    return {"data": {
        CAP_TEMPERATURA: [{"value": l["temp"], "timestamp": l["ts"]} for l in leituras],
        CAP_VIBRACAO: [{"value": l["vibra"], "timestamp": l["ts"]} for l in leituras],
    }}


async def enviar(http, limite, uuid, msgs):
    """Um POST com várias leituras do mesmo recurso. Confirma ou devolve à fila."""
    global em_voo
    leituras = [json.loads(m.data) for m in msgs]
    url = f"{INTERSCITY_URL}/adaptor/resources/{uuid}/data"
    async with limite:
        em_voo += 1
        inicio = time.monotonic()
        try:
            async with http.post(url, json=corpo_interscity(leituras)) as r:
                ok = r.status < 300
                erro = None if ok else f"HTTP {r.status}: {(await r.text())[:200]}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            ok, erro = False, f"{exc.__class__.__name__}: {exc}"
        finally:
            em_voo -= 1
        janela["lat"].append(time.monotonic() - inicio)

    janela["posts"] += 1
    total["posts"] += 1
    if ok:
        await asyncio.gather(*(m.ack() for m in msgs))
        janela["entregues"] += len(msgs)
        total["entregues"] += len(msgs)
    else:
        LOG.warning("POST de %d leituras falhou (%s); voltam à fila", len(msgs), erro)
        await asyncio.gather(*(m.nak(delay=ATRASO_REENVIO) for m in msgs))
        janela["falhas"] += len(msgs)
        total["falhas"] += len(msgs)


async def buscador(sub, http, limite):
    while True:
        try:
            msgs = await sub.fetch(LOTE_FETCH, timeout=JANELA)
        except (nats.errors.TimeoutError, asyncio.TimeoutError):
            continue  # fila vazia
        except Exception as exc:
            LOG.warning("fetch falhou: %s: %s", exc.__class__.__name__, exc)
            await asyncio.sleep(1)
            continue

        por_recurso = defaultdict(list)
        for m in msgs:
            maquina = m.subject.split(".", 1)[1]
            uuid = MAQUINAS.get(maquina)
            if uuid is None:
                LOG.error("máquina %s sem uuid; leitura descartada", maquina)
                total["descartadas"] += 1
                await m.term()
                continue
            por_recurso[uuid].append(m)

        envios = [
            enviar(http, limite, uuid, grupo[i:i + LEITURAS_POR_POST])
            for uuid, grupo in por_recurso.items()
            for i in range(0, len(grupo), LEITURAS_POR_POST)
        ]
        await asyncio.gather(*envios)


async def relatorio():
    """Uma linha JSON a cada INTERVALO_LOG s: base do relatório do teste de carga."""
    while True:
        await asyncio.sleep(INTERVALO_LOG)
        lat = sorted(janela["lat"])
        linha = {
            "pod": POD,
            "leituras_por_s": round(janela["entregues"] / INTERVALO_LOG, 1),
            "posts_por_s": round(janela["posts"] / INTERVALO_LOG, 2),
            "leituras_por_post": round(janela["entregues"] / janela["posts"], 1)
            if janela["posts"] else 0,
            "falhas": janela["falhas"],
            "lat_media_ms": round(1000 * sum(lat) / len(lat)) if lat else 0,
            "lat_max_ms": round(1000 * lat[-1]) if lat else 0,
            "em_voo": em_voo,
            **{f"total_{k}": v for k, v in total.items()},
        }
        janela.update(entregues=0, posts=0, falhas=0, lat=[])
        if linha["total_posts"]:
            LOG.info("STATS %s", json.dumps(linha))


async def saude(request):
    return web.json_response({"pod": POD, "em_voo": em_voo, **total})


async def main():
    nc = await nats.connect(NATS_URL, name=f"encaminhador-{POD}",
                            max_reconnect_attempts=-1)
    js = nc.jetstream(timeout=10)

    # O stream é criado pelo gateway; espera ele existir.
    while True:
        try:
            await js.stream_info(STREAM)
            break
        except NotFoundError:
            LOG.info("aguardando o stream %s", STREAM)
            await asyncio.sleep(2)

    sub = await js.pull_subscribe(
        "telemetria.>", durable=CONSUMIDOR, stream=STREAM,
        config=ConsumerConfig(
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=60,
            max_deliver=20,
            max_ack_pending=50_000,
        ),
    )

    app = web.Application()
    app.router.add_get("/healthz", saude)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8001).start()

    limite = asyncio.Semaphore(MAX_EM_VOO)
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=MAX_EM_VOO),
        timeout=aiohttp.ClientTimeout(total=15),
    ) as http:
        LOG.info("consumindo %s -> %s (MAX_EM_VOO=%d, JANELA=%.1fs)",
                 STREAM, INTERSCITY_URL, MAX_EM_VOO, JANELA)
        await asyncio.gather(
            relatorio(),
            *(buscador(sub, http, limite) for _ in range(BUSCADORES)),
        )


if __name__ == "__main__":
    asyncio.run(main())
