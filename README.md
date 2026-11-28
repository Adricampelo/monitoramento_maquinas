# Monitoramento de Máquinas Industriais

Sistema distribuído que coleta temperatura e vibração de máquinas industriais,
envia os dados à plataforma **InterSCity** por meio de um **gateway IoT** e os
exibe num dashboard. Gateway, fila (NATS JetStream), encaminhador e dashboard
rodam num cluster **Kubernetes**.

## Arquitetura

```
ESP32 / simulador ──POST──► gateway (2–8) ──► NATS JetStream ──► encaminhador (2–6) ──► InterSCity
                    202 ◄── valida e grava     3 nós, stream R3    agrupa por máquina,      /adaptor
                            na fila            (fila durável)      1 POST por lote             │
                                                                                               │
            navegador ◄── dashboard (Streamlit) ◄── InterSCity /collector ◄────────────────────┘

                        └──────────────── cluster Kubernetes (kind) ────────────────┘
```

O gateway responde assim que a leitura está gravada na fila, sem esperar o
InterSCity. Por isso a latência do dispositivo não depende do servidor externo,
e uma lentidão ou queda do InterSCity só faz a fila crescer: nada se perde e
nada volta como erro ao ESP32. O `202` quer dizer "aceito", não "gravado no
InterSCity" (consistência eventual).

| Componente | O que faz |
|---|---|
| **ESP32** ([esp32/monitoramento.ino](esp32/monitoramento.ino)) | Lê os sensores e faz `POST /telemetria` no gateway. Se falhar, tenta de novo. |
| **Simulador** ([simulador/esp32_sim.py](simulador/esp32_sim.py)) | Faz o papel da ESP32 quando não há hardware. |
| **Gateway** ([services/gateway/gateway.py](services/gateway/gateway.py)) | Recebe a leitura, confere se a máquina está cadastrada, publica no stream `TELEMETRIA` do NATS e responde `202` quando a maioria das réplicas do stream confirmou a gravação. Fila cheia ou sem quórum: `503` e o dispositivo reenvia. Não guarda estado. |
| **NATS JetStream** ([k8s/05-nats.yaml](k8s/05-nats.yaml)) | Fila durável em cluster de 3 servidores (Raft). O stream é replicado nos 3 (R3) e em modo *work queue*: a leitura sai da fila quando o encaminhador confirma a entrega. |
| **Encaminhador** ([services/encaminhador/encaminhador.py](services/encaminhador/encaminhador.py)) | Consome a fila em lote (janela de 1 s), agrupa por `uuid` e faz **um** `POST /adaptor/resources/{uuid}/data` com até 500 leituras. Limita os POSTs simultâneos (`MAX_EM_VOO`) para proteger o InterSCity. Se o POST falhar, as leituras voltam à fila e são reentregues (entrega "pelo menos uma vez"). |
| **Dashboard** ([services/dashboard/interface.py](services/dashboard/interface.py)) | Lê o histórico no **Data Collector** do InterSCity (`GET /collector/resources/{uuid}/data`) e mostra gráficos, alertas e exportação em CSV. |
| **InterSCity** | Middleware de cidades inteligentes da UFMA. Guarda os recursos e as medições. |

### Recursos de Kubernetes usados ([k8s/](k8s/))

- **ConfigMap** `interscity`: URL do InterSCity e o mapa máquina → uuid. Para mudar, basta editar sem rebuild.
- **StatefulSet** `nats` com 3 réplicas, volume persistente por réplica e Service headless (endereço estável de cada servidor do cluster).
- **Deployment** `gateway` com 2 réplicas espalhadas entre os nós, probes de liveness/readiness e rolling update com `maxUnavailable: 0`.
- **Service** `gateway` (NodePort): balanceia as requisições entre as réplicas.
- **Deployment** `encaminhador`: várias réplicas dividem o mesmo consumidor durável, então cada leitura vai a uma só réplica.
- **HPA**: escala o gateway de 2 a 8 réplicas (alvo de 50% de CPU) e o dashboard de 2 a 4.
- **KEDA ScaledObject**: escala o encaminhador de 2 a 6 réplicas pelo **tamanho da fila**, e não por CPU. O encaminhador passa o tempo esperando o InterSCity, então a CPU dele não mostra o atraso.
- **Deployment + Service** `dashboard` com 2 réplicas e `sessionAffinity: ClientIP` (o Streamlit guarda a sessão na memória do pod, então cada usuário fica sempre no mesmo pod).

### Comportamento em falhas

| Falha | O que acontece |
|---|---|
| Um pod do gateway morre | O Service passa a mandar tudo para a outra réplica e o Deployment recria o pod. |
| Um servidor do NATS cai | Os outros 2 mantêm o quórum e o stream R3 continua aceitando e entregando. |
| Um encaminhador cai no meio de um lote | As leituras sem confirmação voltam à fila após 60 s e outra réplica as entrega (podem ficar duplicadas no InterSCity). |
| InterSCity lento ou fora do ar | A fila cresce, o gateway continua respondendo `202` e o encaminhador reenvia com atraso. Só com a fila cheia (1 GB) o gateway responde `503`. |
| Atualização de versão | O rolling update sobe o pod novo antes de derrubar o antigo, sem interromper o serviço. |
| Carga alta | O HPA cria mais gateways (CPU) e o KEDA cria mais encaminhadores (fila). |

## Como rodar

### Pré-requisitos

Docker, [`kind`](https://kind.sigs.k8s.io/), `kubectl` e Python 3.10+ (para o simulador).

### Passo a passo

```bash
make cluster                      # cluster de 3 nós + metrics-server + KEDA (~2 min)
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

### Testes de carga

Para medir quantos envios e quantos usuários o sistema aguenta, contra um InterSCity
falso (para não sobrecarregar o servidor da UFMA), siga o
[plano de testes](testes/PLANO_DE_TESTES.md):

```bash
make teste-preparar      # sobe o InterSCity falso e aponta os serviços para ele
make teste-gateway       # k6: envios por segundo
make teste-dashboard     # Playwright: usuários simultâneos
make teste-restaurar     # volta para o InterSCity real
```

#### Testes com o InterSCity real

Estes testes gravam leituras de verdade no servidor da UFMA. Rode `make teste-restaurar`
antes, caso tenha usado o InterSCity falso.

**Carga leve** (até 20 req/s, cerca de 100 ESP32, com freio automático):

```bash
make teste-real
```

**Teste autorizado** (até 1000 req/s, cerca de 5000 ESP32, ~9 min), autorizado pelo LSDi/UFMA:

```bash
make teste-recursos               # cria os recursos de teste no Catalog (uma vez só)
make teste-autorizado-preparar    # 4+ réplicas do gateway; deve terminar com HTTP 202
make teste-monitorar              # em outro terminal: fila, réplicas, CPU (grava CSV)
make teste-autorizado             # dispara (digite SIM para confirmar)
make teste-autorizado-restaurar   # volta ao normal (HPA do gateway 2–8)
```

O teste para sozinho se o erro passar de 2% ou o p95 passar de 2 s, e o Ctrl+C
interrompe a qualquer momento. Como o gateway responde ao gravar na fila, o k6
mede a **recepção**. A **entrega** se vê no `teste-monitorar` (`entregues_por_s`
e `fila_pendente`): o sistema acompanha a carga se a fila não cresce sem parar.
Resultado do último teste em [testes/RELATORIO_TESTE_AUTORIZADO.md](testes/RELATORIO_TESTE_AUTORIZADO.md).

### Cadastrar outra máquina

1. Crie o recurso no InterSCity (Catalog) com as capabilities `temperatura` e `vibração` e anote o `uuid`.
2. Acrescente `maq-xxx=<uuid>` em `MAQUINAS` e `Nome=<uuid>` em `MAQUINAS_DASHBOARD`, no arquivo [k8s/00-namespace-config.yaml](k8s/00-namespace-config.yaml).
3. Rode `make deploy && kubectl -n monitoramento rollout restart deployment/gateway deployment/encaminhador deployment/dashboard`.

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
│   ├── 05-nats.yaml           StatefulSet do NATS JetStream (3 nós)
│   ├── 10-gateway.yaml        Deployment + Service + HPA
│   ├── 15-encaminhador.yaml   Deployment + ScaledObject (KEDA)
│   └── 20-dashboard.yaml      Deployment + Service
├── services/
│   ├── gateway/               FastAPI: dispositivo → fila
│   ├── encaminhador/          fila → InterSCity, em lote
│   └── dashboard/             Streamlit: InterSCity → gráficos
├── testes/                    plano, scripts de carga e InterSCity falso
├── simulador/esp32_sim.py
└── esp32/monitoramento.ino
```

## Tecnologias

Python, FastAPI, NATS JetStream, KEDA, Streamlit, Pandas, Plotly, Docker, Kubernetes (kind), InterSCity, ESP32.
