# Guia: sistema distribuído de monitoramento com Kubernetes

Este guia descreve como evoluir o dashboard atual (que só **lê** o InterSCity) para
um sistema que **recebe** telemetria de uma ESP32, distribui o processamento em
vários pods e sobrevive à queda de partes dele.

Tudo o que está aqui foi executado nesta máquina, em um cluster kind de 3 nós.
Os números da seção [Experimentos medidos](#experimentos-medidos) são resultado
desses testes, não estimativas.

---

## 1. Arquitetura alvo

```
   ESP32 (maq-001)                     ESP32 (maq-002)
   temp + vibra                        temp + vibra
        │                                    │
        │ MQTT QoS 1                         │
        └──────────────┬─────────────────────┘
                       ▼
              ┌──────────────────┐
              │  mosquitto       │   broker MQTT (1 pod, PVC)
              │  NodePort 31883  │   ── publicado na LAN na porta 1883
              └────────┬─────────┘
                       │ $share/ingestores/maquinas/+/telemetria
                       │ (o broker divide as mensagens entre as réplicas)
                       ▼
              ┌──────────────────┐
              │  ingestor        │   StatefulSet, 2 réplicas
              │  (Python)        │   valida, deduplica por seq, grava
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐
              │  postgres        │   StatefulSet + PVC
              │  telemetria      │   PK (maquina_id, seq)
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐        ┌──────────────────┐
              │  api (FastAPI)   │◀───────│  dashboard       │
              │  2 réplicas      │        │  (Streamlit)     │
              └──────────────────┘        └──────────────────┘
```

O que é **distribuído** aqui, e não é enfeite:

| Propriedade | Onde aparece |
|---|---|
| Desacoplamento temporal | a ESP32 publica e esquece; se o banco cair, o broker segura |
| Balanceamento de carga | shared subscription do MQTT + Service do Kubernetes |
| Identidade estável | StatefulSet do ingestor (client_id fixo por réplica) |
| Tolerância a falhas | probes, reentrega do broker, `ON CONFLICT` |
| Atualização sem queda | rolling update com `maxUnavailable: 0` |
| Escala horizontal | HPA por CPU |
| Consistência sob duplicata | entrega *at-least-once* + chave primária idempotente |

---

## 2. O contrato de dados

Defina isso **antes** de escrever qualquer código. Todo o resto depende dele.

**Tópico:** `maquinas/<maquina_id>/telemetria`

**Payload:**

```json
{
  "maquina_id": "maq-001",
  "seq": 10432,
  "ts": "2026-11-18T17:40:25.123Z",
  "temp": 63.4,
  "vibra": 2.81,
  "fw": "1.0.0"
}
```

**Regras:**

- `seq` é um contador monotônico por máquina, **persistido na NVS**. Sem isso, cada
  reboot do ESP32 recomeça do zero e o banco descarta as leituras novas como
  duplicatas. É o erro mais fácil de cometer e o mais difícil de perceber.
- `ts` é o relógio do dispositivo e **não** serve para ordenar: o ESP32 sem NTP
  acorda em 1970. O eixo do tempo é o `recebido_em` gerado pelo banco. O `ts` fica
  guardado só para medir latência e detectar relógio errado.
- QoS 1 (pelo menos uma vez). Duplicata é esperada; a idempotência resolve.
- Last Will no tópico `maquinas/<id>/status` com payload `{"online":false}` retido.
  Ao conectar, publica `{"online":true}` retido. Isso dá detecção de falha do
  dispositivo sem nenhum polling.

---

## 3. Decisões de projeto

**MQTT e não HTTP.** O ESP32 publica em um broker e não precisa saber quem consome.
Se o ingestor estiver reiniciando, a mensagem fica no broker. Com HTTP direto, o
dispositivo teria que implementar retry e guardar fila.

**Mosquitto e não Kafka.** Para ~dezenas de mensagens por segundo, Mosquitto é 2 MB
de RAM contra centenas do Kafka. O preço está na seção
[Limites conhecidos](#16-limites-conhecidos-e-próximos-passos).

**Postgres e não InfluxDB/TimescaleDB.** O volume do projeto não justifica um banco
de série temporal, e a chave primária `(maquina_id, seq)` é o que garante a
idempotência — em hypertable do TimescaleDB a chave teria que incluir a coluna de
tempo, o que quebra a deduplicação por `seq`. Se um dia o volume crescer, dá para
migrar depois; a API isola o dashboard dessa decisão.

**StatefulSet no ingestor, não Deployment.** Ver a seção
[Experimentos medidos](#experimentos-medidos): com Deployment (nome de pod aleatório)
o broker continua entregando mensagens para a sessão durável do pod morto, e
**26,8% das leituras se perdem**. Com StatefulSet (nome fixo), zero perda.

**kind e não minikube.** kind sobe nós como containers Docker, inicia em ~20 s e
permite mapear portas do host para dentro do cluster — que é o que faz a ESP32
enxergar o broker.

---

## 4. Ambiente

Ferramentas necessárias: Docker, `kind`, `kubectl`.

```bash
mkdir -p ~/.local/bin
curl -sSLo ~/.local/bin/kind "https://kind.sigs.k8s.io/dl/v0.30.0/kind-linux-amd64"
chmod +x ~/.local/bin/kind
```

O `kubectl` oficial vem de `dl.k8s.io`, que **não responde** nesta rede. Use o
bucket do Google:

```bash
V=$(curl -s https://storage.googleapis.com/kubernetes-release/release/stable.txt)
curl -sSLo ~/.local/bin/kubectl \
  "https://storage.googleapis.com/kubernetes-release/release/$V/bin/linux/amd64/kubectl"
chmod +x ~/.local/bin/kubectl
```

Confira que `~/.local/bin` está no `PATH`.

Recursos desta máquina (4 vCPU, 11 GB): a stack inteira, ociosa, consome
**~1,5 GB** — cabe folgado. Feche o que estiver pesado antes de subir o cluster.

---

## 5. Fase 1 — subir o cluster

O arquivo `kind-cluster.yaml` já existe na raiz do projeto:

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: monitoramento
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 31883   # broker MQTT
        hostPort: 1883
        protocol: TCP
      - containerPort: 30080   # dashboard
        hostPort: 8080
        protocol: TCP
  - role: worker
  - role: worker
```

```bash
make cluster
kubectl get nodes
```

Saída esperada: 3 nós `Ready`, um `control-plane` e dois `worker`.

> **Ponto sem retorno:** `extraPortMappings` só pode ser definido na criação do
> cluster. Se esquecer, é `make down` e criar de novo.

A porta 1883 do host passa a apontar para o nó control-plane, que encaminha para o
NodePort 31883 do broker. É assim que a ESP32 (na mesma rede Wi-Fi) vai alcançar o
cluster.

---

## 6. Fase 2 — segredos

Nada de senha em manifesto versionado.

```bash
make segredos
```

O script `scripts/segredos.sh`:

1. gera duas senhas aleatórias;
2. cria o `password_file` do mosquitto com `mosquitto_passwd` (o hash tem que ser gerado pela ferramenta, não escrito à mão);
3. cria os Secrets `mqtt-creds` e `postgres-creds`;
4. grava `credenciais.env` (já no `.gitignore`) para o simulador usar.

```bash
kubectl -n monitoramento get secrets
```

---

## 7. Fase 3 — broker

```bash
kubectl apply -f k8s/20-mosquitto.yaml
kubectl -n monitoramento rollout status deploy/mosquitto
```

Configuração relevante (`k8s/20-mosquitto.yaml`):

| Opção | Por quê |
|---|---|
| `allow_anonymous false` | a ESP32 autentica com usuário e senha |
| `persistence true` + PVC | sessões e filas sobrevivem ao restart do pod |
| `max_queued_messages 10000` | quanto o broker segura com o consumidor fora |
| `persistent_client_expiration 10m` | descarta sessão órfã (ver seção 9) |

> **Armadilha que custa uma hora:** o mosquitto 2.x **não abre** o `password_file`
> quando ele é um symlink — e é exatamente isso que o kubelet cria ao montar um
> Secret como diretório (`pwfile -> ..data/pwfile`). O sintoma é
> `password-file: Error: Unable to open pwfile` e `CrashLoopBackOff`, mesmo com o
> arquivo legível (`cat` funciona, `su mosquitto -c cat` funciona).
> A correção é montar a chave com `subPath`, que entrega o arquivo real:

```yaml
volumeMounts:
  - name: creds
    mountPath: /mosquitto/creds/pwfile
    subPath: pwfile
    readOnly: true
```

Teste rápido, do host:

```bash
source credenciais.env
mosquitto_sub -h localhost -p 1883 -u "$MQTT_USER" -P "$MQTT_PASS" -t '#' -v
```

(ou o equivalente em Python, se não tiver o cliente instalado)

---

## 8. Fase 4 — banco

```bash
kubectl apply -f k8s/01-configmap-postgres.yaml -f k8s/10-postgres.yaml
kubectl -n monitoramento rollout status statefulset/postgres
```

O schema vem de um ConfigMap montado em `/docker-entrypoint-initdb.d`, então roda
uma única vez, na criação do volume:

```sql
CREATE TABLE IF NOT EXISTS telemetria (
    maquina_id     text             NOT NULL,
    seq            bigint           NOT NULL,
    recebido_em    timestamptz      NOT NULL DEFAULT now(),
    ts_dispositivo timestamptz,
    temp           double precision,
    vibra          double precision,
    fw             text,
    PRIMARY KEY (maquina_id, seq)
);
```

A chave primária **é** o mecanismo de idempotência. Combinada com
`ON CONFLICT DO NOTHING`, uma mensagem reentregue três vezes grava uma linha só.

`PGDATA` aponta para um subdiretório porque o volume recém-provisionado pode conter
`lost+found`, e o `initdb` se recusa a rodar em diretório não vazio.

---

## 9. Fase 5 — ingestor (o coração do sistema)

```bash
make build && make load
kubectl apply -f k8s/30-ingestor.yaml
kubectl -n monitoramento rollout status statefulset/ingestor
```

O `services/ingestor/ingestor.py` faz quatro coisas que importam:

**1. Shared subscription.** Assina `$share/ingestores/maquinas/+/telemetria`. O
prefixo `$share/<grupo>/` transforma a assinatura em um grupo de consumidores: cada
mensagem vai para **uma** réplica, não para todas. Sem isso, as duas réplicas
gravariam a mesma leitura (e o `ON CONFLICT` esconderia o problema, mas você estaria
processando tudo em dobro).

**2. Ack manual depois do commit.** `manual_ack=True` no construtor do cliente paho.
O PUBACK só é enviado ao broker **depois** que o lote foi gravado:

```python
client = mqtt.Client(..., clean_session=False, manual_ack=True)
# ...
gravados = gravar(linhas)          # commit no Postgres
for mid, qos in acks:
    client.ack(mid, qos)           # só agora o broker descarta a mensagem
```

Se o pod morrer no meio, o broker reentrega. Se o commit já tinha acontecido, a
reentrega vira `ON CONFLICT DO NOTHING`.

**3. Identidade estável.** `client_id` vem do `HOSTNAME`, que em StatefulSet é
`ingestor-0` / `ingestor-1` — sempre o mesmo. Com `clean_session=False`, o broker
guarda a sessão; quando o pod volta com o mesmo nome, ele recebe o que ficou
pendente.

**4. Falha de banco não vira perda.** Se o `INSERT` falha, o lote volta para o
buffer, a conexão é refeita e nada é confirmado ao broker.

**5. Cuidado com transação implícita.** A conexão é aberta com `autocommit=True`
justamente para que cada `with conn.transaction()` emita `BEGIN`/`COMMIT` de
verdade. Se você deixar `autocommit=False` e rodar um `SELECT 1` solto (num ping de
readiness, por exemplo), essa consulta abre uma transação implícita; os `INSERT`
seguintes viram `SAVEPOINT` dentro dela e **nunca são commitados**. O sintoma é
cruel: o log diz "20 gravado", o `rowcount` confirma, e o `count(*)` no banco não
muda. A conexão também não é segura entre threads — o ingestor serializa todo acesso
com um `RLock`.

> **O experimento que muda o desenho.** A primeira versão usava Deployment, com
> `client_id` aleatório por pod. Ao matar um pod durante a carga, 268 de 1000
> leituras se perderam (26,8%) — e o padrão das lacunas era *uma sim, uma não*:
> o broker continuava fazendo rodízio com a sessão durável do pod que não existia
> mais. Com StatefulSet, 1000 de 1000. Detalhes na seção 15.

---

## 10. Fase 6 — API

```bash
kubectl apply -f k8s/40-api.yaml
```

Três rotas:

| Rota | Uso |
|---|---|
| `GET /maquinas` | última leitura de cada máquina + `online` (sem dados há >10 min) |
| `GET /telemetria/{id}?horas=24` | série temporal em formato largo |
| `/healthz`, `/readyz` | liveness (processo vivo) e readiness (banco alcançável) |

A separação entre `/healthz` e `/readyz` não é preciosismo: se o Postgres cair,
o pod **não** deve ser reiniciado (liveness continua OK), só tirado do balanceamento
(readiness falha). Reiniciar não conserta um banco fora do ar — só piora.

---

## 11. Fase 7 — dashboard

```bash
kubectl apply -f k8s/50-dashboard.yaml
```

Acesse **http://localhost:8080**.

O `services/dashboard/interface.py` é o `interface.py` da raiz com a camada de dados
trocada: em vez de `requests.get` no collector do InterSCity, chama a API interna e
transforma a resposta no mesmo formato longo (`Data`, `Valor`, `Variável`) que o
resto do código já consumia. Toda a parte visual (métricas, gráfico com linha de
limite, tabela, exportação CSV) ficou intacta.

O `interface.py` original continua na raiz e continua funcionando contra o
InterSCity. Quando estiver satisfeito com a versão nova, substitua.

Probe do Streamlit é `/_stcore/health` — caminho pouco óbvio, vale anotar.

---

## 12. Fase 8 — ESP32 real

Antes de queimar firmware, valide o pipeline inteiro sem hardware:

```bash
source credenciais.env
python simulador/esp32_sim.py --host localhost --maquinas maq-001 maq-002 --hz 2
```

Ele publica exatamente o mesmo contrato do firmware. Se o dashboard atualiza, o
cluster está de pé e o problema (se houver) é do lado do dispositivo.

Depois, o firmware em `esp32/monitoramento.ino`:

- Wi-Fi + PubSubClient, QoS 1;
- `seq` na NVS, gravado a cada 10 leituras (poupa a flash);
- Last Will `{"online":false}` retido;
- NTP para o `ts` (só diagnóstico — o eixo do tempo é do servidor);
- reconexão automática de Wi-Fi e MQTT.

Ajuste `MQTT_HOST` para o IP da máquina que roda o cluster:

```bash
ip -4 -o addr show scope global | awk '{print $2, $4}'
# wlp0s20f3 192.168.14.111/23
```

Confira se o firewall do host libera a 1883 para a rede local:

```bash
sudo ufw status
```

Se o ESP32 não conectar: teste primeiro do celular/notebook na mesma rede
(`nc -vz 192.168.14.111 1883`). Se falhar aí, é firewall ou `extraPortMappings`,
não é firmware.

---

## 13. Fase 9 — resiliência

```bash
kubectl apply -f k8s/60-hpa.yaml -f k8s/70-pdb.yaml
```

O que já está nos manifestos:

| Recurso | Configuração | Efeito |
|---|---|---|
| `readinessProbe` | `/readyz` | pod sem banco sai do balanceamento |
| `livenessProbe` | `/healthz` | processo travado é reiniciado |
| `resources.requests` | 50–100m CPU | o scheduler consegue distribuir |
| `topologySpreadConstraints` | por hostname | réplicas em nós diferentes |
| `maxUnavailable: 0` | rolling update | sobe o novo antes de matar o antigo |
| `terminationGracePeriodSeconds: 30` | + handler de SIGTERM | drena o buffer antes de sair |
| PDB `minAvailable: 1` | api e ingestor | `kubectl drain` não derruba o serviço |
| HPA 2→6 | CPU 60–70% | escala sob carga |

> **Readiness não é "estou recebendo dados".** A primeira versão do `/readyz` do
> ingestor falhava quando nenhuma mensagem chegava havia mais de 60 s. Com a planta
> parada (situação normal), os pods saíam do ar e o rollout nunca terminava. O
> critério correto é *capacidade*: broker conectado **e** banco alcançável (um
> `SELECT 1` a cada 10 s). Planta parada não é falha.

HPA precisa do metrics-server. Em kind ele exige `--kubelet-insecure-tls`:

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
```

> Nesta rede o `registry.k8s.io` está bloqueado (`i/o timeout` em
> `cdn.registry.k8s.io`), então o pod fica em `ImagePullBackOff`. Alternativa que
> funcionou: puxar `bitnamilegacy/metrics-server:0.7.2` do Docker Hub, importar a
> imagem nos nós com `ctr` e apontar o Deployment para ela.
>
> ```bash
> docker pull --platform=linux/amd64 bitnamilegacy/metrics-server:0.7.2
> docker save bitnamilegacy/metrics-server:0.7.2 -o /ms.tar
> for n in monitoramento-control-plane monitoramento-worker monitoramento-worker2; do
>   docker cp /ms.tar $n:/ms.tar && docker exec $n ctr -n k8s.io images import /ms.tar
> done
> kubectl -n kube-system set image deploy/metrics-server \
>   metrics-server=bitnamilegacy/metrics-server:0.7.2
> ```
>
> Detalhe: `docker cp` para `/tmp` não funciona nos nós do kind (o `/tmp` é tmpfs e
> esconde o arquivo). Copie para `/ms.tar`.

---

## 14. Fase 10 — observabilidade

O mínimo que resolve:

```bash
kubectl -n monitoramento logs -l app=ingestor --prefix --tail=50
kubectl -n monitoramento top pods
kubectl -n monitoramento get hpa
```

O ingestor expõe `/metrics` com contadores (`gravados`, `duplicados`, `invalidos`,
`ultimo_commit`). É o suficiente para responder "estou perdendo dado?" sem
Prometheus.

Quando quiser Prometheus + Grafana, **dimensione**: o `kube-prometheus-stack`
completo pede ~1,5 GB e vai brigar com os 11 GB desta máquina. Comece com
`prometheus` sozinho, retenção de 2 dias e `resources.limits` definidos.

---

## 15. Experimentos medidos

Todos com o simulador a 50 Hz e contagem conferida no banco.

| Experimento | Configuração | Publicadas | Gravadas | Perdidas |
|---|---|---:|---:|---:|
| Matar 1 pod do ingestor | **Deployment** (`client_id` aleatório) | 1000 | 732 | **268 (26,8%)** |
| Matar 1 pod do ingestor | **StatefulSet** (`client_id` estável) | 1000 | 1000 | 0 |
| Apagão total (~9 s sem consumidor) | StatefulSet, 2 réplicas | 1000 | 1000 | 0 |
| Rolling update sob carga | StatefulSet, `maxUnavailable: 0` | 1000 | 1000 | 0 |
| HPA sob carga | api, 2 réplicas | — | — | 2 → 6 réplicas |

Latência de ingestão (publicação → commit), 20 Hz, cluster ocioso:

| p50 | p95 | máx |
|---|---|---|
| 0,58 s | 1,04 s | 1,16 s |

Esses ~600 ms são o `FLUSH_INTERVAL_S=1.0` do ingestor, não a rede. Para reduzir,
baixe o intervalo (e o `BATCH_SIZE` junto) — o custo é mais commits por segundo.

Reproduza:

```bash
export PYTHON=.venv/bin/python     # ou o python com paho-mqtt instalado
bash drills/01-matar-ingestor.sh   maq-t1 1000 50
bash drills/02-apagao-ingestor.sh  maq-t2 1000 50
bash drills/03-rolling-update.sh   maq-t3 1000 50
```

> Use um `maquina_id` novo a cada execução. O `seq` do simulador recomeça do zero e
> o `ON CONFLICT` descartaria tudo — o mesmo motivo pelo qual o firmware precisa
> persistir o `seq` na NVS.

Conferir lacunas:

```bash
kubectl -n monitoramento exec postgres-0 -- psql -U monitoramento -d monitoramento \
  -c "SELECT count(*) AS gravadas, max(seq) AS maior_seq FROM telemetria WHERE maquina_id='maq-t1';"
```

---

## 16. Limites conhecidos e próximos passos

**O broker é ponto único de falha.** Se o pod do mosquitto morrer, a ingestão para
até ele voltar (o PVC preserva sessões e filas, mas não há HA). Para produção:
EMQX em cluster, ou Kafka com consumer group — que resolve de vez o problema da
sessão órfã, porque o rebalanceamento é do grupo, não de cada cliente.

**Sessão órfã ao reduzir réplicas.** Se o HPA baixar de 3 para 2, a sessão do
`ingestor-2` fica no broker até o `persistent_client_expiration` (10 min). Durante
essa janela, parte das mensagens é enfileirada para um cliente que não volta.
Mitigação atual: expiração curta. Solução real: Kafka.

**Storage local não é HA.** O `local-path` do kind amarra o PVC ao nó. Se o nó do
`postgres-0` morrer, o pod não sobe em outro nó — fica `Pending`. Para testar
resiliência de nó de verdade, é preciso um StorageClass de rede (Longhorn, NFS) ou
um Postgres gerenciado.

**Sem TLS.** O broker aceita conexão em claro na 1883, dentro da LAN. O próximo
passo é a 8883 com certificado e `WiFiClientSecure` no ESP32 (ou mTLS por
dispositivo, que é o padrão industrial).

**Limiares são fixos no código.** Os limites de alerta estão no dashboard. O passo
natural é um serviço `alertas` consumindo o mesmo tópico (outro `$share` com grupo
diferente) e gravando ocorrências — o dashboard passa a só exibir.

**Sem retenção.** A tabela cresce para sempre. Um `DELETE` agendado ou
particionamento por mês resolve.

---

## 17. Checklist

```bash
# 1. cluster
kubectl get nodes                       # 3 nós Ready

# 2. tudo de pé
kubectl -n monitoramento get pods       # todos 1/1 Running

# 3. broker aceita conexão autenticada
kubectl -n monitoramento logs deploy/mosquitto --tail=5

# 4. ingestor assinou
kubectl -n monitoramento logs -l app=ingestor --tail=5 | grep assinando

# 5. dados entrando
kubectl -n monitoramento exec postgres-0 -- \
  psql -U monitoramento -d monitoramento -c \
  "SELECT maquina_id, count(*), max(recebido_em) FROM telemetria GROUP BY 1;"

# 6. API responde
kubectl -n monitoramento exec deploy/api -- \
  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/maquinas').read()[:200])"

# 7. dashboard
curl -s http://localhost:8080/_stcore/health   # ok
```

---

## 18. Ordem sugerida

Se for fazer em etapas, esta ordem evita retrabalho:

1. **Contrato de dados** (seção 2). Nada de código antes disso.
2. **Cluster + broker + simulador**, sem banco: só confirmar que uma mensagem
   publicada no host chega a um `mosquitto_sub` dentro do cluster.
3. **Postgres + ingestor**, ainda com o simulador. Confirme `count(*)` crescendo.
4. **API + dashboard**: aqui o sistema já é demonstrável de ponta a ponta.
5. **ESP32 real**, trocando o simulador pelo hardware. O cluster não muda.
6. **Resiliência** (probes, PDB, HPA) e os experimentos da seção 15.
7. **Observabilidade**, TLS, retenção — só depois que o resto estiver estável.

O passo 4 é o ponto em que o projeto passa a valer uma demonstração. Antes dele,
nada é visível; depois, cada melhoria é incremental.

---

## Estrutura de arquivos

```
monitoramento_maquinas/
├── interface.py              dashboard original (InterSCity) — intocado
├── kind-cluster.yaml         definição do cluster kind (portas mapeadas)
├── Makefile                  cluster / build / load / deploy / ps / down
├── k8s/
│   ├── 00-namespace.yaml
│   ├── 01-configmap-postgres.yaml
│   ├── 10-postgres.yaml      StatefulSet + PVC + schema
│   ├── 20-mosquitto.yaml     broker + NodePort 31883
│   ├── 30-ingestor.yaml      StatefulSet, shared subscription
│   ├── 40-api.yaml           FastAPI, 2 réplicas
│   ├── 50-dashboard.yaml     Streamlit, NodePort 30080
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
└── docs/guia-kubernetes.md   este guia
```
