"""Simulador de ESP32: publica telemetria em MQTT no mesmo contrato do firmware.

Use antes de ter o hardware na mão — o pipeline inteiro funciona igual.

    python esp32_sim.py --host localhost --maquinas maq-001 maq-002 --hz 2
"""

import argparse
import json
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt


def agora_iso():
    """ISO-8601 com milissegundos, como o ESP32 deve publicar."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

parser = argparse.ArgumentParser()
parser.add_argument("--host", default=os.getenv("MQTT_HOST", "localhost"))
parser.add_argument("--port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
parser.add_argument("--user", default=os.getenv("MQTT_USER", "sensor"))
parser.add_argument("--senha", default=os.getenv("MQTT_PASS", ""))
parser.add_argument("--maquinas", nargs="+", default=["maq-001", "maq-002"])
parser.add_argument("--hz", type=float, default=1.0, help="mensagens por segundo por máquina")
parser.add_argument("--total", type=int, default=0, help="encerra após N mensagens por máquina (0 = infinito)")
parser.add_argument("--temp-base", type=float, default=62.0)
parser.add_argument("--vibra-base", type=float, default=2.4)
parser.add_argument("--falha", type=float, default=0.0, help="probabilidade de excursão acima do limite")
args = parser.parse_args()

rodando = True


def parar(*_):
    global rodando
    rodando = False


signal.signal(signal.SIGINT, parar)
signal.signal(signal.SIGTERM, parar)

cliente = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{os.getpid()}")
cliente.username_pw_set(args.user, args.senha)
cliente.connect(args.host, args.port, keepalive=30)
cliente.loop_start()

print(f"publicando em {args.host}:{args.port} | {args.maquinas} @ {args.hz} Hz")

seq = {m: 0 for m in args.maquinas}
intervalo = 1.0 / args.hz if args.hz > 0 else 1.0
proxima = time.monotonic()

try:
    while rodando:
        for maquina in args.maquinas:
            seq[maquina] += 1
            excursao = random.random() < args.falha
            temp = args.temp_base + random.gauss(0, 1.2) + (25 if excursao else 0)
            vibra = args.vibra_base + random.gauss(0, 0.25) + (6 if excursao else 0)

            payload = {
                "maquina_id": maquina,
                "seq": seq[maquina],
                "ts": agora_iso(),
                "temp": round(temp, 2),
                "vibra": round(vibra, 2),
                "fw": "sim-1.0",
            }
            info = cliente.publish(
                f"maquinas/{maquina}/telemetria",
                json.dumps(payload),
                qos=1,
            )
            if seq[maquina] % 30 == 0:
                info.wait_for_publish(timeout=2)
                print(f"{maquina} seq={seq[maquina]} temp={temp:.1f} vibra={vibra:.2f}")

        if args.total and all(seq[m] >= args.total for m in args.maquinas):
            print(f"total atingido: {args.total} por máquina")
            break

        proxima += intervalo
        espera = proxima - time.monotonic()
        if espera > 0:
            time.sleep(espera)
        else:
            proxima = time.monotonic()

except Exception as erro:  # noqa: BLE001
    print(f"erro: {erro}", file=sys.stderr)
finally:
    cliente.loop_stop()
    cliente.disconnect()
    print("encerrado")
