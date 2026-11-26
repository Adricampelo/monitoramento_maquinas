# Plano de testes de carga

Objetivo: descobrir **quantos envios da ESP32** e **quantos usuários do dashboard**
o sistema aguenta, e mostrar que o Kubernetes escala sob carga.

## 1. O que está sendo medido

| Pergunta | Alvo | Ferramenta | Métrica principal |
|---|---|---|---|
| Quantos envios por segundo? | Gateway (`POST /telemetria`) | k6, cenário `vazao` | req/s com erro < 1% e p95 < 1 s |
| Quantos envios simultâneos? | Gateway | k6, cenário `simultaneos` | nº de conexões com p95 < 1 s |
| Quantos usuários no dashboard? | Dashboard (Streamlit) | Playwright (navegadores reais) | nº de abas com p95 de carregamento < 10 s |
| O HPA funciona? | Gateway e dashboard | `kubectl get hpa` durante os testes | réplicas sobem com a carga e descem depois |

### Critério de "aguenta"

Um degrau de carga **passa** quando, ao mesmo tempo:

- **Gateway:** taxa de erro abaixo de 1% e 95% das respostas abaixo de 1 s.
- **Dashboard:** nenhum erro e 95% das abas carregadas em até 10 s.

A **capacidade** é o último degrau que passou.

## 2. Por que usar um InterSCity falso

O servidor do InterSCity é da UFMA, compartilhado e acadêmico. Um teste de carga
contra ele:

- pode **derrubar ou atrapalhar** um serviço usado por outras pessoas;
- mediria a capacidade **dele**, não a nossa.

Por isso [mock-interscity/mock.py](mock-interscity/mock.py) imita o Adaptor e o
Collector com **latência configurável** (padrão 200 ms, parecido com uma chamada
pela internet). O resultado é a capacidade **do nosso sistema**.

A capacidade real em produção é o **menor** de dois números: o que nós
aguentamos e o que o InterSCity aguenta. Para descobrir o segundo, seria preciso
combinar um teste com os responsáveis pelo servidor da UFMA.

## 3. Preparação

Pré-requisito: cluster rodando (`make cluster && make up`).

```bash
make teste-preparar
```

Esse comando:
1. constrói e carrega a imagem do InterSCity falso;
2. sobe o falso no cluster (`testes/mock-interscity.yaml`);
3. aponta o gateway e o dashboard para ele e desliga o log por envio
   (`LOG_LEVEL=WARNING`), que sob carga pesa.

Em **outro terminal**, deixe rodando durante todos os testes:

```bash
make teste-monitorar     # HPA, CPU/memória dos pods e pods, a cada 2 s
```

Para cada teste, anote o pico de réplicas e o uso de CPU e memória.

## 4. Testes

### T1 — Vazão do gateway (envios por segundo)

```bash
make teste-gateway                 # cenário "vazao"
```

Ritmo crescente: 100 → 250 → 500 → 1000 → 1500 → 2000 req/s, 1 minuto cada, cerca
de 7 minutos no total. Cada requisição é igual ao envio de uma ESP32.

**O que observar**
- No resumo do k6: `http_reqs` (vazão), `http_req_duration p(95)`, `http_req_failed`.
- Avisos `Insufficient VUs`: o sistema não está dando conta do ritmo, e o k6 precisa
  de cada vez mais conexões abertas.
- No monitor: o HPA do gateway deve subir de 2 para até 5 réplicas.
- `respostas_por_pod` mostra a distribuição da carga entre os pods.

O resultado é salvo em `testes/resultado-gateway-vazao.json`.

### T2 — Envios simultâneos

```bash
make teste-gateway CENARIO=simultaneos
```

Conexões abertas ao mesmo tempo, cada uma enviando sem pausa: 100 → 250 → 500 → 1000.

**O que observar:** em que degrau o p95 passa de 1 s. Cada pod tem um teto de
**200 conexões** com o InterSCity (`MAX_CONEXOES`). Acima disso as requisições
esperam na fila, a latência sobe e o HPA deve escalar.

### T3 — Recuperação durante a carga (tolerância a falhas)

Durante o T1, no degrau de 500 req/s, em outro terminal:

```bash
make drill
```

**Esperado:** um pico curto de erros ou latência e depois a volta ao normal. A taxa
de erro total do teste deve continuar abaixo de 1%.

### T4 — Usuários no dashboard

```bash
make teste-dashboard                       # degraus: 5 10 20 40 80 usuários
make teste-dashboard USUARIOS="10 30 60"   # degraus personalizados
```

Na primeira execução, o comando cria `.venv-testes` e baixa o Chromium (cerca de
150 MB). Cada "usuário" é uma aba real que abre o painel, espera os gráficos e o
recarrega a cada 5 s durante 60 s por degrau. O teste para no primeiro degrau que
estoura e imprime a estimativa de capacidade.

**Atenção a dois efeitos que distorcem o resultado:**
- **Mesma máquina:** os navegadores rodam no mesmo computador que o cluster e
  disputam a CPU com ele. Se a CPU da máquina chegar a 100% (veja no `htop`), quem
  estourou foi o **teste**, não o dashboard. Para um número confiável, rode o teste
  de outro computador da rede: `--url http://<ip-do-pc>:8080`.
- **Afinidade de sessão:** todas as abas saem do mesmo IP, então o
  `sessionAffinity: ClientIP` manda **todas para o mesmo pod**. O teste mede a
  capacidade **por pod**. A capacidade total, com usuários reais em IPs
  diferentes, é aproximadamente *capacidade por pod × réplicas*.

### T5 — Repetir com latência maior (opcional)

Simula um InterSCity mais lento:

```bash
kubectl -n monitoramento set env deployment/mock-interscity LATENCIA_MS=800
make teste-gateway CENARIO=simultaneos
```

**Esperado:** o número de envios simultâneos suportado se mantém, mas a vazão cai.
Pela Lei de Little, vazão ≈ conexões em andamento / latência.

## 5. Encerrar

```bash
make teste-restaurar     # volta para o InterSCity real e remove o falso
```

## 6. Tabela de resultados (preencher)

| Teste | Configuração | Capacidade medida | p95 no limite | Réplicas no pico | CPU/pod no pico | Observações |
|---|---|---|---|---|---|---|
| T1 vazão | 2–5 pods, latência 200 ms | ___ req/s | ___ ms | ___ | ___ m | |
| T2 simultâneos | idem | ___ conexões | ___ ms | ___ | ___ m | |
| T3 falha | 500 req/s + drill | erro total ___ % | — | — | — | |
| T4 dashboard | 2 pods (1 usado no teste) | ___ usuários/pod | ___ s | ___ | ___ m | |
| T5 latência 800 ms | idem | ___ conexões / ___ req/s | ___ ms | ___ | ___ m | |

Para converter em dispositivos: cada ESP32 envia **1 leitura a cada 5 s** (0,2 req/s),
então **dispositivos suportados ≈ vazão × 5**.

## 7. Referência: medição prévia fora do cluster

Para validar a mudança de código, medimos **um único processo** do gateway rodando
direto na máquina (4 CPUs), contra o InterSCity falso com 200 ms de latência, com o
k6 em carga constante por 15 s:

| Versão do gateway | 40 simultâneos | 200 simultâneos | 500 simultâneos |
|---|---|---|---|
| Assíncrono com `httpx` | 165 req/s, p95 0,44 s | 64 req/s, p95 7,7 s | 52 req/s, p95 19 s |
| **Assíncrono com `aiohttp` (atual)** | **193 req/s, p95 0,21 s** | **829 req/s, p95 0,33 s** | **824 req/s, p95 1,16 s** |

Leitura:
- Com `aiohttp`, **um pod** sustenta cerca de **800 envios/s**, o equivalente a uns
  **4000 dispositivos** enviando a cada 5 s.
- O teto aparece entre 200 e 500 conexões simultâneas: é o limite `MAX_CONEXOES=200`
  mais a CPU de um processo.
- O cliente `httpx` piorava à medida que a concorrência subia (um problema
  conhecido do pool de conexões dele). Por isso o gateway usa `aiohttp`.
- **Não houve nenhum erro** em nenhum dos cenários.

Esses números são de um processo **sem limite de CPU**. No cluster, os pods dividem
a mesma máquina com o kind, o dashboard e o próprio k6, então espere valores menores
por pod. Os testes T1 e T2 é que dão o número oficial.

## 8. Teste com o InterSCity real (25/11/2026)

```bash
make teste-real     # rampa 1 → 2 → 5 → 10 → 20 req/s, 1 min cada, com freio automático
```

O freio interrompe o teste se a taxa de erro passar de 5% ou o p95 passar de 3 s.
Neste teste, um único processo do gateway rodou fora do cluster, apontando para
`cidadesinteligentes.lsdi.ufma.br`.

| Métrica | Resultado |
|---|---|
| Envios | 1810 (pico de 20 req/s) |
| Erros | **0%** |
| Latência ponta a ponta (gateway + InterSCity) | mediana 35 ms · p95 42 ms · p99 94 ms · máx 205 ms |
| Conexões simultâneas necessárias | no máximo 2 |
| Leituras encontradas no Collector | **1811 de 1811** (o teste + 1 envio manual), conferidas minuto a minuto |

Conclusões:
- A 20 req/s o InterSCity real não mostrou nenhum sinal de esforço. 20 req/s
  equivalem a **cerca de 100 ESP32** enviando a cada 5 s.
- Não testamos acima de 20 req/s de propósito: o servidor é compartilhado. Para
  subir mais, combine antes com os responsáveis pelo LSDi/UFMA.
- Efeito colateral: o teste deixou 1811 leituras sintéticas no histórico real das
  duas máquinas (25/11, 19:34 a 19:39 UTC).

### Achados sobre a API do Collector

- **O Collector devolve no máximo 1000 registros por consulta**, somando todas as
  capabilities. **Corrigido:** o dashboard consulta por período (`start_date` e
  `end_date`) e divide ao meio toda janela que volta com 1000 registros. Conferido
  contra o servidor real: a contagem do dashboard bate exatamente com a contagem
  minuto a minuto do Collector.
- **Nomes de capability:** no Catalog, as máquinas estão cadastradas com
  `temperatura` e `vibração`, mas o gateway enviava `temp` e `vibra`, e o histórico
  ficou dividido em quatro séries. **Corrigido:** o gateway passou a enviar os nomes
  do Catalog, e o dashboard junta as séries antigas às novas.
- **Registros antigos com outro campo:** 47 leituras de outubro guardam o número em
  `temperature`, e não em `value`, e o dashboard as descartava. **Corrigido:** o
  valor é lido do primeiro campo numérico preenchido.
- **503 passageiro:** o Collector às vezes responde 503. O dashboard tenta de novo
  até 4 vezes, com espera crescente.

## 9. Teste autorizado: até 1000 req/s no InterSCity real

Autorizado pelo LSDi/UFMA. Com 1000 req/s, e cada ESP32 enviando 1 leitura a cada
5 s, o teste equivale a **cerca de 5000 ESP32**.

### Passo a passo

```bash
make cluster && make up            # se o cluster não estiver no ar
make build load deploy             # garante a versão atual do gateway no cluster
make teste-recursos                # 1x: cria "Teste de carga 01/02" no Catalog
make teste-autorizado-preparar     # 4 réplicas, recursos de teste, 1 envio de conferência
make teste-monitorar               # (outro terminal) HPA, CPU e pods ao vivo
make teste-autorizado              # pede "SIM" e dispara (~9 min)
make teste-autorizado-restaurar    # volta o gateway ao normal
```

### O que cada passo faz

| Passo | Efeito |
|---|---|
| `teste-recursos` | Cria 2 recursos no Catalog, com as capabilities `temperatura` e `vibração`, e grava os UUIDs em `testes/recursos-teste.env` (fora do git). Se o arquivo já existir, reaproveita os recursos. **As ~260 mil leituras vão para esses recursos, não para as máquinas reais.** |
| `teste-autorizado-preparar` | Acrescenta `maq-teste-01` e `maq-teste-02` ao gateway, desliga o log por envio, sobe o mínimo do HPA para 4 réplicas (máximo 6) e faz um envio de conferência, que deve voltar HTTP 201 |
| `teste-autorizado` | Roda o k6 com a rampa abaixo; pede confirmação antes |
| `teste-autorizado-restaurar` | Volta o gateway às máquinas do ConfigMap e o HPA para 2 a 5 réplicas |

### A carga

| Tempo | Envios/s | ESP32 equivalentes |
|---|---|---|
| 0–1 min | 10 → 50 | até 250 |
| 1–2 min | 50 → 100 | 500 |
| 2–3 min | 100 → 250 | 1 250 |
| 3–4 min | 250 → 500 | 2 500 |
| 4–5 min | 500 → 750 | 3 750 |
| 5–6 min | 750 → 1000 | 5 000 |
| 6–8 min | **1000 sustentados** | **5 000** |
| 8–8:50 | 1000 → 0 | — |

São cerca de **260 mil requisições** no total.

**Freio automático:** o teste para sozinho se a taxa de erro passar de 2% ou o p95
passar de 2 s. Os limites só são avaliados depois dos primeiros 30 s. Ctrl+C também
interrompe a qualquer momento.

### Como ler o resultado

- **Passou inteiro:** o sistema, com o InterSCity real, sustenta 1000 req/s, ou seja,
  5000 ESP32.
- **O freio disparou:** veja em que degrau. Para descobrir **quem** estourou:
  - **CPU da máquina a 100%** (`htop`): o limite é o **seu computador**. O kind, os
    4 pods e o k6 dividem as mesmas CPUs. Não é limite do sistema em si.
  - **CPU dos pods alta e réplicas no máximo** (`teste-monitorar`): o limite é o
    **gateway**.
  - **Erros 502 ou latência alta com CPU sobrando:** o limite é o **InterSCity**.
    Os logs do gateway (`make logs`) mostram as tentativas que falharam.
- O resumo completo fica em `testes/resultado-gateway-autorizado.json`.

### Cuidados

- Combine o horário com o LSDi e avise quando começar e terminar.
- Os recursos de teste ficam no Catalog depois do teste. Peça ao LSDi para
  removê-los, se quiserem.

### Disparar a partir de um computador Windows

O cluster continua no Linux; o Windows só gera a carga. Assim o k6 não disputa
CPU com o gateway.

1. **No Windows**, instale o k6 (PowerShell):
   `winget install k6 --source winget`. Depois feche e abra o PowerShell.
2. **No Windows**, tenha a pasta `testes/` do projeto, via `git clone` ou cópia.
3. **No Linux**, libere a porta do gateway na rede: `sudo ufw allow 8000/tcp`.
   Anote o IP com `ip -4 addr | grep inet`.
4. **No Linux**, prepare normalmente: `make teste-recursos` e
   `make teste-autorizado-preparar`. Deixe `make teste-monitorar` aberto.
5. **No Windows**, dentro da pasta do projeto:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\testes\disparar-windows.ps1 -IpLinux <ip-do-linux>
   ```
   O script confere se o gateway responde, pede `SIM` e dispara o mesmo cenário
   do `make teste-autorizado`.
6. **No Linux**, ao terminar: `make teste-autorizado-restaurar`.

Prefira os dois computadores **no cabo de rede**. O Wi-Fi aguenta o volume
(~1,5 MB/s), mas oscila a latência e pode disparar o freio por conta própria.
