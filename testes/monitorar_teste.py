"""Coleta, a cada 5 s, o estado de cada ponto da arquitetura durante um teste.

    python3 testes/monitorar_teste.py --saida testes/resultado-autorizado/

Grava:
  amostras.csv        uma linha por amostra (fila, réplicas, CPU, máquina)
  encaminhador.log    linhas STATS de todas as réplicas do encaminhador
                      (inclusive as que o KEDA criar/remover durante o teste)

Interrompa com Ctrl+C (ou SIGTERM) ao fim do teste.
"""

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time

NS = "monitoramento"
COMPONENTES = ("gateway", "encaminhador", "nats")


def sh(*cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return ""


def fila():
    """Estado do stream e do consumidor, lido do monitor HTTP do NATS."""
    for pod in ("nats-0", "nats-1", "nats-2"):
        bruto = sh("kubectl", "-n", NS, "exec", pod, "--",
                   "wget", "-qO-", "http://localhost:8222/jsz?consumers=true")
        try:
            d = json.loads(bruto)
            s = d["account_details"][0]["stream_detail"][0]
            c = s["consumer_detail"][0]
            return {
                "fila_pendente": s["state"]["messages"],
                "aceitas_total": s["state"]["last_seq"],
                "entregues_total": c["ack_floor"]["stream_seq"],
                "em_processamento": c["num_ack_pending"],
                "reentregas": c.get("num_redelivered", 0),
                "lider_stream": s["cluster"].get("leader", ""),
            }
        except (ValueError, KeyError, IndexError):
            continue
    return {}


def replicas():
    d = json.loads(sh("kubectl", "-n", NS, "get", "deploy", "-o", "json") or "{}")
    out = {}
    for item in d.get("items", []):
        nome = item["metadata"]["name"]
        if nome in ("gateway", "encaminhador"):
            out[f"rep_{nome}"] = item.get("status", {}).get("readyReplicas", 0)
    return out


def cpu_pods():
    out = {f"cpu_m_{c}": 0 for c in COMPONENTES}
    out.update({f"mem_mi_{c}": 0 for c in COMPONENTES})
    out.update({f"cpu_m_max_{c}": 0 for c in COMPONENTES})
    for linha in sh("kubectl", "-n", NS, "top", "pods", "--no-headers").splitlines():
        partes = linha.split()
        if len(partes) < 3:
            continue
        nome, cpu, mem = partes[0], int(partes[1].rstrip("m")), int(partes[2].rstrip("Mi"))
        for c in COMPONENTES:
            if nome.startswith(c + "-"):
                out[f"cpu_m_{c}"] += cpu
                out[f"mem_mi_{c}"] += mem
                out[f"cpu_m_max_{c}"] = max(out[f"cpu_m_max_{c}"], cpu)
    return out


def reinicios():
    d = json.loads(sh("kubectl", "-n", NS, "get", "pods", "-o", "json") or "{}")
    total = 0
    for p in d.get("items", []):
        for cs in p.get("status", {}).get("containerStatuses", []):
            total += cs.get("restartCount", 0)
    return {"reinicios_total": total}


def maquina():
    carga1 = open("/proc/loadavg").read().split()[0]
    mem = {}
    for linha in open("/proc/meminfo"):
        k, v = linha.split(":")
        mem[k] = int(v.split()[0])
    out = {
        "load1": float(carga1),
        "mem_usada_gb": round((mem["MemTotal"] - mem["MemAvailable"]) / 1024 ** 2, 2),
    }
    # CPU de cada nó do kind (cada nó é um container Docker)
    for linha in sh("docker", "stats", "--no-stream", "--format",
                    "{{.Name}} {{.CPUPerc}}").splitlines():
        nome, cpu = linha.split()
        if nome.startswith("monitoramento-"):
            out["cpu_no_" + nome.removeprefix("monitoramento-")] = float(cpu.rstrip("%"))
        elif "k6" in nome or nome.startswith("grafana"):
            out["cpu_k6"] = float(cpu.rstrip("%"))
    return out


def seguir_logs(saida, seguidos):
    """Abre um 'kubectl logs -f' para cada réplica nova do encaminhador."""
    pods = sh("kubectl", "-n", NS, "get", "pods", "-l", "app=encaminhador",
              "-o", "jsonpath={.items[*].metadata.name}").split()
    for pod in pods:
        if pod not in seguidos:
            arq = open(os.path.join(saida, "encaminhador.log"), "a")
            seguidos[pod] = subprocess.Popen(
                ["kubectl", "-n", NS, "logs", "-f", "--since=1s", pod],
                stdout=arq, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", required=True)
    ap.add_argument("--intervalo", type=float, default=5)
    args = ap.parse_args()
    os.makedirs(args.saida, exist_ok=True)

    seguidos = {}
    parar = []
    signal.signal(signal.SIGTERM, lambda *_: parar.append(1))
    signal.signal(signal.SIGINT, lambda *_: parar.append(1))

    arq = open(os.path.join(args.saida, "amostras.csv"), "w", newline="")
    escritor = None
    inicio = time.time()
    anterior = None
    while not parar:
        t0 = time.time()
        seguir_logs(args.saida, seguidos)
        linha = {"t_s": round(t0 - inicio), "hora": time.strftime("%H:%M:%S")}
        for coleta in (fila, replicas, cpu_pods, reinicios, maquina):
            try:
                linha.update(coleta())
            except Exception as exc:  # uma coleta falha não derruba as outras
                print(f"{coleta.__name__}: {exc}", file=sys.stderr)
        # taxas desde a amostra anterior (leituras/s)
        if anterior and "aceitas_total" in linha and "aceitas_total" in anterior:
            dt = t0 - anterior["_t"]
            linha["aceitas_por_s"] = round((linha["aceitas_total"] - anterior["aceitas_total"]) / dt, 1)
            linha["entregues_por_s"] = round((linha["entregues_total"] - anterior["entregues_total"]) / dt, 1)
        if escritor is None:
            campos = ["t_s", "hora", "aceitas_por_s", "entregues_por_s", "fila_pendente",
                      "em_processamento", "aceitas_total", "entregues_total", "reentregas",
                      "lider_stream", "rep_gateway", "rep_encaminhador",
                      *[f"cpu_m_{c}" for c in COMPONENTES],
                      *[f"cpu_m_max_{c}" for c in COMPONENTES],
                      *[f"mem_mi_{c}" for c in COMPONENTES],
                      "reinicios_total", "load1", "mem_usada_gb",
                      "cpu_no_control-plane", "cpu_no_worker", "cpu_no_worker2", "cpu_k6"]
            escritor = csv.DictWriter(arq, fieldnames=campos, extrasaction="ignore")
            escritor.writeheader()
        escritor.writerow(linha)
        arq.flush()
        print(" ".join(f"{k}={linha.get(k, '')}" for k in
                       ("hora", "aceitas_por_s", "entregues_por_s", "fila_pendente",
                        "rep_gateway", "rep_encaminhador", "cpu_m_gateway", "load1")),
              flush=True)
        linha["_t"] = t0
        anterior = linha
        time.sleep(max(0, args.intervalo - (time.time() - t0)))

    for p in seguidos.values():
        p.terminate()


if __name__ == "__main__":
    main()
