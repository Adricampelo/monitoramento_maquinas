#!/usr/bin/env bash
# Derruba um pod do ingestor no meio da carga e confere se alguma leitura se perde.
#
#   ./drills/01-matar-ingestor.sh [maquina] [total] [hz]
#
# Espera-se: gravadas == total (o broker reentrega o que não foi confirmado).

set -euo pipefail

MAQUINA="${1:-maq-drill1}"
TOTAL="${2:-1000}"
HZ="${3:-50}"
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
NS=monitoramento

cd "$RAIZ"
set -a
. ./credenciais.env
set +a

echo "== publicando $TOTAL mensagens de $MAQUINA a $HZ Hz =="
"${PYTHON:-python3}" simulador/esp32_sim.py --host localhost --maquinas "$MAQUINA" --hz "$HZ" --total "$TOTAL" &
SIM=$!

sleep 7
ALVO="$(kubectl -n $NS get pod -l app=ingestor -o jsonpath='{.items[0].metadata.name}')"
echo "== matando $ALVO =="
kubectl -n $NS delete pod "$ALVO" --wait=false

wait "$SIM"
echo "== carga encerrada; esperando o cluster se recompor =="
kubectl -n $NS wait --for=condition=Ready pod -l app=ingestor --timeout=120s >/dev/null
sleep 12

kubectl -n $NS exec postgres-0 -- psql -U monitoramento -d monitoramento -tAc \
  "SELECT 'gravadas='||count(*)||' esperado=$TOTAL maior_seq='||max(seq)||' perdidas='||($TOTAL-count(*))
     FROM telemetria WHERE maquina_id='$MAQUINA';"
