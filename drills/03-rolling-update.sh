#!/usr/bin/env bash
# Atualização rolante do ingestor no meio da carga.
#
#   ./drills/03-rolling-update.sh [maquina] [total] [hz]
#
# Espera-se zero perda: SIGTERM drena o buffer, o broker reentrega o resto.

set -euo pipefail

MAQUINA="${1:-maq-drill4}"
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

sleep 4
echo "== rollout restart =="
kubectl -n $NS rollout restart statefulset ingestor
kubectl -n $NS rollout status statefulset ingestor --timeout=180s

wait "$SIM"
sleep 15

kubectl -n $NS exec postgres-0 -- psql -U monitoramento -d monitoramento -tAc \
  "SELECT 'gravadas='||count(*)||' esperado=$TOTAL maior_seq='||max(seq)||' perdidas='||($TOTAL-count(*))
     FROM telemetria WHERE maquina_id='$MAQUINA';"
