# Monitoramento de Máquinas Industriais

Sistema distribuído que coleta temperatura e vibração de máquinas industriais,
envia os dados à plataforma **InterSCity** por meio de um **gateway IoT** e os
exibe num dashboard. O gateway e o dashboard rodam num cluster **Kubernetes**.

## Arquitetura

```
ESP32 / simulador ──HTTP POST──► Service "gateway" ──► gateway (2+ réplicas) ──► InterSCity
                                  (balanceamento)                                /adaptor
                                                                                    │
            navegador ◄── dashboard (Streamlit) ◄── InterSCity /collector ◄─────────┘

                        └──────────── cluster Kubernetes (kind) ────────────┘
```

| Componente | O que faz |
|---|---|
| **ESP32** ([esp32/monitoramento.ino](esp32/monitoramento.ino)) | Lê os sensores e faz `POST /telemetria` no gateway. Se falhar, tenta de novo. |
| **Simulador** ([simulador/esp32_sim.py](simulador/esp32_sim.py)) | Faz o papel da ESP32 quando não há hardware. |
| **Gateway** ([services/gateway/gateway.py](services/gateway/gateway.py)) | Recebe a leitura, traduz `maquina_id` para o `uuid` do recurso no InterSCity e envia ao **Resource Adaptor** (`POST /adaptor/resources/{uuid}/data`). Não guarda estado, então qualquer réplica atende qualquer requisição. |
| **Dashboard** ([services/dashboard/interface.py](services/dashboard/interface.py)) | Lê o histórico no **Data Collector** do InterSCity (`GET /collector/resources/{uuid}/data`) e mostra gráficos, alertas e exportação em CSV. |
| **InterSCity** | Middleware de cidades inteligentes da UFMA. Guarda os recursos e as medições. |

### Recursos de Kubernetes usados ([k8s/](k8s/))

- **ConfigMap** `interscity`: URL do InterSCity e o mapa máquina → uuid. Para mudar, basta editar sem rebuild.
- **Deployment** `gateway` com 2 réplicas espalhadas entre os nós, probes de liveness/readiness e rolling update com `maxUnavailable: 0`.
- **Service** `gateway` (NodePort): balanceia as requisições entre as réplicas.
- **HPA**: escala o gateway de 2 a 5 réplicas conforme a CPU.
- **Deployment + Service** `dashboard`.

### Comportamento em falhas

| Falha | O que acontece |
|---|---|
| Um pod do gateway morre | O Service passa a mandar tudo para a outra réplica e o Deployment recria o pod. |
| InterSCity fora do ar | O gateway tenta 3 vezes e responde `502`. O dispositivo reenvia depois. |
| Atualização de versão | O rolling update sobe o pod novo antes de derrubar o antigo, sem interromper o serviço. |
| Carga alta | O HPA cria mais réplicas do gateway. |

## Como rodar

### Pré-requisitos

Docker, [`kind`](https://kind.sigs.k8s.io/), `kubectl` e Python 3.10+ (para o simulador).

### Passo a passo

```bash
make cluster                      # cluster de 3 nós + metrics-server (~1 min)
make up                           # build das imagens, carga no kind e kubectl apply
make sim                          # simulador enviando ao gateway (cria o .venv na 1ª vez)
```

- Dashboard: <http://localhost:8080>
- Gateway: <http://localhost:8000/docs> (Swagger)

A saída do simulador mostra qual pod atendeu cada envio, o que deixa o balanceamento visível:

```
ok    maq-001 temp=61.8 vibra=2.31 pod=gateway-6d9f7c-abcde
ok    maq-002 temp=62.4 vibra=2.55 pod=gateway-6d9f7c-xyz12
```

### Demonstração de tolerância a falhas

Com o `make sim` rodando em outro terminal:

```bash
make drill        # mata um pod do gateway; o simulador continua com "ok"
make ps           # pods e HPA
make logs         # logs de todas as réplicas do gateway
```

Rolling update:

```bash
kubectl -n monitoramento rollout restart deployment/gateway
kubectl -n monitoramento rollout status  deployment/gateway
```

### Usando a ESP32 de verdade

Em [esp32/monitoramento.ino](esp32/monitoramento.ino), ajuste o Wi-Fi e `GATEWAY_URL`
com o IP do computador que roda o cluster (porta 8000). O `MAQUINA_ID` precisa
estar cadastrado no ConfigMap.

### Cadastrar outra máquina

1. Crie o recurso no InterSCity (Catalog) com as capabilities `temp` e `vibra` e anote o `uuid`.
2. Acrescente `maq-xxx=<uuid>` em `MAQUINAS` e `Nome=<uuid>` em `MAQUINAS_DASHBOARD`, no arquivo [k8s/00-namespace-config.yaml](k8s/00-namespace-config.yaml).
3. Rode `make deploy && kubectl -n monitoramento rollout restart deployment/gateway deployment/dashboard`.

### Encerrar

```bash
make down
```

## Estrutura

```
├── Makefile                   atalhos (cluster, up, sim, drill, ps, logs, down)
├── kind-cluster.yaml          cluster local: 1 control-plane + 2 workers
├── drill-gateway.sh           teste de falha: mata um pod do gateway
├── k8s/
│   ├── 00-namespace-config.yaml
│   ├── 10-gateway.yaml        Deployment + Service + HPA
│   └── 20-dashboard.yaml      Deployment + Service
├── services/
│   ├── gateway/               FastAPI: dispositivo → InterSCity
│   └── dashboard/             Streamlit: InterSCity → gráficos
├── simulador/esp32_sim.py
└── esp32/monitoramento.ino
```

## Tecnologias

Python, FastAPI, Streamlit, Pandas, Plotly, Docker, Kubernetes (kind), InterSCity, ESP32.
