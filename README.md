# Monitoramento de Máquinas Industriais

Sistema de monitoramento de temperatura e vibração de máquinas industriais, com
telemetria enviada por ESP32 e processamento distribuído em Kubernetes.

O projeto tem duas partes:

1. **Sistema distribuído** (principal) — recebe telemetria via MQTT, grava em
   PostgreSQL e serve um dashboard, tudo rodando em pods que sobrevivem a falhas.
2. **Dashboard original** — `interface.py` lendo dados da plataforma
   InterSCity. Continua funcionando e está documentado no fim deste arquivo.

Guia técnico completo, com as decisões justificadas e os experimentos de falha
medidos: [`docs/guia-kubernetes.md`](docs/guia-kubernetes.md).

**Sumário**

- [1. O que o projeto é](#1-o-que-o-projeto-é)
- [2. O caminho de uma leitura](#2-o-caminho-de-uma-leitura)
- [3. Por que cada peça existe](#3-por-que-cada-peça-existe)
- [4. As cinco ideias de sistema distribuído](#4-as-cinco-ideias-de-sistema-distribuído)
- [5. O que acontece quando algo quebra](#5-o-que-acontece-quando-algo-quebra)
- [6. Como iniciar](#6-como-iniciar)
- [7. Escada de verificação](#7-escada-de-verificação)
- [8. Comandos do dia a dia](#8-comandos-do-dia-a-dia)
- [9. Armadilhas](#9-armadilhas)
- [10. O que ainda não está pronto](#10-o-que-ainda-não-está-pronto)
- [Estrutura de arquivos](#estrutura-de-arquivos)
- [Dashboard original (InterSCity)](#dashboard-original-interscity)

---

## 1. O que o projeto é

```
ESP32 ──MQTT──▶ Mosquitto ──▶ ingestor ──▶ Postgres ──▶ API ──▶ dashboard
                (broker)      (2 pods)      (banco)    (2 pods)  (Streamlit)
```

Cinco serviços, cada um em um pod separado, conversando por rede. Nenhum deles
sabe o endereço IP de outro: a descoberta é por nome (`mosquitto`, `postgres`,
`api`), resolvido pelo Kubernetes.

| Serviço | Imagem | Réplicas | Papel |
|---|---|---|---|
| `mosquitto` | `eclipse-mosquitto:2` | 1 | broker MQTT; porta 1883 na LAN |
| `ingestor` | `monitoramento/ingestor` | 2 | valida, deduplica e grava a telemetria |
| `postgres` | `postgres:16-alpine` | 1 | histórico em `telemetria` |
| `api` | `monitoramento/api` | 2 | leitura HTTP (FastAPI) |
| `dashboard` | `monitoramento/dashboard` | 1 | Streamlit, porta 8080 no host |

---

## 2. O caminho de uma leitura

Acompanhe **uma** medição de temperatura, do sensor até o gráfico.

**1. O ESP32 mede e publica.** Ele monta um JSON e manda para o tópico
`maquinas/maq-001/telemetria`:

```json
{"maquina_id":"maq-001","seq":10432,"ts":"2026-11-18T18:40:25.123Z","temp":63.4,"vibra":2.81}
```

O `seq` é um contador que nunca volta atrás — fica salvo na memória flash do ESP32
(NVS). Isso é essencial; o motivo está no item 4.

**2. O broker recebe e guarda.** O Mosquitto é uma caixa de correio: não sabe o que
é temperatura, só sabe quem assinou o tópico. Ele responde ao ESP32 "recebi" (QoS 1)
e o ESP32 segue a vida — pode até desligar.

> É por isso que o ESP32 não precisa de lógica de retry, fila ou banco. Ele publica
> e esquece. Se o cluster inteiro estiver reiniciando, a mensagem espera no broker.

**3. O broker entrega para *uma* réplica do ingestor.** A assinatura é
`$share/ingestores/maquinas/+/telemetria`. O prefixo `$share/<grupo>/` significa
"somos um grupo, dividam o trabalho".

Pensa num grupo de mensagens onde cada mensagem chega para **um** membro só, em
rodízio. Sem o `$share`, as duas réplicas receberiam a mesma mensagem e tudo seria
processado em dobro.

**4. O ingestor acumula e grava.** Ele junta as mensagens num lote (até 50, ou 1
segundo, o que vier primeiro) e roda:

```sql
INSERT INTO telemetria (...) VALUES (...)
ON CONFLICT (maquina_id, seq) DO NOTHING
```

**5. Só depois do commit ele confirma ao broker.** O `PUBACK` sai **depois** que o
Postgres confirmou a gravação:

```python
gravados = gravar(linhas)     # commit no Postgres
for mid, qos in acks:
    client.ack(mid, qos)      # só agora o broker pode descartar
```

Se o pod morrer no meio, o broker **reentrega** a mensagem para quem estiver vivo.

**6. O banco descarta duplicatas.** Como a reentrega é possível, a mesma leitura
pode chegar duas vezes. A chave primária `(maquina_id, seq)` resolve: é como tentar
cadastrar o mesmo CPF duas vezes — a segunda é ignorada, sem erro.

> Esse par **entrega garantida (at-least-once) + chave idempotente** é o padrão da
> indústria para ingestão de dados.

**7. O dashboard lê.** O Streamlit chama a API a cada 30 s
(`GET /telemetria/maq-001?horas=24`); a API consulta o Postgres e devolve a série;
o dashboard desenha.

O dashboard **nunca** fala com o banco nem com o broker — só conhece a API. É o que
permite trocar o banco depois sem tocar no front.

---

## 3. Por que cada peça existe

| Peça | Serve para | Se você remover |
|---|---|---|
| **Mosquitto** | desacoplar o sensor do processamento | ESP32 precisa de retry, fila e banco embutidos |
| **ingestor** | validar, deduplicar e gravar | cada consumidor teria que saber SQL |
| **Postgres** | guardar histórico e servir consultas | nada sobrevive a um restart |
| **API** | isolar o banco do front | o dashboard precisaria de credencial do banco |
| **dashboard** | mostrar | — (é o produto) |

Por que **2 réplicas** de ingestor e de API? Dois motivos concretos:

1. **Capacidade** — o trabalho é dividido; se a carga dobrar, entra uma terceira.
2. **Disponibilidade** — numa atualização, o pod novo sobe antes de o antigo sair
   (`maxUnavailable: 0`). O serviço nunca fica sem ninguém atendendo.

---

## 4. As cinco ideias de sistema distribuído

**1. Idempotência.** A mesma operação repetida não muda o resultado. Aqui:
`(maquina_id, seq)` + `ON CONFLICT DO NOTHING`.

**2. Identidade estável.** Um pod com nome aleatório (`ingestor-7d9f-x2k`) é um
estranho a cada restart. Um StatefulSet dá nome fixo (`ingestor-0`, `ingestor-1`),
e o broker reconhece quem voltou.

> Esse foi o experimento que mudou o projeto. Com Deployment, matar um pod no meio
> da carga perdeu **268 de 1000** leituras (26,8%) — e as lacunas eram *uma sim,
> uma não*, porque o broker continuava fazendo rodízio com a sessão do pod que não
> existia mais. Com StatefulSet: **0 de 1000**.

**3. Separação entre vivo e pronto.** São duas perguntas diferentes:

- `/healthz` — "o processo está travado?" → se falhar, o Kubernetes **reinicia**.
- `/readyz` — "posso receber tráfego?" → se falhar, ele só **tira do balanceamento**.

Se o Postgres cair, o ingestor não deve ser reiniciado (reiniciar não conserta o
banco), só marcado como não-pronto. Confundir os dois é um dos erros mais comuns.

**4. Readiness descreve capacidade, não atividade.** A primeira versão do `/readyz`
falhava quando nenhuma mensagem chegava havia 60 s. Com a planta parada — situação
normal — os pods saíam do ar. Agora ele testa "broker conectado **e** banco
alcançável".

**5. Balanceamento e descoberta por DNS.** Ninguém configura IP. O ingestor acha o
broker pelo nome `mosquitto`; a API acha o banco por `postgres`.

---

## 5. O que acontece quando algo quebra

| Quem cai | O que o sistema faz | O que você vê |
|---|---|---|
| 1 pod do ingestor | broker reentrega para o outro / para a sessão dele quando volta | nada |
| **os 2** ingestores | broker enfileira até 10.000 mensagens | dados atrasados, **nada perdido** |
| Postgres | ingestor não confirma; mensagens ficam no broker | gráfico congela, depois volta cheio |
| 1 pod da API | o outro atende | nada |
| Mosquitto | ESP32 reconecta sozinho | buraco no gráfico até o pod voltar |
| Nó do cluster | pods dos outros nós assumem… **menos** Postgres e Mosquitto | ver [armadilha 4](#9-armadilhas) |

Números medidos (50 Hz, contagem conferida no banco):

| Experimento | Publicadas | Gravadas | Perdidas |
|---|---:|---:|---:|
| Matar 1 pod — **Deployment** (`client_id` aleatório) | 1000 | 732 | **268 (26,8%)** |
| Matar 1 pod — **StatefulSet** (`client_id` estável) | 1000 | 1000 | 0 |
| Apagão total (~9 s sem consumidor) | 1000 | 1000 | 0 |
| Rolling update sob carga | 1000 | 1000 | 0 |
| HPA sob carga | — | — | 2 → 6 réplicas |

Latência de ingestão (publicação → commit): p50 **0,58 s**, p95 **1,04 s** — é o
`FLUSH_INTERVAL_S` do ingestor, não a rede.

---

## 6. Como iniciar

### Pré-requisitos

Docker, `kind` e `kubectl`. Numa máquina Debian/Ubuntu:

```bash
mkdir -p ~/.local/bin
curl -sSLo ~/.local/bin/kind "https://kind.sigs.k8s.io/dl/v0.30.0/kind-linux-amd64"
chmod +x ~/.local/bin/kind

# o kubectl oficial vem de dl.k8s.io; se esse host não responder, use o bucket:
V=$(curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt)
curl -sSLo ~/.local/bin/kubectl \
  "https://storage.googleapis.com/kubernetes-release/release/$V/bin/linux/amd64/kubectl"
chmod +x ~/.local/bin/kubectl
```

Confira que `~/.local/bin` está no `PATH`.

A stack inteira, ociosa, consome ~1,5 GB de RAM. Cabe folgado em 8 GB.

### Caminho A — do zero (máquina nova, ou depois de `make down`)

```bash
cd monitoramento_maquinas
```

**1. Criar o cluster** (3 nós, ~40 s):

```bash
make cluster
kubectl get nodes          # 3 nós Ready
```

**2. Criar as senhas e os Secrets:**

```bash
make segredos
```

Gera duas senhas aleatórias, cria o arquivo de senha do Mosquitto com a ferramenta
oficial, cria os Secrets no cluster e grava `credenciais.env` (que o simulador usa
e que está no `.gitignore`).

**3. Construir as imagens, carregar nos nós e aplicar os manifestos:**

```bash
make up
```

São três coisas de uma vez: `docker build` das 3 imagens → `kind load` (sem isso o
Kubernetes não enxerga imagem feita localmente) → `kubectl apply -f k8s/`.

No fim ele lista os pods. **Todos precisam estar `1/1 Running`.**

**4. Ligar o sensor (simulado):**

```bash
source credenciais.env
python simulador/esp32_sim.py --maquinas maq-001 maq-002 --hz 2
```

O simulador publica exatamente o mesmo contrato do firmware. Use-o antes de ter o
hardware na mão — o pipeline não precisa esperar o ESP32.

**5. Abrir o dashboard:** http://localhost:8080

### Caminho B — o cluster já existe

Não rode `make cluster` de novo (dá erro de cluster existente). Só:

```bash
make ps          # ver o que está de pé
```

Se algo estiver fora:

```bash
make up                                                    # reaplica tudo (idempotente)
kubectl -n monitoramento rollout restart statefulset ingestor
```

E para ver dados chegando agora:

```bash
source credenciais.env && python simulador/esp32_sim.py --hz 2
```

### Como desligar

```bash
make down        # apaga o cluster inteiro, inclusive os dados do Postgres
```

`make down` **não** preserva nada. Para só parar de consumir recursos sem perder
dados:

```bash
docker stop monitoramento-control-plane monitoramento-worker monitoramento-worker2
docker start monitoramento-control-plane monitoramento-worker monitoramento-worker2
```

### ESP32 real

O firmware está em `esp32/monitoramento.ino` (Wi-Fi + PubSubClient, QoS 1, `seq` na
NVS, Last Will para detectar dispositivo offline). Ajuste `MQTT_HOST` para o IP da
máquina que roda o cluster:

```bash
ip -4 -o addr show scope global | awk '{print $2, $4}'
```

O broker é publicado na porta 1883 do host (`extraPortMappings` do
`kind-cluster.yaml`). Se o ESP32 não conectar, teste primeiro de outro computador na
mesma rede:

```bash
nc -vz <ip-da-maquina> 1883
```

Se falhar aí, é firewall ou mapeamento de porta — não é firmware.

---

## 7. Escada de verificação

Quando algo parecer errado, desça a escada: ela diz **onde** está o problema.

```bash
# 1. Os pods existem e estão prontos?
kubectl -n monitoramento get pods

# 2. O ingestor assinou o tópico?
kubectl -n monitoramento logs -l app=ingestor --tail=5 | grep assinando

# 3. As mensagens estão sendo gravadas?
kubectl -n monitoramento logs -l app=ingestor --tail=5 | grep lote

# 4. Elas estão no banco?
kubectl -n monitoramento exec postgres-0 -- psql -U monitoramento -d monitoramento \
  -c "SELECT maquina_id, count(*), max(recebido_em) FROM telemetria GROUP BY 1;"

# 5. A API responde?
kubectl -n monitoramento exec deploy/api -- python -c \
  "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/maquinas').read()[:200])"

# 6. O dashboard está vivo?
curl -s http://localhost:8080/_stcore/health     # tem que responder "ok"
```

O primeiro passo que falhar é o seu problema. Se os 6 passarem e a tela estiver
estranha, é o navegador.

---

## 8. Comandos do dia a dia

| Comando | Responde |
|---|---|
| `make ps` | "o que está de pé?" |
| `kubectl -n monitoramento logs -f deploy/api` | "o que a API está dizendo?" |
| `kubectl -n monitoramento logs -l app=ingestor --tail=20` | "estou gravando?" |
| `kubectl -n monitoramento describe pod <nome>` | "por que este pod não sobe?" |
| `kubectl -n monitoramento exec -it postgres-0 -- psql -U monitoramento -d monitoramento` | "o que tem no banco?" |
| `kubectl -n monitoramento top pods` | "quem está consumindo CPU?" |
| `kubectl -n monitoramento get hpa` | "o que escalou?" |
| `bash drills/01-matar-ingestor.sh maq-t1 1000 50` | "eu perco dado se um pod morrer?" |

`describe` é o mais subestimado: no fim dele fica a seção `Events`, onde aparece
`ImagePullBackOff`, `CrashLoopBackOff`, `FailedScheduling` e o motivo real.

---

## 9. Armadilhas

**1. `extraPortMappings` só vale na criação.** As portas 1883 (broker) e 8080
(dashboard) estão no `kind-cluster.yaml`. Mudar depois exige recriar o cluster.

**2. Imagem local não existe para o Kubernetes.** `docker build` não basta: o nó não
vê a imagem até um `kind load docker-image`. É o que o `make load` faz.

**3. `seq` que reinicia zera tudo.** Rodar o simulador duas vezes com o mesmo
`--maquinas maq-001` faz o `seq` voltar para 1, e o `ON CONFLICT` descarta tudo —
parece perda catastrófica, mas é o banco funcionando. Use um `maquina_id` novo a
cada teste (os drills fazem isso: `maq-t1`, `maq-t2`…). O firmware tem o mesmo risco,
e por isso grava o `seq` na NVS.

**4. `local-path` não é alta disponibilidade.** O volume do Postgres e do Mosquitto
fica preso ao nó onde nasceu. Se aquele nó morrer, o pod fica `Pending` para sempre.
Não rode `kubectl drain` no nó do banco esperando que ele migre.

**5. Senha não vai para o git.** `credenciais.env` e `pwfile` estão no `.gitignore`.
O `make segredos` recria tudo; se os Secrets já existem, ele só reescreve o
`credenciais.env`.

**6. Mosquitto não abre `password_file` que seja symlink.** É o que o kubelet cria
ao montar um Secret como diretório. O sintoma é
`password-file: Error: Unable to open pwfile` e `CrashLoopBackOff`, mesmo com o
arquivo legível. A correção é montar a chave com `subPath` (já está no
`k8s/20-mosquitto.yaml`).

---

## 10. O que ainda não está pronto

- **Firmware não compilado.** O `.ino` está escrito e segue o contrato, mas a
  primeira compilação (e o ajuste dos pinos dos sensores) é sua.
- **Sem TLS.** O broker aceita conexão em claro na 1883, dentro da rede local. Para
  produção: 8883 com certificado e `WiFiClientSecure`.
- **Sem retenção de dados.** A tabela cresce para sempre; falta um `DELETE`
  agendado ou particionamento.
- **Broker é ponto único de falha.** Se o pod do Mosquitto morrer, a ingestão para
  até ele voltar. Alternativas: EMQX em cluster ou Kafka.

---

## Estrutura de arquivos

```text
monitoramento_maquinas/
├── interface.py              dashboard original (InterSCity) — legado
├── requirements.txt          dependências do dashboard original
├── kind-cluster.yaml         cluster kind, com as portas mapeadas
├── Makefile                  cluster / segredos / build / load / deploy / ps / down
├── k8s/
│   ├── 00-namespace.yaml
│   ├── 01-configmap-postgres.yaml    schema da tabela telemetria
│   ├── 10-postgres.yaml              StatefulSet + PVC
│   ├── 20-mosquitto.yaml             broker + NodePort 31883
│   ├── 30-ingestor.yaml              StatefulSet, shared subscription
│   ├── 40-api.yaml                   FastAPI, 2 réplicas
│   ├── 50-dashboard.yaml             Streamlit, NodePort 30080
│   ├── 60-hpa.yaml
│   └── 70-pdb.yaml
├── services/
│   ├── ingestor/             ingestor.py + Dockerfile
│   ├── api/                  main.py + Dockerfile
│   └── dashboard/            interface.py adaptado + Dockerfile
├── simulador/esp32_sim.py    publica no mesmo contrato do firmware
├── esp32/monitoramento.ino   firmware
├── drills/                   experimentos de falha, com contagem no banco
├── scripts/segredos.sh       gera senhas e cria os Secrets
└── docs/guia-kubernetes.md   guia técnico completo
```

---

## Dashboard original (InterSCity)

A aplicação em `interface.py` consulta a API da plataforma InterSCity e continua
funcionando de forma independente do cluster.

### Funcionalidades

- Visualização dos dados das máquinas cadastradas.
- Consulta ao histórico de medições.
- Gráficos para acompanhar a evolução das variáveis monitoradas.
- Comparação entre máquinas.
- Filtro por período de tempo.
- Definição de limites para identificação de valores acima do esperado.
- Exportação do histórico de medições em CSV.

### Tecnologias utilizadas

Python, Streamlit, Pandas, Requests, Plotly, InterSCity.

### Requisitos

- Python 3.10 ou superior.
- Git (opcional, caso escolha clonar o repositório).

### Como obter o projeto

**Opção 1 — Clonar com Git**

```powershell
git clone https://github.com/Adricampelo/monitoramento_maquinas.git
cd monitoramento_maquinas
```

**Opção 2 — Baixar em ZIP**

1. Acesse o [repositório no GitHub](https://github.com/Adricampelo/monitoramento_maquinas).
2. Clique no botão **Code**.
3. Selecione **Download ZIP**.
4. Extraia o ZIP e abra o terminal na pasta onde estão `interface.py` e `requirements.txt`.

### Como executar no Windows

**1. Criar um ambiente virtual**

```powershell
python -m venv .venv
```

**2. Ativar o ambiente virtual**

```powershell
.\.venv\Scripts\Activate.ps1
```

Se a ativação for bloqueada pelas políticas do PowerShell, utilize diretamente o
executável Python do ambiente virtual.

**3. Instalar as dependências**

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**4. Executar o dashboard**

```powershell
streamlit run interface.py
```

O Streamlit abrirá a aplicação no navegador. Caso isso não aconteça automaticamente,
acesse [http://localhost:8501](http://localhost:8501). Para encerrar, pressione
`Ctrl + C` no terminal.

### Fonte dos dados

O dashboard consulta a API da plataforma InterSCity para obter os dados das máquinas
monitoradas. Para que os dados sejam exibidos corretamente, é necessário que a API
esteja acessível e que os recursos consultados possuam medições disponíveis.

> A versão containerizada em `services/dashboard/interface.py` é essa mesma
> interface com a camada de dados trocada: em vez do InterSCity, chama a API interna
> do cluster e transforma a resposta no mesmo formato longo (`Data`, `Valor`,
> `Variável`) que o resto do código já consumia. Toda a parte visual permaneceu
> intacta.
