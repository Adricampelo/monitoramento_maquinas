# Atalhos do ambiente Kubernetes local (kind).
#
#   make cluster   cria o cluster de 3 nós (+ metrics-server para o HPA)
#   make up        build das imagens + carga no kind + apply dos manifestos
#   make sim       roda o simulador de ESP32 enviando ao gateway
#   make drill     mata um pod do gateway com o simulador rodando
#   make ps        estado dos pods
#   make down      apaga o cluster
#
# Testes de carga (ver testes/PLANO_DE_TESTES.md):
#   make teste-preparar   sobe o InterSCity falso e aponta gateway/dashboard para ele
#   make teste-gateway    carga de envios (k6)          [CENARIO=vazao|simultaneos]
#   make teste-dashboard  carga de usuários (Playwright) [USUARIOS="5 10 20 40 80"]
#   make teste-monitorar  pods, HPA e uso de CPU/memória ao vivo
#   make teste-restaurar  volta para o InterSCity real
#   make teste-real       carga LEVE no InterSCity real (até 20 req/s, com freio)
#
# Teste AUTORIZADO (até 1000 req/s ≈ 5000 ESP32), nesta ordem:
#   make teste-recursos              cria recursos de teste no Catalog (uma vez)
#   make teste-autorizado-preparar   gateway com 4+ réplicas e os recursos de teste
#   make teste-autorizado            dispara (pede confirmação)
#   make teste-autorizado-restaurar  volta o gateway ao normal

CLUSTER := monitoramento
IMAGENS := gateway dashboard
TAG     := 0.1
NS      := monitoramento

.PHONY: cluster build load deploy up sim drill ps logs down \
	teste-preparar teste-gateway teste-dashboard teste-monitorar teste-restaurar teste-real \
	teste-recursos teste-autorizado-preparar teste-autorizado teste-autorizado-restaurar

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

# ---------------------------------------------------------------- testes de carga
MOCK     := http://mock-interscity:8000
CENARIO  ?= vazao
USUARIOS ?= 5 10 20 40 80
K6       := grafana/k6:0.54.0

teste-preparar:
	docker build -t monitoramento/mock-interscity:$(TAG) testes/mock-interscity
	kind load docker-image monitoramento/mock-interscity:$(TAG) --name $(CLUSTER)
	kubectl apply -f testes/mock-interscity.yaml
	kubectl -n $(NS) rollout status deployment/mock-interscity --timeout=120s
	kubectl -n $(NS) set env deployment/gateway INTERSCITY_URL=$(MOCK) LOG_LEVEL=WARNING
	kubectl -n $(NS) set env deployment/dashboard INTERSCITY_URL=$(MOCK)
	kubectl -n $(NS) rollout status deployment/gateway --timeout=120s
	kubectl -n $(NS) rollout status deployment/dashboard --timeout=120s

teste-gateway:
	docker run --rm -i --network host --user $$(id -u):$$(id -g) -e CENARIO=$(CENARIO) -e URL=http://localhost:8000 \
		-v $(CURDIR)/testes:/testes $(K6) run --summary-export=/testes/resultado-gateway-$(CENARIO).json \
		/testes/carga_gateway.js

.venv-testes/bin/python:
	python3 -m venv .venv-testes
	.venv-testes/bin/pip install -q playwright==1.49.1
	.venv-testes/bin/playwright install chromium

teste-dashboard: .venv-testes/bin/python
	.venv-testes/bin/python testes/carga_dashboard.py --usuarios $(USUARIOS)

teste-monitorar:
	watch -n 2 'kubectl -n $(NS) get hpa; echo; kubectl -n $(NS) top pods; echo; kubectl -n $(NS) get pods -o wide'

teste-restaurar:
	kubectl -n $(NS) set env deployment/gateway INTERSCITY_URL- LOG_LEVEL-
	kubectl -n $(NS) set env deployment/dashboard INTERSCITY_URL-
	kubectl delete -f testes/mock-interscity.yaml --ignore-not-found

# Usa o gateway do cluster apontando para o InterSCity REAL (rode antes o
# teste-restaurar se tiver usado o falso). Grava leituras de teste no histórico.
teste-real:
	$(MAKE) teste-gateway CENARIO=real

# ---------------------------------------------------------------- teste autorizado
# Até 1000 req/s no InterSCity REAL (autorizado pelo LSDi/UFMA).
# Roteiro completo em testes/PLANO_DE_TESTES.md, seção 9.
teste-recursos:
	python3 testes/criar_recursos_teste.py

teste-autorizado-preparar:
	./testes/teste-autorizado.sh preparar

teste-autorizado:
	./testes/teste-autorizado.sh disparar

teste-autorizado-restaurar:
	./testes/teste-autorizado.sh restaurar
