
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
import pandas as pd
import streamlit as st
import plotly.express as px

# ============================================================
# CONFIGURAÇÃO
# ============================================================

# Formato: "Nome=uuid,Nome=uuid" (vem do ConfigMap no Kubernetes)
MAQUINAS = dict(
    item.split("=", 1)
    for item in os.getenv(
        "MAQUINAS_DASHBOARD",
        "Máquina 001=bcb2ade2-946f-4df2-a501-56e41c259cb7,"
        "Máquina 002=41bdd388-b2c1-4fc4-99eb-8668c4ef5c07",
    ).split(",")
)

INTERSCITY_URL = os.getenv(
    "INTERSCITY_URL",
    "https://cidadesinteligentes.lsdi.ufma.br/interscity_lh",
).rstrip("/")

BASE_URL = INTERSCITY_URL + "/collector/resources/{uuid}/data"

# O Collector devolve no máximo 1000 registros por consulta (somando todas as
# capabilities). Quando uma janela de tempo bate nesse teto, ela é dividida ao
# meio e consultada de novo, até cada pedaço caber numa resposta.
TETO_COLLECTOR = 1000

# Início do histórico usado no período "Tudo"
HISTORICO_DESDE = pd.Timestamp(os.getenv("HISTORICO_DESDE", "2026-01-01"), tz="UTC")

# Janela mínima consultada, para o status (última medição) continuar correto
# mesmo quando o período exibido é curto
JANELA_MINIMA = pd.Timedelta(days=7)

# Nomes antigos enviados pelo gateway -> nomes cadastrados no Catalog.
# O histórico fica numa série só, qualquer que seja o nome usado no envio.
SINONIMOS = {
    "temp": "temperatura",
    "vibra": "vibração",
}

INTERVALO = 30
LIMITE_ONLINE_MIN = 10
FUSO = "America/Fortaleza"

LIMITES = {
    "temperatura": {
        "rotulo": "Temperatura",
        "unidade": "°C",
        "padrao": 80.0,
    },
    "vibração": {
        "rotulo": "Vibração",
        "unidade": "mm/s",
        "padrao": 7.0,
    },
}

PERIODOS = {
    "Última hora": pd.Timedelta(hours=1),
    "Últimas 24 h": pd.Timedelta(hours=24),
    "Últimos 7 dias": pd.Timedelta(days=7),
    "Tudo": None,
}

# Tons médios: contraste suficiente tanto no fundo claro quanto no escuro
COR = "#0E8A96"
COR_ALERTA = "#E08E0B"
COR_CRITICA = "#E5484D"
COR_OK = "#2F9E5B"


st.set_page_config(
    page_title="Monitoramento Industrial",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)


# ============================================================
# ESTILO
# ============================================================

st.markdown(
    f"""
    <style>
    @import url(
      'https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap'
    );

    html, body, [class*="css"] {{
        font-family: 'IBM Plex Sans', sans-serif;
    }}

    .block-container {{
        padding-top: 2rem;
        max-width: 1400px;
    }}

    /* As superfícies derivam da cor do texto do tema do Streamlit
       (escura no light, clara no dark), então acompanham o tema
       escolhido no app, e não o do sistema operacional. */
    :root {{
        --superficie: color-mix(in srgb, currentColor 4%, transparent);
        --superficie-2: color-mix(in srgb, currentColor 6%, transparent);
        --borda: color-mix(in srgb, currentColor 18%, transparent);
    }}

    div[data-testid="stMetric"] {{
        background: var(--superficie);
        border: 1px solid var(--borda);
        border-left: 4px solid {COR};
        border-radius: 8px;
        padding: 14px 18px;
    }}

    div[data-testid="stMetricLabel"] p {{
        color: inherit;
        opacity: 0.8;
        font-weight: 500;
    }}

    .cartao-status {{
        padding: 10px 12px;
        border-radius: 8px;
        background: var(--superficie-2);
        border: 1px solid var(--borda);
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# CONSULTA À API
# ============================================================

def _data_iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def _consultar_janela(uuid, inicio, fim):
    """Uma consulta ao Collector. Devolve (capabilities, total de registros).

    O servidor às vezes responde 503 passageiro: tenta até 4 vezes, com
    espera crescente, antes de desistir.
    """
    for tentativa in range(4):
        try:
            resposta = requests.get(
                BASE_URL.format(uuid=uuid),
                params={"start_date": _data_iso(inicio), "end_date": _data_iso(fim)},
                timeout=20,
            )
            if resposta.status_code < 500:
                break
        except (requests.ConnectionError, requests.Timeout):
            if tentativa == 3:
                raise
        time.sleep(0.5 * 2 ** tentativa)  # 0,5 s, 1 s, 2 s
    resposta.raise_for_status()

    recurso = next(
        (r for r in resposta.json().get("resources", []) if r.get("uuid") == uuid),
        {},
    )
    capacidades = recurso.get("capabilities", {}) or {}
    total = sum(len(v) for v in capacidades.values() if isinstance(v, list))
    return capacidades, total


def _consultar_paginado(uuid, inicio, fim, profundidade=0):
    """Busca [inicio, fim] dividindo a janela ao meio sempre que bater no teto."""
    capacidades, total = _consultar_janela(uuid, inicio, fim)

    if total < TETO_COLLECTOR or fim - inicio <= pd.Timedelta(seconds=1) or profundidade > 30:
        return [capacidades]

    meio = inicio + (fim - inicio) / 2
    # end_date é inclusivo: a segunda metade começa 1 ms depois do meio
    return (
        _consultar_paginado(uuid, inicio, meio, profundidade + 1)
        + _consultar_paginado(uuid, meio + pd.Timedelta(milliseconds=1), fim, profundidade + 1)
    )


@st.cache_data(ttl=INTERVALO, show_spinner=False)
def consultar_dados(uuid, periodo_nome):
    """Histórico completo do período, juntando todas as páginas."""
    agora = pd.Timestamp.now(tz="UTC")
    janela = PERIODOS[periodo_nome]

    if janela is None:
        inicio = HISTORICO_DESDE
    else:
        inicio = agora - max(janela, JANELA_MINIMA)

    # 1 dia à frente: tolera relógio adiantado no dispositivo
    fim = agora + pd.Timedelta(days=1)

    # A janela inicial é dividida em 8 partes consultadas em paralelo
    passo = (fim - inicio) / 8
    partes = [
        (inicio + passo * i + (pd.Timedelta(milliseconds=1) if i else pd.Timedelta(0)),
         inicio + passo * (i + 1))
        for i in range(8)
    ]
    # paralelismo baixo de propósito: o servidor é compartilhado
    with ThreadPoolExecutor(max_workers=2) as pool:
        paginas = [
            pagina
            for resultado in pool.map(lambda p: _consultar_paginado(uuid, *p), partes)
            for pagina in resultado
        ]

    # junta as páginas numa estrutura igual à resposta original
    capacidades = {}
    for pagina in paginas:
        for nome, registros in pagina.items():
            if isinstance(registros, list):
                capacidades.setdefault(nome, []).extend(registros)

    return {"resources": [{"uuid": uuid, "capabilities": capacidades}]}


def montar_historico(recurso):
    tabelas = []

    capacidades = recurso.get("capabilities", {})

    for capacidade, registros in capacidades.items():
        if not isinstance(registros, list) or not registros:
            continue

        df = pd.DataFrame(registros)

        if "date" not in df.columns:
            continue

        # O valor costuma vir em "value", mas registros antigos usam outros
        # nomes (ex.: "temperature"). Usa o primeiro campo preenchido,
        # dando preferência a "value" e ao nome da capability.
        candidatas = ["value", capacidade] + [
            c for c in df.columns if c not in ("value", capacidade, "date")
        ]
        candidatas = [c for c in candidatas if c in df.columns]

        if not candidatas:
            continue

        valores = df[candidatas].apply(pd.to_numeric, errors="coerce")

        df = pd.DataFrame({
            "Data": df["date"],
            "Valor": valores.bfill(axis=1).iloc[:, 0],
        })

        df["Data"] = pd.to_datetime(
            df["Data"],
            errors="coerce",
            utc=True,
        )

        df["Valor"] = pd.to_numeric(
            df["Valor"],
            errors="coerce",
        )

        df["Variável"] = SINONIMOS.get(capacidade, capacidade)

        tabelas.append(df)

    if not tabelas:
        return pd.DataFrame(
            columns=["Data", "Valor", "Variável"]
        )

    historico = pd.concat(tabelas, ignore_index=True)

    historico = historico.dropna(
        subset=["Data", "Valor"]
    )

    # páginas vizinhas podem repetir registros na borda
    historico = historico.drop_duplicates(
        subset=["Data", "Valor", "Variável"]
    )

    return historico.sort_values("Data").reset_index(drop=True)


def buscar_historico(maquina, uuid, periodo_nome):
    dados = consultar_dados(uuid, periodo_nome)

    recursos = dados.get("resources", [])

    recurso = next(
        (
            r for r in recursos
            if r.get("uuid") == uuid
        ),
        None,
    )

    if recurso is None:
        return pd.DataFrame(
            columns=["Data", "Valor", "Variável"]
        )

    return montar_historico(recurso)


# ============================================================
# LIMITES E FORMATAÇÃO
# ============================================================

def regra_da_variavel(variavel, limites_usuario):
    nome = variavel.lower()

    for chave, configuracao in LIMITES.items():
        if chave in nome:
            return (
                configuracao,
                limites_usuario[chave],
            )

    return None, None


def formatar_data(data):
    return data.tz_convert(FUSO).strftime(
        "%d/%m/%Y %H:%M:%S"
    )


def filtrar_periodo(df, periodo_nome):
    janela = PERIODOS[periodo_nome]

    if janela is None or df.empty:
        return df.copy()

    agora = pd.Timestamp.now(tz="UTC")

    return df[df["Data"] >= agora - janela].copy()


# ============================================================
# BARRA LATERAL
# ============================================================

with st.sidebar:
    st.header("Configurações")

    modo = st.radio(
        "Visualização",
        [
            "Máquina individual",
            "Comparar máquinas",
        ],
    )

    maquina_nome = st.selectbox(
        "Máquina",
        list(MAQUINAS.keys()),
        disabled=(modo == "Comparar máquinas"),
    )

    periodo_nome = st.selectbox(
        "Período do histórico",
        list(PERIODOS.keys()),
        index=1,
    )

    st.divider()
    st.subheader("Limites de alerta")

    limites_usuario = {}

    for chave, cfg in LIMITES.items():
        limites_usuario[chave] = st.number_input(
            f"{cfg['rotulo']} máxima ({cfg['unidade']})",
            min_value=0.0,
            value=cfg["padrao"],
            step=1.0,
            key=f"limite_{chave}",
        )

    st.caption(
        "Os limites são configuráveis e devem ser "
        "ajustados conforme as especificações das máquinas."
    )

    st.divider()

    if st.button(
        "Atualizar agora",
        use_container_width=True,
        type="primary",
    ):
        consultar_dados.clear()
        st.rerun()

    st.caption(
        f"Atualização automática a cada {INTERVALO} segundos."
    )


# ============================================================
# CABEÇALHO
# ============================================================

st.title("Monitoramento Industrial")
st.caption(
    "InterSCity | Acompanhamento de variáveis industriais"
)

st.divider()


# ============================================================
# PAINEL PRINCIPAL
# ============================================================

@st.fragment(run_every=INTERVALO)
def painel(modo, maquina_nome, periodo_nome, limites_usuario):

    try:
        if modo == "Máquina individual":
            maquinas_consultar = [maquina_nome]
        else:
            maquinas_consultar = list(MAQUINAS.keys())

        historicos = {}

        with st.spinner("Consultando medições..."):
            for nome in maquinas_consultar:
                uuid = MAQUINAS[nome]
                historicos[nome] = buscar_historico(
                    nome,
                    uuid,
                    periodo_nome,
                )

        # --------------------------------------------
        # Verificação dos dados
        # --------------------------------------------

        for nome, historico in historicos.items():
            if historico.empty:
                st.warning(
                    f"{nome}: não foram encontradas medições "
                    "válidas na resposta da API."
                )

        historicos = {
            nome: df
            for nome, df in historicos.items()
            if not df.empty
        }

        if not historicos:
            st.info(
                "Não há dados para exibir. Verifique se as "
                "máquinas possuem medições cadastradas."
            )
            return

        # --------------------------------------------
        # Status e alertas
        # --------------------------------------------

        agora = pd.Timestamp.now(tz="UTC")
        resumo = []

        for nome, historico in historicos.items():
            ultima_data = historico["Data"].max()

            minutos = (
                agora - ultima_data
            ).total_seconds() / 60

            alertas = []

            for variavel in historico["Variável"].unique():
                cfg, limite = regra_da_variavel(
                    variavel,
                    limites_usuario,
                )

                if cfg is None:
                    continue

                df_var = historico[
                    historico["Variável"] == variavel
                ]

                ultimo = df_var.loc[
                    df_var["Data"].idxmax()
                ]

                if ultimo["Valor"] > limite:
                    alertas.append(
                        f"{cfg['rotulo']}: "
                        f"{ultimo['Valor']:.2f} "
                        f"{cfg['unidade']}"
                    )

            if minutos > LIMITE_ONLINE_MIN:
                status = "Sem dados recentes"
                cor = COR_ALERTA
            elif alertas:
                status = "Limite ultrapassado"
                cor = COR_CRITICA
            else:
                status = "Sem limites ultrapassados"
                cor = COR_OK

            resumo.append({
                "Máquina": nome,
                "Status": status,
                "Cor": cor,
                "Última medição": ultima_data,
                "Alertas": alertas,
                "Minutos sem dados": minutos,
            })

        st.subheader("Estado das máquinas")

        colunas = st.columns(len(resumo))

        for coluna, item in zip(colunas, resumo):
            with coluna:
                st.markdown(f"### {item['Máquina']}")

                st.markdown(
                    f"""
                    <div class="cartao-status"
                         style="border-left: 4px solid {item['Cor']};">
                        <strong>{item['Status']}</strong><br>
                        <span style="font-size: 0.85rem;">
                            Última medição:<br>
                            {formatar_data(item['Última medição'])}
                        </span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                for alerta in item["Alertas"]:
                    st.error(alerta)

        st.divider()

        # --------------------------------------------
        # Seleção de variável
        # --------------------------------------------

        todas_variaveis = sorted({
            variavel
            for historico in historicos.values()
            for variavel in historico["Variável"].unique()
        })

        variavel = st.selectbox(
            "Variável monitorada",
            todas_variaveis,
            key=f"variavel_{modo}_{maquina_nome}",
        )

        # --------------------------------------------
        # Montagem dos dados do período
        # --------------------------------------------

        partes = []

        for nome, historico in historicos.items():
            df = filtrar_periodo(
                historico,
                periodo_nome,
            )

            df = df[df["Variável"] == variavel].copy()

            if df.empty:
                continue

            df["Máquina"] = nome
            partes.append(df)

        if not partes:
            st.info(
                "Não há medições dessa variável no período "
                "selecionado. Escolha um período maior."
            )
            return

        dados_grafico = pd.concat(
            partes,
            ignore_index=True,
        )

        dados_grafico["Data"] = (
            dados_grafico["Data"].dt.tz_convert(FUSO)
        )

        cfg, limite = regra_da_variavel(
            variavel,
            limites_usuario,
        )

        # --------------------------------------------
        # Indicadores
        # --------------------------------------------

        if modo == "Máquina individual":
            nome = maquina_nome

            df = dados_grafico[
                dados_grafico["Máquina"] == nome
            ].sort_values("Data")

            ultima = df.iloc[-1]

            c1, c2, c3, c4 = st.columns(4)

            c1.metric(
                "Último valor",
                f"{ultima['Valor']:.2f}",
            )

            c2.metric(
                "Média",
                f"{df['Valor'].mean():.2f}",
            )

            c3.metric(
                "Mínimo",
                f"{df['Valor'].min():.2f}",
            )

            c4.metric(
                "Máximo",
                f"{df['Valor'].max():.2f}",
            )

            if limite is not None:
                quantidade = int(
                    (df["Valor"] > limite).sum()
                )

                st.caption(
                    f"Registros acima do limite de "
                    f"{limite:.2f} {cfg['unidade']}: "
                    f"{quantidade} de {len(df)}."
                )

        else:
            st.caption(
                "Comparação baseada nas medições disponíveis "
                "de cada máquina no período selecionado."
            )

            indicadores = []

            for nome, grupo in dados_grafico.groupby("Máquina"):
                grupo = grupo.sort_values("Data")

                indicadores.append({
                    "Máquina": nome,
                    "Último valor": grupo.iloc[-1]["Valor"],
                    "Média": grupo["Valor"].mean(),
                    "Mínimo": grupo["Valor"].min(),
                    "Máximo": grupo["Valor"].max(),
                    "Registros": len(grupo),
                })

            st.dataframe(
                pd.DataFrame(indicadores),
                use_container_width=True,
                hide_index=True,
            )

        # --------------------------------------------
        # Gráfico
        # --------------------------------------------

        st.subheader(f"Histórico de {variavel}")

        fig = px.line(
            dados_grafico.sort_values("Data"),
            x="Data",
            y="Valor",
            color="Máquina",
            markers=True,
            labels={
                "Data": "Data e hora",
                "Valor": variavel,
                "Máquina": "Equipamento",
            },
        )

        if limite is not None:
            fig.add_hline(
                y=limite,
                line_dash="dash",
                line_color=COR_CRITICA,
                annotation_text=(
                    f"Limite: {limite:g} {cfg['unidade']}"
                ),
            )

        fig.update_layout(
            height=430,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            hovermode="x unified",
            margin=dict(l=10, r=10, t=25, b=10),
        )

        fig.update_yaxes(
            title=cfg["unidade"] if cfg else variavel,
            gridcolor="rgba(128,128,128,0.25)",
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
        )

  
        # --------------------------------------------
        # Tabela e exportação
        # --------------------------------------------

        st.subheader("Histórico de medições")

        tabela = dados_grafico.sort_values(
            "Data",
            ascending=False,
        ).copy()

        if limite is not None:
            tabela["Situação"] = tabela["Valor"].apply(
                lambda valor: (
                    "Acima do limite"
                    if valor > limite
                    else "Normal"
                )
            )

        # Aplica vermelho aos valores acima do limite
        def destacar_valores(linha):
            estilos = [""] * len(linha)

            if limite is not None and linha["Valor"] > limite:
                estilos[linha.index.get_loc("Valor")] = (
                    "background-color: rgba(229,72,77,0.22); font-weight: bold;"
                )

                if "Situação" in linha.index:
                    estilos[linha.index.get_loc("Situação")] = (
                        "background-color: rgba(229,72,77,0.22); font-weight: bold;"
                    )

            return estilos

        st.dataframe(
            tabela.style.apply(
                destacar_valores,
                axis=1,
            ),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Data": st.column_config.DatetimeColumn(
                    "Data e hora",
                    format="DD/MM/YYYY HH:mm:ss",
                ),
                "Valor": st.column_config.NumberColumn(
                    variavel,
                    format="%.2f",
                ),
            },
        )

        st.download_button(
            "Exportar histórico CSV",
            data=tabela.to_csv(index=False).encode("utf-8"),
            file_name=(
                f"{modo.replace(' ', '_')}_"
                f"{variavel}_historico.csv"
            ),
            mime="text/csv",
        )

    except requests.RequestException as erro:
        st.error(
            "Não foi possível consultar a plataforma InterSCity. "
            "Verifique a conexão e tente atualizar os dados."
        )

        with st.expander("Detalhes técnicos"):
            st.code(str(erro))

    except (ValueError, TypeError, KeyError) as erro:
        st.error(
            "Não foi possível interpretar os dados recebidos "
            "da API."
        )

        with st.expander("Detalhes técnicos"):
            st.code(str(erro))


painel(
    modo,
    maquina_nome,
    periodo_nome,
    limites_usuario,
)
