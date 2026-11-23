# Atalhos do ambiente Kubernetes local (kind).
#
#   make cluster   cria o cluster de 3 nós (+ metrics-server para o HPA)
#   make up        build das imagens + carga no kind + apply dos manifestos
#   make sim       roda o simulador de ESP32 enviando ao gateway
#   make drill     mata um pod do gateway com o simulador rodando
#   make ps        estado dos pods
#   make down      apaga o cluster

CLUSTER := monitoramento
IMAGENS := gateway dashboard
TAG     := 0.1
NS      := monitoramento

.PHONY: cluster build load deploy up sim drill ps logs down

cluster:
	kind create cluster --config kind-cluster.yaml --image kindest/node:v1.31.4
	kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
	kubectl -n kube-system patch deployment metrics-server --type=json \
		-p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'

build:
	@for s in $(IMAGENS); do \
		docker build -t monitoramento/$$s:$(TAG) services/$$s || exit 1; \
	done

load:
	kind load docker-image $(foreach s,$(IMAGENS),monitoramento/$(s):$(TAG)) --name $(CLUSTER)

deploy:
	kubectl apply -f k8s/

up: build load deploy
	kubectl -n $(NS) rollout status deployment/gateway --timeout=120s
	kubectl -n $(NS) rollout status deployment/dashboard --timeout=120s
	kubectl -n $(NS) get pods -o wide

# cria o ambiente virtual do simulador na primeira execução
.venv/bin/python:
	python3 -m venv .venv
	.venv/bin/pip install -q -r simulador/requirements.txt

sim: .venv/bin/python
	.venv/bin/python simulador/esp32_sim.py --url http://localhost:8000

drill:
	./drill-gateway.sh

ps:
	kubectl -n $(NS) get pods -o wide
	kubectl -n $(NS) get hpa

logs:
	kubectl -n $(NS) logs -l app=gateway -f --prefix

down:
	kind delete cluster --name $(CLUSTER)
