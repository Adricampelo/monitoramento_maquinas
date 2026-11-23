# Teoria por trás do projeto

Este documento liga cada parte do projeto à teoria de sistemas distribuídos que
ela aplica. Cada seção tem três partes: **o conceito**, **onde aparece no
projeto** e **o que dizer na apresentação**.

Sumário:

1. [Visão geral: o que é um sistema distribuído](#1-visão-geral-o-que-é-um-sistema-distribuído)
2. [Arquitetura em camadas da IoT](#2-arquitetura-em-camadas-da-iot)
3. [Middleware e a plataforma InterSCity](#3-middleware-e-a-plataforma-interscity)
4. [O padrão gateway](#4-o-padrão-gateway)
5. [Comunicação: HTTP, REST e JSON](#5-comunicação-http-rest-e-json)
6. [Serviços sem estado (stateless)](#6-serviços-sem-estado-stateless)
7. [Tolerância a falhas: retry, timeout e backoff](#7-tolerância-a-falhas-retry-timeout-e-backoff)
8. [Contêineres e Docker](#8-contêineres-e-docker)
9. [Kubernetes: conceitos usados](#9-kubernetes-conceitos-usados)
10. [Tempo e relógios](#10-tempo-e-relógios)
11. [Propriedades de sistema distribuído no projeto](#11-propriedades-de-sistema-distribuído-no-projeto)
12. [Limitações conhecidas (e como responder)](#12-limitações-conhecidas-e-como-responder)
13. [Roteiro de demonstração](#13-roteiro-de-demonstração)
14. [Perguntas prováveis na apresentação](#14-perguntas-prováveis-na-apresentação)
15. [Glossário rápido](#15-glossário-rápido)

---

## 1. Visão geral: o que é um sistema distribuído

**Conceito.** Tanenbaum define um sistema distribuído como *"um conjunto de
computadores independentes que se apresenta aos usuários como um sistema único
e coerente"*. Os componentes rodam em máquinas diferentes, se comunicam só por
troca de mensagens pela rede e não compartilham memória nem relógio.

Os objetivos clássicos são:

- **Compartilhamento de recursos**: vários clientes usam os mesmos serviços.
- **Transparência**: esconder do usuário onde e como as coisas rodam.
- **Abertura**: interfaces padronizadas que permitem trocar peças.
- **Escalabilidade**: crescer sem precisar reescrever o sistema.

E os desafios clássicos:

- **Falhas parciais**: uma parte quebra e o resto continua.
- **Ausência de relógio global**.
- **Rede não confiável**: latência, perda de mensagens, partições.
- **Heterogeneidade**: hardware e linguagens diferentes.

**No projeto.** São pelo menos quatro "computadores" independentes:

```
ESP32 (C++, microcontrolador)
   │ HTTP pela rede Wi-Fi
   ▼
Gateway (Python, várias réplicas em nós diferentes do cluster)
   │ HTTPS pela internet
   ▼
InterSCity (servidores da UFMA, outros microsserviços)
   ▲
   │ HTTPS
Dashboard (Python/Streamlit, outro pod do cluster)
```

Nenhum deles compartilha memória com os outros, e toda a comunicação é por
mensagens HTTP. O usuário abre o dashboard e enxerga "um sistema de
monitoramento", sem saber quantos pods existem nem onde os dados estão
guardados. Isso é a transparência.

**Na apresentação.** "É distribuído porque os componentes rodam em máquinas
independentes, se comunicam só por rede e o sistema continua funcionando
quando uma parte falha."

---

## 2. Arquitetura em camadas da IoT

**Conceito.** Sistemas de Internet das Coisas costumam ser descritos em camadas:

| Camada | Função | No projeto |
|---|---|---|
| **Percepção (dispositivos)** | Sensores medem o mundo físico | ESP32 + sensores de temperatura e vibração (ou o simulador) |
| **Rede / borda** | Transporta os dados e traduz protocolos | Gateway no Kubernetes |
| **Middleware / plataforma** | Gerencia recursos e armazena e disponibiliza dados | InterSCity |
| **Aplicação** | Entrega valor ao usuário | Dashboard Streamlit |

**Por que separar em camadas?** Pelo **baixo acoplamento**: cada camada só
conhece a interface da camada vizinha. O dashboard não sabe que existe uma
ESP32, só conhece o InterSCity. Se amanhã o sensor for trocado por um CLP
industrial, o dashboard não muda nada.

---

## 3. Middleware e a plataforma InterSCity

### 3.1 O que é middleware

**Conceito.** Middleware é a camada de software que fica entre as aplicações e
a infraestrutura (sistema operacional e rede). Ela oferece serviços comuns,
como comunicação, nomeação, armazenamento e descoberta. Ela esconde a
heterogeneidade: a aplicação fala com uma API única, sem lidar com cada
dispositivo.

Sem middleware, cada aplicação de cidade inteligente teria que reinventar
cadastro de sensores, armazenamento de séries temporais, busca e controle de
acesso.

### 3.2 InterSCity

O InterSCity é uma plataforma de middleware **open source para cidades
inteligentes**, criada no INCT da Internet do Futuro para Cidades Inteligentes
(USP e parceiros). Neste projeto usamos a instância da **UFMA/LSDi**:
`https://cidadesinteligentes.lsdi.ufma.br/interscity_lh`.

A arquitetura é de **microsserviços**: cada responsabilidade é um serviço
separado, e todos ficam atrás de um API Gateway.

| Microsserviço | Responsabilidade | Usado no projeto? |
|---|---|---|
| **Resource Catalog** (`/catalog`) | Cadastro de recursos (sensores e atuadores) e das *capabilities* que cada um oferece. Gera o **UUID** do recurso. | Indiretamente: as máquinas já estão cadastradas, e os UUIDs ficam no ConfigMap |
| **Resource Adaptor** (`/adaptor`) | Porta de **entrada** de dados: os recursos enviam medições por aqui | ✅ O gateway faz `POST /adaptor/resources/{uuid}/data` |
| **Data Collector** (`/collector`) | Porta de **saída**: guarda e consulta o histórico de medições | ✅ O dashboard faz `GET /collector/resources/{uuid}/data` |
| **Resource Discovery** (`/discovery`) | Busca recursos por capability e localização | Não |
| **Actuator Controller** (`/actuator`) | Envia comandos para atuadores | Não |

Internamente, esses serviços trocam eventos por um barramento de mensagens
(RabbitMQ). Quando o Adaptor recebe um dado, ele publica um evento, e o
Collector consome esse evento e grava. Por isso o dado enviado pelo gateway
aparece no dashboard pouco depois: o modelo é de **consistência eventual**.

### 3.3 Conceitos-chave do InterSCity

- **Recurso**: qualquer entidade da cidade que produz dados ou aceita comandos.
  Aqui, cada máquina industrial é um recurso.
- **UUID**: identificador universal único do recurso. É **nomeação** no sentido
  de sistemas distribuídos: um nome global que independe de onde o recurso está.
- **Capability**: um tipo de dado que o recurso oferece. Aqui são `temp` e `vibra`.

Formato que o gateway envia ao Adaptor:

```json
{
  "data": {
    "temp":  [{ "value": 61.2, "timestamp": "2026-11-23T18:42:11+00:00" }],
    "vibra": [{ "value": 2.3,  "timestamp": "2026-11-23T18:42:11+00:00" }]
  }
}
```

**Na apresentação.** "O InterSCity é o middleware: ele dá cadastro, ingestão e
consulta de dados como serviço. Nosso sistema não armazena nada. A fonte da
verdade é o InterSCity, e nós produzimos e consumimos dados através das APIs
dele."

---

## 4. O padrão gateway

**Conceito.** Um **gateway IoT** é um intermediário entre dispositivos simples
e a plataforma. Ele tem quatro funções típicas:

1. **Tradução de protocolo e formato**: o dispositivo fala uma linguagem
   simples, e a plataforma espera outra.
2. **Tradução de identidade**: o dispositivo se identifica de um jeito local, e
   a plataforma usa um ID global.
3. **Desacoplamento**: o dispositivo não precisa conhecer a URL, o formato nem
   as regras da plataforma.
4. **Ponto de controle**: validação, retry, log e, no futuro, autenticação,
   agregação e cache.

**No projeto** ([services/gateway/gateway.py](services/gateway/gateway.py)):

| Função | Como está implementada |
|---|---|
| Tradução de formato | Recebe `{"maquina_id","temp","vibra","ts"}` (simples para a ESP32) e monta o JSON do Adaptor com listas de `{value, timestamp}` |
| Tradução de identidade | `maq-001` → `bcb2ade2-...` usando o mapa `MAQUINAS` do ConfigMap |
| Validação | O Pydantic (`class Leitura`) rejeita com 422 um payload sem `temp` numérico, por exemplo. Máquina desconhecida recebe 404. |
| Confiabilidade | 3 tentativas com backoff ao falar com o InterSCity |
| Observabilidade | Log de cada envio, `/healthz` com contadores e o campo `pod` na resposta |

**Por que não mandar direto da ESP32 para o InterSCity?**

- A ESP32 teria que saber o UUID, a URL HTTPS e o formato do Adaptor. Qualquer
  mudança exigiria regravar o firmware de todos os dispositivos.
- HTTPS com certificados é pesado para o microcontrolador.
- Sem o gateway, não haveria um ponto central para escalar, monitorar e
  aplicar regras.

**Atenção à diferença:** "API Gateway" é outra coisa. O InterSCity tem o seu
próprio API Gateway (Kong), que é a porta única de entrada para os
microsserviços dele. O nosso é um **gateway IoT**, que fica do lado dos
dispositivos. Na prática, as requisições do nosso gateway passam também pelo
API Gateway do InterSCity, porque é ele que roteia `/adaptor`, `/collector`
etc. para o serviço certo.

---

## 5. Comunicação: HTTP, REST e JSON

**Conceito.**

- **Cliente-servidor**: o cliente faz uma requisição e o servidor responde. É
  uma comunicação **síncrona**: o cliente espera a resposta.
- **REST**: estilo arquitetural em que tudo é um **recurso** identificado por
  URL e manipulado por verbos HTTP (`GET` lê, `POST` cria). É **sem estado**:
  cada requisição carrega tudo o que o servidor precisa.
- **JSON**: formato de serialização em texto, independente de linguagem. É o
  que permite que C++ (ESP32) e Python (gateway) se entendam. Isso resolve a
  heterogeneidade.
- **Códigos de status**: `2xx` sucesso, `4xx` erro do cliente (não adianta
  repetir), `5xx` erro do servidor (pode adiantar repetir).

**No projeto.**

| De → Para | Requisição | Respostas possíveis |
|---|---|---|
| ESP32 → Gateway | `POST /telemetria` | `201` ok · `404` máquina desconhecida · `422` payload inválido · `502` InterSCity falhou |
| Gateway → InterSCity | `POST /adaptor/resources/{uuid}/data` | `2xx` ok, qualquer outra conta como falha |
| Dashboard → InterSCity | `GET /collector/resources/{uuid}/data` | JSON com o histórico |

O **502 Bad Gateway** tem significado exato: "eu sou um intermediário e o
servidor depois de mim falhou". Por isso o gateway responde 502 quando o
InterSCity falha, e não 500.

**HTTP vs. MQTT (pergunta provável).** O MQTT é publish/subscribe, assíncrono,
leve e tem broker. Ele seria melhor com milhares de sensores ou rede muito
instável, porque o broker guarda as mensagens. Escolhemos HTTP direto por
**simplicidade**: um componente a menos, e o mesmo protocolo de ponta a ponta,
já que o InterSCity também é HTTP. A contrapartida é que, se tudo estiver fora
do ar, a responsabilidade de guardar e reenviar fica com o dispositivo.

---

## 6. Serviços sem estado (stateless)

**Conceito.** Um serviço é *stateless* quando não guarda, entre uma requisição
e outra, nenhuma informação necessária para atender a próxima. O estado fica
em outro lugar, como um banco ou um middleware.

Consequências:

- **Qualquer réplica atende qualquer requisição.** Isso permite o balanceamento
  de carga.
- **Escalabilidade horizontal**: para atender mais carga, basta adicionar
  réplicas.
- **Recuperação trivial**: se um pod morre, não se perde nada. Um novo é
  criado do zero.

**No projeto.** O gateway não tem banco e não tem sessão. O mapa máquina →
uuid vem do ConfigMap e é igual em todas as réplicas. Os contadores do
`/healthz` são só informativos e locais a cada pod. Todo o estado do sistema
(o histórico de medições) fica no InterSCity.

Foi justamente isso que permitiu remover Postgres, ingestor e Mosquitto: os
componentes **com estado** são os mais difíceis de operar num cluster, e eles
deixaram de ser necessários.

---

## 7. Tolerância a falhas: retry, timeout e backoff

**Conceito.** Em sistemas distribuídos, falhas são normais. Os mecanismos
básicos para lidar com elas são:

- **Timeout**: não esperar para sempre. Sem timeout, uma requisição travada
  prende um recurso indefinidamente.
- **Retry**: repetir uma operação que falhou por causa **transitória**, como
  uma oscilação de rede ou um pod reiniciando.
- **Backoff**: aumentar a espera entre as tentativas, para não sobrecarregar
  um serviço que já está com problema.
- **Redundância**: ter mais de uma cópia de cada componente, para que nenhum
  seja um ponto único de falha.

**No projeto, em dois níveis:**

1. **Gateway → InterSCity**: `timeout=10s`, 3 tentativas, espera de 0,5 s e
   depois 1 s. Se as três falharem, responde 502.
2. **Dispositivo → Gateway**: a ESP32 tenta 3 vezes (espera 1 s e 2 s) e o
   simulador tenta 5 vezes. Se um pod morrer no meio da requisição, a próxima
   tentativa cai no outro pod.

**Modelo de falhas.** O projeto trata principalmente **falhas por parada**
(*crash*): o pod morre ou o servidor não responde. Não trata falhas
**bizantinas**, em que um componente responde errado de propósito.

**Semântica de entrega.** Com retry, a garantia é **"pelo menos uma vez"**
(*at-least-once*). Se o InterSCity gravou o dado mas a resposta se perdeu na
rede, o gateway reenvia e a medição fica **duplicada**. A alternativa, "no
máximo uma vez", perderia dados. Para monitoramento, uma duplicata ocasional é
preferível a perder leituras.

---

## 8. Contêineres e Docker

**Conceito.** Um **contêiner** empacota a aplicação junto com todas as suas
dependências (interpretador, bibliotecas, configuração) e roda isolado no
mesmo kernel do host. Diferente de uma máquina virtual, não há um sistema
operacional inteiro por instância. Por isso ele sobe em segundos e consome
pouco.

- **Imagem**: o "molde" imutável, construído a partir de um `Dockerfile`.
- **Contêiner**: uma instância em execução de uma imagem.
- **Benefício**: o código roda igual no notebook, no cluster e em produção.

**No projeto.** Cada serviço tem o seu
[Dockerfile](services/gateway/Dockerfile):

```dockerfile
FROM python:3.12-slim                         # imagem base enxuta
COPY requirements.txt . && pip install ...    # dependências (camada cacheável)
COPY gateway.py .                             # código
RUN useradd --uid 10001 ... ; USER 10001      # roda sem root (segurança)
CMD ["uvicorn", "gateway:app", ...]           # processo principal
```

Duas boas práticas visíveis aí:

- As dependências são copiadas **antes** do código. Assim o Docker reaproveita
  o cache quando só o código muda.
- O processo roda com **usuário não-root**, o que reduz o estrago se o
  contêiner for comprometido.

---

## 9. Kubernetes: conceitos usados

### 9.1 O que é e por que usar

**Conceito.** O Kubernetes é um **orquestrador de contêineres**: ele decide
onde cada contêiner roda, reinicia os que falham, distribui a carga e faz
atualizações.

A ideia central é o **modelo declarativo**. Você não dá ordens do tipo "suba
um contêiner ali". Você declara o **estado desejado** ("quero 2 réplicas do
gateway"), e os **controladores** do Kubernetes rodam um **laço de
reconciliação**:

```
loop:
    observar o estado atual
    comparar com o estado desejado
    agir para eliminar a diferença
```

É por isso que, quando um pod é morto, outro aparece sozinho: o estado atual
(1 réplica) ficou diferente do desejado (2), e o controlador corrigiu.

### 9.2 Arquitetura do cluster

- **Control plane**: o "cérebro".
  - `kube-apiserver`: porta de entrada; o `kubectl` fala com ele.
  - `etcd`: banco chave-valor **distribuído e consistente**, usa o algoritmo
    de consenso Raft e guarda todo o estado do cluster.
  - `scheduler`: escolhe em qual nó cada pod vai rodar.
  - `controller-manager`: roda os laços de reconciliação.
- **Workers (nós)**: as máquinas que rodam os pods. Cada uma tem o `kubelet`,
  que conversa com o control plane e cuida dos contêineres locais.

**No projeto** ([kind-cluster.yaml](kind-cluster.yaml)): o **kind**
(*Kubernetes in Docker*) simula um cluster de **3 nós**, cada um sendo um
contêiner Docker: 1 control-plane e 2 workers. Na prática é um cluster de
verdade, só que rodando numa máquina só.

### 9.3 Objetos usados

| Objeto | Teoria | No projeto |
|---|---|---|
| **Namespace** | Isolamento lógico dentro do cluster | `monitoramento` agrupa todos os recursos do projeto |
| **Pod** | Menor unidade de execução: um ou mais contêineres com o mesmo IP. É efêmero. | Cada réplica do gateway e do dashboard é um pod |
| **Deployment** | Controlador que mantém N réplicas idênticas e faz atualizações | `gateway` (2 réplicas) e `dashboard` (1 réplica) |
| **ReplicaSet** | Criado pelo Deployment; é quem garante o número de pods | Criado automaticamente |
| **Service** | IP e nome DNS **estáveis** para um grupo de pods, que mudam de IP o tempo todo. Faz **balanceamento de carga**. | `gateway` e `dashboard` |
| **NodePort** | Tipo de Service que expõe uma porta em cada nó para acesso de fora do cluster | 30000 (gateway) e 30080 (dashboard), mapeadas para 8000 e 8080 do host pelo kind |
| **ConfigMap** | Configuração separada da imagem (princípio *12-factor*) | URL do InterSCity e mapas de máquinas, injetados como variáveis de ambiente via `envFrom` |
| **HPA** | Autoescalonamento horizontal por métrica | Gateway entre 2 e 5 réplicas, mirando 70% de CPU |

### 9.4 Service: descoberta e balanceamento

Os pods nascem e morrem, e cada pod novo ganha um IP novo. O Service resolve
isso com um **ponto de acesso estável**. Ele seleciona os pods pelo **label**
`app: gateway` e distribui as conexões entre os que estão **prontos**.

Isso é **transparência de localização e de replicação**: a ESP32 manda tudo
para `ip-do-pc:8000` sem saber quantas réplicas existem nem onde estão. O
campo `pod` na resposta do gateway serve exatamente para mostrar esse
balanceamento ao vivo.

### 9.5 Probes: detectar falhas

- **livenessProbe**: "o processo está vivo?" Se falhar repetidamente, o
  kubelet **reinicia** o contêiner.
- **readinessProbe**: "está pronto para receber tráfego?" Se falhar, o pod
  sai do Service (para de receber requisições), mas **não é reiniciado**.

No gateway, as duas consultam `/healthz`. A readiness **não** depende do
InterSCity, e isso é proposital: se o InterSCity cair, todas as réplicas
ficariam "não prontas" ao mesmo tempo, o Service ficaria sem destino e o
dispositivo receberia "conexão recusada" em vez de um 502 claro. Uma
dependência **externa** fora do ar não é motivo para tirar o seu próprio
serviço do ar.

Isso é um **detector de falhas** no sentido teórico: o sistema descobre que um
componente falhou por **timeout de heartbeat**, já que é impossível distinguir
com certeza um processo lento de um processo morto.

### 9.6 Rolling update: atualizar sem parar

```yaml
strategy:
  rollingUpdate:
    maxUnavailable: 0   # nunca fica com menos réplicas do que o desejado
    maxSurge: 1         # pode criar 1 a mais temporariamente
```

O Kubernetes sobe um pod da versão nova, espera ele ficar **pronto**
(readiness), só então derruba um pod antigo, e repete. O serviço nunca fica
fora do ar. Se a versão nova não ficar pronta, a atualização para e os pods
antigos continuam atendendo. Dá para desfazer com `kubectl rollout undo`.

### 9.7 Distribuição entre nós

```yaml
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
```

Pede ao scheduler que espalhe as réplicas do gateway entre os nós. Se um
**nó inteiro** cair, ainda sobra réplica em outro. Isso é **redundância
geográfica** em pequena escala: evitar que todas as cópias compartilhem o
mesmo ponto de falha.

### 9.8 HPA e metrics-server

O **HorizontalPodAutoscaler** consulta periodicamente o uso de CPU dos pods,
via **metrics-server**, e ajusta o número de réplicas:

```
réplicas_desejadas = ceil(réplicas_atuais × uso_atual / uso_alvo)
```

Por exemplo, com 2 réplicas a 140% de CPU e alvo de 70%, ele calcula
`2 × 140 / 70 = 4` réplicas. Isso é **escalabilidade horizontal elástica**, que
só é possível porque o gateway é stateless (seção 6). O `make cluster` instala
o metrics-server, porque o kind não vem com ele.

### 9.9 Requests e limits

```yaml
resources:
  requests: { cpu: 50m, memory: 64Mi }   # reserva usada pelo scheduler
  limits:   { memory: 256Mi }            # teto; acima disso o pod é morto (OOMKilled)
```

O `requests` informa ao scheduler quanto o pod precisa, para que ele escolha
um nó com espaço, e é a base do cálculo de porcentagem do HPA. O `limits`
protege os vizinhos contra vazamento de memória.

---

## 10. Tempo e relógios

**Conceito.** Num sistema distribuído **não existe relógio global**. Cada
máquina tem o seu, e eles divergem (*clock drift*). As soluções vão de
sincronização física, como **NTP**, até relógios lógicos, como **Lamport** e
vetoriais, quando só a ordem importa.

**No projeto.**

- A ESP32 liga com o relógio em 1970. Ela sincroniza via **NTP**
  (`pool.ntp.org`, `a.st1.ntp.br`) antes de usar o horário.
- Se o NTP falhar, ela **não manda** `ts`, e o gateway usa o horário dele.
  Assim um timestamp claramente errado nunca chega ao InterSCity.
- Todos os horários estão em **UTC (ISO-8601)**. O dashboard converte para o
  fuso `America/Fortaleza` só na exibição. Essa é a prática correta: guardar
  em UTC e converter na borda.

---

## 11. Propriedades de sistema distribuído no projeto

| Propriedade | Como o projeto atende |
|---|---|
| **Transparência de localização** | O dispositivo usa um endereço só; não sabe onde estão os pods nem o InterSCity |
| **Transparência de replicação** | Há N réplicas do gateway, mas para o cliente parece uma só |
| **Transparência de falha** (parcial) | Um pod morre e o cliente, no máximo, faz um retry |
| **Escalabilidade horizontal** | Gateway stateless com HPA de 2 a 5 réplicas |
| **Disponibilidade** | Réplicas em nós diferentes, rolling update sem downtime, recriação automática de pods |
| **Heterogeneidade** | C++/ESP32, Python, serviços da UFMA, tudo conversando por HTTP e JSON |
| **Abertura** | APIs REST documentadas (`/docs` no gateway) e middleware aberto (InterSCity) |
| **Consistência eventual** | O dado aparece no Collector pouco depois de entrar no Adaptor |

**Teorema CAP (se perguntarem).** Diante de uma partição de rede, um sistema
distribuído escolhe entre consistência (C) e disponibilidade (A). O
InterSCity, com ingestão e consulta separadas e eventos assíncronos, prioriza
**disponibilidade com consistência eventual**: a leitura pode estar alguns
instantes atrasada, mas o sistema continua aceitando dados.

---

## 12. Limitações conhecidas (e como responder)

Reconhecer limitações demonstra domínio do assunto. Estas são as do projeto:

| Limitação | Por que existe | Como evoluir |
|---|---|---|
| **InterSCity é ponto único de falha externo** | É um serviço de terceiros fora do nosso cluster | Um buffer local no gateway (fila ou banco) para reenviar depois |
| **Possíveis duplicatas** | Semântica *at-least-once* do retry | Um ID de sequência por leitura e deduplicação |
| **Perda de dados se tudo cair por muito tempo** | Sem broker ou fila, a ESP32 desiste após 3 tentativas | Buffer na flash da ESP32, ou voltar a usar MQTT com broker |
| **Sem autenticação no gateway** | Simplificação para a demonstração | Token por dispositivo (header) ou mTLS |
| **HTTP sem TLS entre ESP32 e gateway** | Rede local, e TLS é custoso no microcontrolador | TLS com certificado no gateway (Ingress) |
| **Dashboard com 1 réplica** | Streamlit guarda sessão em memória (stateful) | Mais réplicas com *session affinity* no Service |
| **Cluster numa máquina só (kind)** | Ambiente de laboratório | O mesmo YAML roda em cluster real (GKE, EKS, k3s em várias máquinas) |

---

## 13. Roteiro de demonstração

1. **Arquitetura** (desenho da seção 1): dispositivo → gateway → InterSCity →
   dashboard.
2. `make ps`: mostrar os 3 nós, as 2 réplicas do gateway em **nós diferentes**
   e o HPA.
3. `make sim`: mostrar o campo `pod=` **alternando** (balanceamento do Service).
4. Abrir o dashboard: os dados chegando pelo InterSCity.
5. `make drill`: matar um pod. O simulador continua com `ok` e um pod novo
   aparece (reconciliação).
6. `kubectl -n monitoramento rollout restart deployment/gateway`: atualização
   sem interromper o simulador.
7. (Opcional) Editar o ConfigMap e reiniciar: mudar a configuração sem
   reconstruir a imagem.

---

## 14. Perguntas prováveis na apresentação

**Onde está o "distribuído"?**
Vários processos independentes, em máquinas diferentes (ESP32, nós do cluster,
servidores da UFMA), cooperando só por mensagens de rede, com replicação e
tolerância a falhas parciais.

**Qual o papel do InterSCity?**
É o middleware: cadastro de recursos (Catalog), ingestão (Adaptor) e consulta
(Collector). É a fonte da verdade dos dados. Nós não temos banco próprio.

**Por que um gateway?**
Para desacoplar dispositivos simples da plataforma. Ele traduz formato e
identidade (id local → UUID), valida, faz retry e é o ponto que escala no
Kubernetes.

**O que acontece se um pod do gateway morrer?**
O Service tira o pod do balanceamento, a requisição em andamento falha, o
dispositivo tenta de novo e cai na outra réplica. O Deployment percebe que há
1 réplica em vez de 2 e cria outra.

**E se o InterSCity cair?**
O gateway tenta 3 vezes e responde 502. O dispositivo tenta de novo depois.
Os pods continuam saudáveis, porque a readiness não depende do InterSCity.
Leituras podem se perder se a queda for longa (ver limitações).

**Por que HTTP e não MQTT?**
Simplicidade: um componente a menos e o mesmo protocolo de ponta a ponta. O
MQTT valeria a pena com muitos dispositivos ou rede instável, por causa do
broker que guarda as mensagens.

**Por que o gateway escala e o dashboard não?**
O gateway é stateless. O Streamlit mantém a sessão do usuário na memória do
pod, então escalar exigiria afinidade de sessão.

**O que é o laço de reconciliação?**
Os controladores do Kubernetes comparam continuamente o estado desejado (o
YAML) com o estado real e agem para eliminar a diferença. É a base da
auto-recuperação.

**Liveness vs. readiness?**
Liveness falhando reinicia o contêiner. Readiness falhando só tira o pod do
tráfego.

**Por que o etcd usa consenso?**
Para que o estado do cluster seja consistente mesmo com falha de alguns nós do
control plane. O Raft garante que a maioria concorde antes de confirmar uma
escrita.

---

## 15. Glossário rápido

| Termo | Significado |
|---|---|
| **Backoff** | Esperar cada vez mais entre tentativas |
| **Capability** | Tipo de dado que um recurso do InterSCity oferece (ex.: `temp`) |
| **ConfigMap** | Objeto do Kubernetes com configuração em chave-valor |
| **Deployment** | Controlador que mantém N réplicas de um pod e faz atualizações |
| **HPA** | Escala o número de pods automaticamente por métrica |
| **Idempotente** | Operação que, repetida, dá o mesmo resultado (ex.: GET) |
| **kind** | Ferramenta que cria um cluster Kubernetes usando contêineres Docker como nós |
| **Middleware** | Camada entre aplicações e infraestrutura que oferece serviços comuns |
| **NodePort** | Service exposto numa porta fixa de cada nó |
| **NTP** | Protocolo de sincronização de relógios pela rede |
| **Pod** | Menor unidade de execução do Kubernetes |
| **Probe** | Verificação periódica de saúde (liveness/readiness) |
| **Reconciliação** | Ajustar continuamente o estado real ao estado desejado |
| **Service** | Endereço estável e balanceador para um grupo de pods |
| **Stateless** | Serviço que não guarda estado entre requisições |
| **UUID** | Identificador único global de um recurso |
| **At-least-once** | Garantia de entrega em que nada se perde, mas pode haver duplicatas |
