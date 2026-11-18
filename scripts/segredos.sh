#!/usr/bin/env bash
# Gera as senhas do broker e do Postgres e cria os Secrets no cluster.
# Idempotente: se os Secrets já existem, não mexe em nada.
#
# Grava as credenciais em credenciais.env (fora do git) para o simulador usar.

set -euo pipefail

NS=monitoramento
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RAIZ"

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

if kubectl -n "$NS" get secret mqtt-creds >/dev/null 2>&1; then
  echo "Secrets já existem em $NS; reconstruindo credenciais.env a partir deles."
  SENHA_MQTT="$(kubectl -n "$NS" get secret mqtt-creds -o jsonpath='{.data.senha}' | base64 -d)"
  SENHA_PG="$(kubectl -n "$NS" get secret postgres-creds -o jsonpath='{.data.senha}' | base64 -d)"
  umask 077
  printf 'MQTT_USER=sensor\nMQTT_PASS=%s\nPGPASS=%s\n' "$SENHA_MQTT" "$SENHA_PG" > credenciais.env
  umask 022
  echo "credenciais.env atualizado."
  exit 0
fi

SENHA_MQTT="$(openssl rand -hex 12)"
SENHA_PG="$(openssl rand -hex 12)"

# O password_file do mosquitto precisa vir do mosquitto_passwd
docker run --rm -u "$(id -u):$(id -g)" -v "$RAIZ":/w \
  --entrypoint mosquitto_passwd eclipse-mosquitto:2 \
  -b -c /w/pwfile sensor "$SENHA_MQTT" >/dev/null

kubectl -n "$NS" create secret generic mqtt-creds \
  --from-file=pwfile=./pwfile --from-literal=senha="$SENHA_MQTT"

DSN="postgresql://monitoramento:${SENHA_PG}"
DSN="${DSN}@postgres:5432/monitoramento"

kubectl -n "$NS" create secret generic postgres-creds \
  --from-literal=senha="$SENHA_PG" \
  --from-literal=dsn="$DSN"

rm -f pwfile
umask 077
printf 'MQTT_USER=sensor\nMQTT_PASS=%s\nPGPASS=%s\n' "$SENHA_MQTT" "$SENHA_PG" > credenciais.env
umask 022

echo "Secrets criados. Credenciais locais em credenciais.env (não comitar)."
