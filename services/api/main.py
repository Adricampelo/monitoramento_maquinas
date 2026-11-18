"""API de leitura: consultas sobre a telemetria gravada no Postgres."""

import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg_pool import ConnectionPool

DSN = os.environ["POSTGRES_DSN"]

pool: ConnectionPool


@asynccontextmanager
async def lifespan(app):
    global pool
    pool = ConnectionPool(DSN, min_size=1, max_size=4, open=True, timeout=5)
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Monitoramento Industrial", version="1.0.0", lifespan=lifespan)

CAPACIDADES = ("temp", "vibra")


def checar_db():
    with pool.connection(timeout=3) as conn:
        conn.execute("SELECT 1")


def healthz():
    return {"status": "ok"}


def readyz():
    try:
        checar_db()
    except Exception as erro:  # noqa: BLE001
        return JSONResponse({"status": "sem banco", "erro": str(erro)}, status_code=503)
    return {"status": "ok"}


def maquinas(limite_offline_min: int = 10):
    """Última leitura de cada máquina e se ela está online."""
    sql = """
        SELECT DISTINCT ON (maquina_id)
               maquina_id,
               recebido_em,
               temp,
               vibra
          FROM telemetria
         ORDER BY maquina_id, recebido_em DESC
    """
    with pool.connection() as conn:
        linhas = conn.execute(sql).fetchall()

    agora = datetime.now(timezone.utc)
    saida = []
    for maquina_id, recebido_em, temp, vibra in linhas:
        minutos = (agora - recebido_em).total_seconds() / 60
        saida.append({
            "maquina_id": maquina_id,
            "ultima_medicao": recebido_em.isoformat(),
            "minutos_sem_dados": round(minutos, 1),
            "online": minutos <= limite_offline_min,
            "temp": temp,
            "vibra": vibra,
        })
    return {"maquinas": saida}


def telemetria(
    maquina_id: str,
    horas: float = Query(24, gt=0, le=24 * 90),
    limite: int = Query(5000, gt=0, le=50000),
):
    """Série temporal da máquina, em formato largo (Data, temp, vibra)."""
    desde = datetime.now(timezone.utc) - timedelta(hours=horas)
    sql = """
        SELECT recebido_em, temp, vibra
          FROM telemetria
         WHERE maquina_id = %s
           AND recebido_em >= %s
         ORDER BY recebido_em
         LIMIT %s
    """
    with pool.connection() as conn:
        linhas = conn.execute(sql, (maquina_id, desde, limite)).fetchall()

    if not linhas:
        raise HTTPException(404, f"sem medições para '{maquina_id}' nas últimas {horas}h")

    return {
        "maquina_id": maquina_id,
        "capacidades": CAPACIDADES,
        "medicoes": [
            {"Data": recebido_em.isoformat(), "temp": temp, "vibra": vibra}
            for recebido_em, temp, vibra in linhas
        ],
    }


app.add_api_route("/healthz", healthz, methods=["GET"])
app.add_api_route("/readyz", readyz, methods=["GET"])
app.add_api_route("/maquinas", maquinas, methods=["GET"])
app.add_api_route("/telemetria/{maquina_id}", telemetria, methods=["GET"])
