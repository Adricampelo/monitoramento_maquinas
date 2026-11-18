#!/usr/bin/env bash
# Apagão total: zera as réplicas do ingestor no meio da carga e devolve depois.
#
#   ./drills/02-apagao-ingestor.sh [maquina] [total] [hz]
#
# Mede o quanto o broker segura enquanto não há consumidor nenhum.

set -euo pipefail

MAQUINA="${1:-maq-drill3}"
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

sleep 3
echo "== replicas -> 0 =="
kubectl -n $NS scale statefulset ingestor --replicas=0
kubectl -n $NS wait --for=delete pod -l app=ingestor --timeout=60s >/dev/null
sleep 6
echo "== replicas -> 2 =="
kubectl -n $NS scale statefulset ingestor --replicas=2

wait "$SIM"
kubectl -n $NS wait --for=condition=Ready pod -l app=ingestor --timeout=120s >/dev/null
sleep 20

kubectl -n $NS exec postgres-0 -- psql -U monitoramento -d monitoramento -tAc \
  "SELECT 'gravadas='||count(*)||' esperado=$TOTAL maior_seq='||max(seq)||' perdidas='||($TOTAL-count(*))
     FROM telemetria WHERE maquina_id='$MAQUINA';"
