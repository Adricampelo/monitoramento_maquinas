# Teste autorizado contra o InterSCity real: arquitetura com fila

**Data:** 28/11/2026, 18:43–18:52 UTC (15:43–15:52 em Brasília)
**Carga:** cenário `autorizado` do [carga_gateway.js](carga_gateway.js): degraus 50 → 100 → 250 → 500 → 750 → 1000 req/s
(1 min cada, rampa linear dentro do degrau), 1000 req/s sustentados por 2 min, descida em 20 s.
Freio: aborta se o erro passar de 2% ou o p95 passar de 2 s.
**Destino:** InterSCity real (`cidadesinteligentes.lsdi.ufma.br/interscity_lh`), recursos de teste
`maq-teste-01` e `maq-teste-02`.
**Dados brutos:** [resultado-autorizado/](resultado-autorizado/) (`amostras.csv` a cada 5 s, `k6.json`,
`encaminhador-completo.log`, `collector-atraso.csv`).

## Resumo

| | Antes (gateway síncrono) | Agora (gateway → fila → encaminhador) |
|---|---|---|
| Resultado | abortado em 2m22s | **completou os 8m20s** |
| Taxa máxima | ~155 req/s | **1000 req/s sustentados** |
| Requisições | 8.377 | **259.299** |
| Erros | 0,87% | **0%** |
| p95 / p99 / máx | 2,16 s / 13,45 s / 15 s | **1,06 ms / 1,35 ms / 14 ms** |
| Leituras confirmadas pelo InterSCity (Adaptor, HTTP 201) | — | **259.299 de 259.299** |
| POSTs feitos ao InterSCity | 1 por leitura | **3.277** (79 leituras por POST, em média) |

**Chegamos a 1000 req/s?** Sim, até o Resource Adaptor do InterSCity. O sistema recebeu,
enfileirou e entregou 1000 leituras/s por 2 minutos sem erro, e a fila oscilou entre 2 mil e 7 mil
leituras sem crescer de forma contínua. A fila esvaziou ~20 s depois do fim da carga.

**Ressalva importante:** o Adaptor confirma com 201 antes de gravar. O armazenamento interno do
InterSCity (Data Collector, que o dashboard consulta) **não acompanhou**: ele grava em torno de
37 leituras/s e vai levar quase 2 horas para gravar o que o teste enviou em 8 minutos. Ver o [ponto 6](#6-interscity-data-collector-armazenamento).
Do dispositivo ao dashboard, portanto, o sistema não sustenta 1000 leituras/s, e o limite está dentro do InterSCity.

## Máquina

16 vCPUs, 14 GB de RAM. Cluster kind com 3 nós (1 control-plane + 2 workers). O k6 rodou na mesma máquina.

| Medida | Pico durante o teste |
|---|---|
| Load average (1 min) | 2,57 (de 16 vCPUs) |
| RAM usada no host | 9,2 GB (8,6 GB antes do teste) |
| CPU do nó `worker2` | 132% (1,3 vCPU) |
| CPU do nó `worker` | 107% (1,1 vCPU) |
| CPU do nó `control-plane` | 19% |
| CPU do k6 | 27% |
| Reinícios de pods | 0 |

A máquina ficou folgada o tempo todo, usando cerca de 2,5 das 16 vCPUs no pico.

## O que aconteceu em cada ponto da arquitetura

```
k6 (ESP32s) ──► gateway (4) ──► NATS JetStream (3, R3) ──► encaminhador (2→4) ──► InterSCity Adaptor ──► Data Collector
```

Tabela por degrau, com médias e picos das amostras de 5 s:

| Degrau (req/s) | Aceitas/s | Entregues/s | Fila máx. | Atraso na fila máx. | Gateways | Encaminhadores | CPU gateway (total) | CPU NATS (total) | CPU encaminhador |
|---|---|---|---|---|---|---|---|---|---|
| 10 → 50 | 29 | — | 29 | ~1 s | 4 | 2 | 36m | 67m | 17m |
| 50 → 100 | 74 | 101 | 67 | <1 s | 4 | 2 | 61m | 124m | 29m |
| 100 → 250 | 172 | 172 | 213 | <1 s | 4 | 2 | 123m | 220m | 45m |
| 250 → 500 | 371 | 361 | 558 | ~1 s | 4 | 2 | 219m | 435m | 80m |
| 500 → 750 | 621 | 616 | 1.453 | ~3 s | 4 | 2 | 345m | 651m | 108m |
| 750 → 1000 | 871 | 840 | 3.834 | ~4 s | 4 | 2 | 432m | 596m | 76m |
| **1000 sustentado** | **1000** | **1001** | **7.387** | **~7 s** | 4 | **4** | 457m | 678m | 156m |
| descida | 275 | 342 | 1.512 | — | 4 | 4 | — | — | — |

"Atraso na fila" é a fila dividida pela taxa de chegada, ou seja, quanto tempo uma leitura esperou
até ser entregue ao InterSCity. "1000m" equivale a 1 vCPU.

### 1. Gerador de carga (k6, papel das ESP32)

- 259.299 requisições, todas `202 Accepted`, sem nenhum erro. O freio nunca disparou.
- Latência praticamente constante do começo ao fim: mediana 0,70 ms, p95 1,06 ms, p99 1,35 ms.
- A latência deixou de depender do InterSCity. No teste anterior, a cauda (p99 de 13 s) era o tempo
  de espera pelo servidor externo.

### 2. Gateway (4 réplicas, HPA 4–8 por CPU)

- A 1000 req/s, as 4 réplicas somaram 457m de CPU (no máximo 127m por pod), o que dá cerca de
  **0,46 vCPU por 1000 req/s**. Memória total: 141 MiB.
- O HPA não escalou porque a média ficou em ~45%, abaixo do alvo de 50% sobre o request de 250m.
  No ensaio com o InterSCity falso ele subiu para 6 réplicas a 1500–2000 req/s.
  Agora a CPU acompanha a carga, ao contrário do teste anterior, em que ficou em 2–18%.
- Nenhuma publicação recusada (`503`) e nenhum reinício.

### 3. NATS JetStream (3 servidores, stream R3)

- Foi o componente que mais gastou CPU: **~680m a 1000 req/s**, somando os 3 servidores. Cada leitura
  é gravada em disco e replicada para os outros 2 por consenso Raft antes de o gateway responder.
  Memória: 165 MiB no total.
- Fila máxima de 7.387 leituras (~7 s de atraso) no trecho sustentado, quando o InterSCity ficou
  lento (ver o ponto 5). Ela absorveu a oscilação e voltou a 2–3 mil em seguida. Limite configurado: 1 GB.
- O líder do stream ficou em `nats-1` o teste inteiro. Ao final, todas as leituras publicadas foram
  confirmadas (`ack_floor` = `last_seq`) e nenhuma ficou pendente.

### 4. Encaminhador (KEDA 2–6 pela fila)

- **3.277 POSTs para 259.299 leituras.** O tamanho do lote cresceu com a carga: 2 leituras por POST no
  início, 120 a 500 req/s e 235 no pico. Com isso, o InterSCity recebeu no máximo ~9 POSTs/s,
  em vez de 1000.
- **O KEDA reagiu à fila.** Quando ela passou de 4 mil leituras (2.000 por réplica), aos ~6m20s,
  subiu de 2 para 4 réplicas, e voltou para 2 depois do *cooldown*. Este era o ponto cego do teste
  anterior: a CPU do encaminhador nunca passou de 156m e não teria disparado um HPA por CPU.
- **7 POSTs falharam**, entre 18:49:45 e 18:49:58: 4 timeouts de 15 s e 3 `connection reset` do
  InterSCity, somando 1.643 leituras. Todas voltaram à fila (`nak` com 2 s de atraso) e foram
  reentregues com sucesso. O dispositivo não percebeu nada.

### 5. InterSCity: Resource Adaptor (entrada)

- Aceitou com `201` **todas as 259.299 leituras**.
- O tempo de resposta cresceu com o **volume de leituras**, não com o número de requisições.
  Mesmo com poucos POSTs/s, a latência média do POST subiu de ~80 ms (lotes pequenos) para 450 ms
  (18:48), 1,3–2,1 s (18:49–18:50) e no pior caso 15,9 s (timeout). O custo do InterSCity é por
  leitura gravada, e o lote só elimina o custo por requisição.

### 6. InterSCity: Data Collector (armazenamento)

O Adaptor publica as leituras numa fila interna do InterSCity, e o Data Collector as grava depois,
de forma assíncrona. Consultando o Collector após o teste:

- **Nenhuma leitura perdida até onde o Collector chegou.** Contando janelas curtas e comparando com a
  taxa que o k6 enviava em cada instante: 278 gravadas para ~285 enviadas (18:45:30–33), 303 para
  ~300 (18:46:00–02), 252 para ~250 (18:46:40–41), 334 para ~320 (18:47:00–01).
- **Mas o Collector está muito atrasado.** Às 18:53:27, 1 minuto depois do fim do teste, a leitura
  mais recente gravada era de 18:46:46, ainda do degrau de ~280 req/s.
- **Taxa de gravação medida: ~37 leituras/s** (~74 medições/s, já que cada leitura tem temperatura e
  vibração). Acompanhamos por 20 minutos ([collector-atraso.csv](resultado-autorizado/collector-atraso.csv)):
  entre 18:57:50 e 19:17:55, o Collector avançou só de 18:47:16 para 18:48:37 no tempo do teste. Nesses
  81 segundos o k6 enviou ~44,7 mil leituras, que levaram 1.205 segundos para ser gravadas.
- Às 19:18 ainda faltavam ~187 mil leituras do teste. No ritmo medido, a gravação deve terminar por
  volta de 20:40 UTC, ~1h50 depois do fim da carga, se o servidor não tiver outra carga.
- O mesmo deve ter acontecido no teste síncrono anterior: o Adaptor aceitava ~155 req/s, mas o Collector
  grava ~37/s.

**Consequência:** com carga alta, o dashboard (que lê o Collector) mostra dados com atraso crescente,
embora nenhuma leitura se perca. É um limite do servidor do InterSCity e não pode ser resolvido
do nosso lado, porque já entregamos tudo e recebemos o 201.

## Conclusões para o relatório

1. **Desacoplar com uma fila resolveu o gargalo de entrada.** O sistema foi de ~155 para 1000 req/s
   no mesmo InterSCity, com a latência caindo de segundos para ~1 ms e os erros de 0,87% para 0%.
2. **O sistema continua distribuído e com mais tolerância a falhas:** produtores (gateway) e
   consumidores (encaminhador) escalam separadamente, a fila é replicada por consenso (Raft) e uma
   falha do InterSCity vira atraso, não erro.
3. **Envio em lote foi o que permitiu respeitar o InterSCity:** 3.277 requisições em vez de 259.299.
   O ganho depende de quantas leituras de **um mesmo recurso** chegam por janela. Aqui foram 2
   recursos a 500 leituras/s cada. Com 5000 recursos enviando a cada 5 s (0,2 leitura/s por recurso),
   uma janela de 1 s quase não agrupa nada e o InterSCity voltaria a receber ~1000 POSTs/s. Nesse
   cenário seria preciso uma janela bem maior, ao custo de mais atraso.
4. **Escalar pela métrica certa:** o HPA por CPU funciona para o gateway, que agora gasta CPU, e o
   KEDA pelo tamanho da fila funciona para o encaminhador, que só espera rede.
5. **Capacidade medida de cada parte:**
   - nosso cluster, com o InterSCity falso: **≥ 2000 req/s**, p99 de 1,4 ms, 0 erros. O cenário parou em 2000 req/s sem encontrar o limite (dados em [resultado-mock/](resultado-mock/));
   - InterSCity Adaptor: aceitou ~1000 leituras/s em lote, com latência subindo e 7 falhas pontuais;
   - InterSCity Data Collector: **~37 leituras/s gravadas**, e esse é o gargalo real de ponta a ponta.
6. **O `202` muda o contrato:** a resposta quer dizer "aceito e persistido na fila", não "gravado no
   InterSCity". É consistência eventual, uma troca consciente de consistência por disponibilidade.

## Limitações deste ensaio

- **Só 2 nós workers.** O control-plane do kind não recebe pods, então 2 dos 3 servidores do NATS
  ficaram no mesmo nó (`worker2`). A queda desse nó derrubaria o quórum. Num cluster real seriam 3+ nós.
- **2 recursos de teste.** Isso favorece o envio em lote; ver a conclusão 3.
- **Entrega "pelo menos uma vez".** Se um encaminhador cair entre o POST e o `ack`, a leitura é
  reentregue e pode ficar duplicada no InterSCity. Neste teste não houve queda de encaminhador.
- **Gerador e cluster na mesma máquina.** O k6 usou no máximo 27% de 1 vCPU, então não interferiu.

## Como reproduzir

```bash
make teste-autorizado-preparar
python3 testes/monitorar_teste.py --saida testes/resultado-autorizado &   # coleta a cada 5 s
make teste-autorizado
make teste-autorizado-restaurar
```
