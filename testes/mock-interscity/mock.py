"""InterSCity falso, só para testes de carga.

Imita os dois endpoints que o projeto usa, com latência configurável,
para medir a capacidade do gateway e do dashboard sem sobrecarregar o
servidor real da UFMA.

    LATENCIA_MS   atraso de cada resposta (padrão 200, parecido com a internet)
    INTERVALO_S   espaçamento das medições geradas pelo Collector (padrão 30)

O Collector respeita start_date/end_date e, como o real, devolve no máximo
1000 registros por consulta (somando as capabilities).
"""

import asyncio
import os
import random
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI

LATENCIA = int(os.getenv("LATENCIA_MS", "200")) / 1000
INTERVALO = int(os.getenv("INTERVALO_S", "30"))
TETO = 1000

app = FastAPI(title="InterSCity falso")
recebidas = {"total": 0}


def _data(texto, padrao):
    if not texto:
        return padrao
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


@app.post("/adaptor/resources/{uuid}/data", status_code=201)
async def adaptor(uuid: str, corpo: dict):
    await asyncio.sleep(LATENCIA)
    recebidas["total"] += 1
    return {}


@app.get("/collector/resources/{uuid}/data/")
@app.get("/collector/resources/{uuid}/data")
async def collector(uuid: str, start_date: str | None = None, end_date: str | None = None):
    await asyncio.sleep(LATENCIA)
    agora = datetime.now(timezone.utc)
    inicio = _data(start_date, agora - timedelta(days=1))
    fim = min(_data(end_date, agora), agora)

    # medições a cada INTERVALO segundos, alinhadas no relógio
    t = datetime.fromtimestamp((int(inicio.timestamp()) // INTERVALO + 1) * INTERVALO, timezone.utc)
    temp, vibra = [], []
    while t <= fim and len(temp) + len(vibra) < TETO:
        d = t.isoformat()
        temp.append({"value": round(62 + random.gauss(0, 1.2), 2), "date": d})
        vibra.append({"value": round(2.4 + random.gauss(0, 0.25), 2), "date": d})
        t += timedelta(seconds=INTERVALO)

    return {"resources": [{"uuid": uuid, "capabilities": {
        "temperatura": temp, "vibração": vibra}}]}


@app.get("/stats")
async def stats():
    return recebidas
