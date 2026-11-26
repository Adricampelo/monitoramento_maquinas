"""Cria no Catalog do InterSCity os recursos usados no teste de carga autorizado.

Assim as centenas de milhares de leituras do teste ficam em recursos próprios
("Teste de carga NN") e não misturam com o histórico das máquinas reais.

    python3 testes/criar_recursos_teste.py            # cria 2 recursos
    python3 testes/criar_recursos_teste.py --qtd 4

Grava os UUIDs em testes/recursos-teste.env. Se o arquivo já existir, não
cria nada de novo (reaproveita os recursos de um teste anterior).
"""

import argparse
import json
import os
import sys
import urllib.request

ARQUIVO = os.path.join(os.path.dirname(__file__), "recursos-teste.env")

parser = argparse.ArgumentParser()
parser.add_argument("--url", default=os.getenv(
    "INTERSCITY_URL", "https://cidadesinteligentes.lsdi.ufma.br/interscity_lh"))
parser.add_argument("--qtd", type=int, default=2)
args = parser.parse_args()

if os.path.exists(ARQUIVO):
    print(f"{ARQUIVO} já existe; reaproveitando os recursos:")
    print(open(ARQUIVO).read())
    sys.exit(0)

pares = []
for i in range(1, args.qtd + 1):
    corpo = {"data": {
        "description": f"Teste de carga {i:02d}",
        "capabilities": ["temperatura", "vibração"],
        "status": "active",
        "lat": -2.5583, "lon": -44.3097,   # São Luís (obrigatório no Catalog)
    }}
    req = urllib.request.Request(
        args.url.rstrip("/") + "/catalog/resources",
        data=json.dumps(corpo).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resposta = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"erro ao criar o recurso {i}: HTTP {e.code} {e.read()[:300]!r}")
        sys.exit(1)

    uuid = resposta["data"]["uuid"]
    pares.append((f"maq-teste-{i:02d}", uuid))
    print(f"criado: maq-teste-{i:02d} -> {uuid}")

with open(ARQUIVO, "w") as f:
    f.write("# Recursos de teste no Catalog (gerado por criar_recursos_teste.py)\n")
    f.write("MAQUINAS_EXTRA=" + ",".join(f"{m}={u}" for m, u in pares) + "\n")
    f.write("MAQUINAS_TESTE=" + ",".join(m for m, _ in pares) + "\n")
print(f"gravado em {ARQUIVO}")
