#!/usr/bin/env bash
# Teste de carga AUTORIZADO pelo LSDi/UFMA contra o InterSCity real.
# Até 1000 req/s, o equivalente a ~5000 ESP32 enviando a cada 5 s.
#
#   ./testes/teste-autorizado.sh preparar    gateway com 4 réplicas + recursos de teste
#   ./testes/teste-autorizado.sh disparar    roda o k6 (pede confirmação)
#   ./testes/teste-autorizado.sh restaurar   volta o gateway ao normal
#
# Pré-requisitos: cluster no ar (make up) e recursos de teste criados
# (make teste-recursos, que grava testes/recursos-teste.env).
set -euo pipefail

NS=monitoramento
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
RECURSOS="$RAIZ/testes/recursos-teste.env"
K6=grafana/k6:0.54.0

carregar_recursos() {
  if [ ! -f "$RECURSOS" ]; then
    echo "Não encontrei $RECURSOS. Rode antes: make teste-recursos"
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$RECURSOS"
}

preparar() {
  carregar_recursos

  echo "== 1. Gateway apontando para o InterSCity real, com as máquinas de teste"
  local atuais
  atuais=$(kubectl -n "$NS" get configmap interscity -o jsonpath='{.data.MAQUINAS}')
  kubectl -n "$NS" set env deployment/gateway \
    "MAQUINAS=${atuais},${MAQUINAS_EXTRA}" LOG_LEVEL=WARNING INTERSCITY_URL-

  echo "== 2. Mínimo de 4 réplicas (o HPA ainda pode subir até 6)"
  kubectl -n "$NS" patch hpa gateway -p '{"spec":{"minReplicas":4,"maxReplicas":6}}'
  kubectl -n "$NS" scale deployment/gateway --replicas=4
  kubectl -n "$NS" rollout status deployment/gateway --timeout=180s
  kubectl -n "$NS" get pods -l app=gateway -o wide

  echo "== 3. Envio de conferência (1 leitura)"
  local primeira="${MAQUINAS_TESTE%%,*}"
  curl -s -X POST http://localhost:8000/telemetria \
    -H 'Content-Type: application/json' \
    -d "{\"maquina_id\":\"${primeira}\",\"temp\":60,\"vibra\":2}" \
    -w "  (HTTP %{http_code})\n"
  echo
  echo "Se apareceu HTTP 201 acima, está tudo pronto: make teste-autorizado"
}

disparar() {
  carregar_recursos
  cat <<EOF

  Vai enviar ATÉ 1000 req/s ao InterSCity REAL por cerca de 9 minutos
  (~260 mil requisições) nas máquinas de teste: ${MAQUINAS_TESTE}

  Degraus: 50 → 100 → 250 → 500 → 750 → 1000 req/s (1 min cada),
           1000 req/s sustentados por 2 min, depois desce.
  Freio:   para sozinho se erro > 2% ou p95 > 2 s (após 30 s).
           Ctrl+C também interrompe a qualquer momento.

EOF
  read -r -p "Digite SIM para disparar: " resposta
  [ "$resposta" = "SIM" ] || { echo "cancelado"; exit 1; }

  docker run --rm -i --network host --user "$(id -u):$(id -g)" \
    -e CENARIO=autorizado -e URL=http://localhost:8000 \
    -e MAQUINAS_TESTE="$MAQUINAS_TESTE" \
    -v "$RAIZ/testes:/testes" "$K6" run \
    --summary-export=/testes/resultado-gateway-autorizado.json \
    /testes/carga_gateway.js
}

restaurar() {
  kubectl -n "$NS" set env deployment/gateway MAQUINAS- LOG_LEVEL-
  kubectl -n "$NS" patch hpa gateway -p '{"spec":{"minReplicas":2,"maxReplicas":5}}'
  kubectl -n "$NS" rollout status deployment/gateway --timeout=180s
  echo "gateway restaurado (máquinas do ConfigMap, HPA 2–5)"
}

case "${1:-}" in
  preparar) preparar ;;
  disparar) disparar ;;
  restaurar) restaurar ;;
  *) echo "uso: $0 preparar|disparar|restaurar"; exit 1 ;;
esac
