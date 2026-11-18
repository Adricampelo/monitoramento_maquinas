"""Ingestor: assina a telemetria MQTT e persiste no Postgres.

Garantias:
- QoS 1 com ack manual: só confirma ao broker depois que o lote foi gravado.
  Se o pod cair antes disso, o broker reentrega e o ON CONFLICT descarta a
  duplicata (idempotência por (maquina_id, seq)).
- Shared subscription ($share/<grupo>/<tópico>): o broker distribui as
  mensagens entre as réplicas, em vez de cada réplica receber tudo.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt
import psycopg

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("ingestor")

MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "sensor")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "maquinas/+/telemetria")
SHARE_GROUP = os.getenv("MQTT_SHARE_GROUP", "ingestores")
DSN = os.environ["POSTGRES_DSN"]

BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
FLUSH_INTERVAL_S = float(os.getenv("FLUSH_INTERVAL_S", "1.0"))

INSERT = """
INSERT INTO telemetria
    (maquina_id, seq, recebido_em, ts_dispositivo, temp, vibra, fw)
VALUES (%s, %s, now(), %s, %s, %s, %s)
ON CONFLICT (maquina_id, seq) DO NOTHING
"""

estado = {
    "mqtt_ok": False,
    "db_ok": False,
    "ultimo_commit": 0.0,
    "gravados": 0,
    "duplicados": 0,
    "invalidos": 0,
}
_lock = threading.Lock()
_db_lock = threading.RLock()   # a conexão psycopg não é segura entre threads
_buffer = []  # [(row, (mid, qos)), ...]


# ---------------------------------------------------------------- Postgres
def conectar_db():
    while True:
        try:
            # autocommit: cada `with conn.transaction()` emite BEGIN/COMMIT de
            # verdade. Sem isso, um SELECT solto abre transação implícita e os
            # lotes seguintes viram savepoints que nunca são commitados.
            conn = psycopg.connect(DSN, autocommit=True, connect_timeout=5)
            LOG.info("conectado ao Postgres")
            estado["db_ok"] = True
            return conn
        except psycopg.Error as erro:
            estado["db_ok"] = False
            LOG.warning("Postgres indisponível (%s); tentando de novo em 3s", erro)
            time.sleep(3)


conn = conectar_db()


def gravar(linhas):
    """Grava o lote e devolve quantas linhas entraram de fato."""
    with _db_lock:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(INSERT, linhas)
                return cur.rowcount


# ---------------------------------------------------------------- MQTT
def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        LOG.error("falha ao conectar no broker: %s", reason_code)
        return
    topico = f"$share/{SHARE_GROUP}/{MQTT_TOPIC}"
    client.subscribe(topico, qos=1)
    estado["mqtt_ok"] = True
    LOG.info("assinando %s (qos 1)", topico)


def on_disconnect(client, userdata, flags, reason_code, properties=None):
    estado["mqtt_ok"] = False
    LOG.warning("desconectado do broker (%s); o paho reconecta sozinho", reason_code)


def on_message(client, userdata, msg):
    partes = msg.topic.split("/")
    if len(partes) < 3:
        client.ack(msg.mid, msg.qos)
        return

    try:
        dado = json.loads(msg.payload)
        maquina_id = dado.get("maquina_id") or partes[1]
        seq = int(dado["seq"])
        linha = (
            maquina_id,
            seq,
            dado.get("ts"),
            dado.get("temp"),
            dado.get("vibra"),
            dado.get("fw"),
        )
        float(linha[3]) if linha[3] is not None else None
        float(linha[4]) if linha[4] is not None else None
    except (ValueError, KeyError, TypeError) as erro:
        estado["invalidos"] += 1
        LOG.warning("payload inválido em %s: %s (%s)", msg.topic, msg.payload[:200], erro)
        client.ack(msg.mid, msg.qos)  # não adianta reentregar
        return

    with _lock:
        _buffer.append((linha, (msg.mid, msg.qos)))
        pronto = len(_buffer) >= BATCH_SIZE
    if pronto:
        drenar(client)


def drenar(client):
    """Grava o buffer; só dá ack no broker depois do commit."""
    global conn
    with _lock:
        lote = list(_buffer)
        _buffer.clear()
    if not lote:
        return

    try:
        gravados = gravar([linha for linha, _ in lote])
    except psycopg.Error as erro:
        LOG.error("falha ao gravar %d linhas (%s); devolvendo ao buffer", len(lote), erro)
        with _lock:
            _buffer[:0] = lote
        with _db_lock:
            try:
                conn.close()
            except psycopg.Error:
                pass
            conn = conectar_db()
        return

    for mid, qos in (ack for _, ack in lote):
        client.ack(mid, qos)

    estado["gravados"] += gravados
    estado["duplicados"] += len(lote) - gravados
    estado["ultimo_commit"] = time.time()
    estado["db_ok"] = True
    LOG.info(
        "lote de %d gravado (%d novas, %d duplicadas)",
        len(lote), gravados, len(lote) - gravados,
    )


def verificar_banco():
    """Ping periódico: mantém a readiness honesta mesmo sem tráfego."""
    global conn
    with _db_lock:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            estado["db_ok"] = True
        except psycopg.Error as erro:
            estado["db_ok"] = False
            LOG.error("banco inacessível no ping: %s", erro)
            try:
                conn.close()
            except psycopg.Error:
                pass
            conn = conectar_db()


# ---------------------------------------------------------------- health
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            corpo, codigo = b"ok", 200
        elif self.path == "/readyz":
            # pronto = consegue consumir (broker + banco), não "está recebendo".
            # Planta parada não é falha.
            pronto = estado["mqtt_ok"] and estado["db_ok"]
            corpo, codigo = json.dumps(estado).encode(), (200 if pronto else 503)
        elif self.path == "/metrics":
            corpo = json.dumps(estado).encode()
            codigo = 200
        else:
            corpo, codigo = b"nao encontrado", 404
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(corpo)

    def log_message(self, *args):
        pass


def servir_health():
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Health).serve_forever()


# ---------------------------------------------------------------- main
def main():
    threading.Thread(target=servir_health, daemon=True).start()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"ingestor-{os.getenv('HOSTNAME', 'local')}",
        clean_session=False,  # sessão durável: broker guarda o que não foi entregue
        manual_ack=True,      # o PUBACK só sai depois do commit no Postgres
    )
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.reconnect_delay_set(min_delay=1, max_delay=15)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    def encerrar(*_):
        LOG.info("SIGTERM: drenando o buffer antes de sair")
        drenar(client)
        sys.exit(0)

    signal.signal(signal.SIGTERM, encerrar)

    ultimo_ping = 0.0

    try:
        while True:
            time.sleep(FLUSH_INTERVAL_S)
            drenar(client)
            if time.time() - ultimo_ping >= 10:
                verificar_banco()
                ultimo_ping = time.time()
    finally:
        client.loop_stop()


if __name__ == "__main__":
    main()
