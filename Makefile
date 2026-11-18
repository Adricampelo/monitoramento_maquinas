# Atalhos do ambiente Kubernetes local (kind).
#
#   make cluster   cria o cluster de 3 nós
#   make up        build das imagens + carga no kind + apply dos manifestos
#   make segredos  gera as senhas do MQTT/Postgres e cria os Secrets
#   make ps        estado dos pods
#   make down      apaga o cluster

CLUSTER   := monitoramento
IMAGENS   := ingestor api dashboard
REGISTRO  := monitoramento
TAG       := 0.1
NS        := monitoramento

.PHONY: cluster segredos build load deploy up ps down limpar

cluster:
	kind create cluster --config kind-cluster.yaml --image kindest/node:v1.31.4

segredos:
	./scripts/segredos.sh

build:
	@for s in $(IMAGENS); do \
		echo "== build $$s =="; \
		docker build -t $(REGISTRO)/$$s:$(TAG) services/$$s || exit 1; \
	done

load:
	kind load docker-image $(addprefix $(REGISTRO)/,$(addsuffix :$(TAG),$(IMAGENS))) --name $(CLUSTER)

deploy:
	kubectl apply -f k8s/

up: build load deploy
	@echo "== aguardando =="
	@kubectl -n $(NS) rollout status statefulset/postgres --timeout=180s
	@kubectl -n $(NS) rollout status statefulset/ingestor --timeout=180s
	@kubectl -n $(NS) rollout status deployment/api --timeout=180s
	@kubectl -n $(NS) get pods -o wide

ps:
	kubectl -n $(NS) get pods -o wide
	kubectl -n $(NS) get hpa

down:
	kind delete cluster --name $(CLUSTER)

limpar:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
