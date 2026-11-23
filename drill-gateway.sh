#!/usr/bin/env bash
# Drill: mata um pod do gateway enquanto o simulador envia dados.
# Esperado: o Service passa a mandar tudo para a outra réplica, o
# Deployment recria o pod morto e o simulador segue com "ok".
set -euo pipefail
NS=monitoramento

echo "== pods antes =="
kubectl -n $NS get pods -l app=gateway -o wide

ALVO=$(kubectl -n $NS get pods -l app=gateway -o jsonpath='{.items[0].metadata.name}')
echo "== matando $ALVO =="
kubectl -n $NS delete pod "$ALVO" --wait=false

sleep 2
echo "== pods logo depois (um novo já está subindo) =="
kubectl -n $NS get pods -l app=gateway -o wide

kubectl -n $NS rollout status deployment/gateway --timeout=60s
echo "== recuperado =="
kubectl -n $NS get pods -l app=gateway -o wide
