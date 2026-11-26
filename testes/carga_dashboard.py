"""Teste de carga do dashboard: N navegadores reais abertos ao mesmo tempo.

O Streamlit mantém uma sessão Python e um websocket por aba aberta, então
um teste HTTP simples não mede a carga real. Aqui cada "usuário" é uma aba
do Chromium (Playwright) que abre o dashboard, espera os gráficos e fica
interagindo (troca a variável monitorada) durante o teste.

    .venv-testes/bin/python testes/carga_dashboard.py --usuarios 5 10 20 40 80

Para cada degrau: tempo até o painel aparecer (mediana e p95), erros e
tempo de resposta a uma interação.
"""

import argparse
import asyncio
import statistics
import time

from playwright.async_api import async_playwright

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://localhost:8080")
parser.add_argument("--usuarios", type=int, nargs="+", default=[5, 10, 20, 40])
parser.add_argument("--segundos", type=int, default=60, help="duração de cada degrau")
parser.add_argument("--limite-s", type=float, default=10, help="tempo máximo aceitável para carregar")
args = parser.parse_args()

TEXTO = "Estado das máquinas"  # aparece quando o painel termina de montar


def p95(v):
    return sorted(v)[max(0, int(len(v) * 0.95) - 1)] if v else float("nan")


async def usuario(browser, fim, cargas, interacoes, erros):
    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    try:
        t0 = time.perf_counter()
        await page.goto(args.url, timeout=60_000)
        await page.get_by_text(TEXTO).first.wait_for(timeout=60_000)
        cargas.append(time.perf_counter() - t0)

        # interação: reexecuta o painel a cada 5 s, como um usuário navegando
        while time.perf_counter() < fim:
            await asyncio.sleep(5)
            t1 = time.perf_counter()
            await page.keyboard.press("r")  # atalho do Streamlit: reexecuta o script
            await page.get_by_text(TEXTO).first.wait_for(timeout=60_000)
            interacoes.append(time.perf_counter() - t1)
    except Exception as e:  # noqa: BLE001
        erros.append(type(e).__name__)
    finally:
        await ctx.close()


async def degrau(browser, n):
    cargas, interacoes, erros = [], [], []
    fim = time.perf_counter() + args.segundos
    await asyncio.gather(*(usuario(browser, fim, cargas, interacoes, erros) for _ in range(n)))
    ok = not erros and cargas and p95(cargas) <= args.limite_s
    print(f"{n:>4} usuários | carga mediana {statistics.median(cargas) if cargas else float('nan'):5.1f}s "
          f"p95 {p95(cargas):5.1f}s | interação p95 {p95(interacoes):5.1f}s | "
          f"erros {len(erros):>3} | {'OK' if ok else 'ESTOUROU'}")
    return ok


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        print(f"alvo: {args.url} | {args.segundos}s por degrau | limite p95 {args.limite_s}s")
        for n in args.usuarios:
            if not await degrau(browser, n):
                print(f"→ capacidade estimada: entre o degrau anterior e {n} usuários")
                break
        await browser.close()


asyncio.run(main())
