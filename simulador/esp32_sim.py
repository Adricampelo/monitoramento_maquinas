"""Simulador de ESP32: envia leituras ao gateway por HTTP, no mesmo contrato do firmware.

    python esp32_sim.py --url http://localhost:8000 --maquinas maq-001 maq-002

Cada linha mostra qual pod do gateway atendeu (balanceamento do Service).
"""

import argparse
import random
import time
from datetime import datetime, timezone

import requests

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://localhost:8000")
parser.add_argument("--maquinas", nargs="+", default=["maq-001", "maq-002"])
parser.add_argument("--intervalo", type=float, default=5.0, help="segundos entre envios")
parser.add_argument("--falha", type=float, default=0.0, help="probabilidade de valor acima do limite")
args = parser.parse_args()

print(f"enviando para {args.url}/telemetria | {args.maquinas} a cada {args.intervalo}s")

try:
    while True:
        for maquina in args.maquinas:
            excursao = random.random() < args.falha
            leitura = {
                "maquina_id": maquina,
                "temp": round(62 + random.gauss(0, 1.2) + (25 if excursao else 0), 2),
                "vibra": round(2.4 + random.gauss(0, 0.25) + (6 if excursao else 0), 2),
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            # tenta de novo se o gateway estiver fora (ex.: pod reiniciando)
            for tentativa in range(5):
                try:
                    r = requests.post(f"{args.url}/telemetria", json=leitura, timeout=15)
                    if r.ok:
                        print(f"ok    {maquina} temp={leitura['temp']} "
                              f"vibra={leitura['vibra']} pod={r.json()['pod']}")
                        break
                    print(f"erro  {maquina} HTTP {r.status_code}: {r.text[:120]}")
                except requests.RequestException as erro:
                    print(f"falha {maquina} ({erro.__class__.__name__}), tentando de novo")
                time.sleep(1 + tentativa)
        time.sleep(args.intervalo)
except KeyboardInterrupt:
    print("encerrado")
