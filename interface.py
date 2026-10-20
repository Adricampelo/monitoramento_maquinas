
import requests
import pandas as pd
import streamlit as st
import plotly.express as px

# ============================================================
# CONFIGURAÇÃO
# ============================================================

MAQUINAS = {
    "Máquina 001": "bcb2ade2-946f-4df2-a501-56e41c259cb7",
    "Máquina 002": "41bdd388-b2c1-4fc4-99eb-8668c4ef5c07",
}

BASE_URL = (
    "https://cidadesinteligentes.lsdi.ufma.br/"
    "interscity_lh/collector/resources/{uuid}/data/"
)

INTERVALO = 30
LIMITE_ONLINE_MIN = 10
FUSO = "America/Fortaleza"

LIMITES = {
    "temp": {
        "rotulo": "Temperatura",
        "unidade": "°C",
        "padrao": 80.0,
    },
    "vibra": {
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

COR = "#0F6E7A"
COR_ALERTA = "#C98A1B"
COR_CRITICA = "#C0392B"
COR_OK = "#3A8F5C"


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

    div[data-testid="stMetric"] {{
        background: #FFFFFF;
        border: 1px solid #E3E6E3;
        border-left: 4px solid {COR};
        border-radius: 8px;
        padding: 14px 18px;
    }}

    div[data-testid="stMetricLabel"] p {{
        color: #5B6A6E;
    }}

    section[data-testid="stSidebar"] {{
        background: #F1F3F1;
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# CONSULTA À API
# ============================================================

@st.cache_data(ttl=INTERVALO, show_spinner=False)
def consultar_dados(uuid):
    url = BASE_URL.format(uuid=uuid)

    resposta = requests.get(url, timeout=20)
    resposta.raise_for_status()

    return resposta.json()


def montar_historico(recurso):
    tabelas = []

    capacidades = recurso.get("capabilities", {})

    for capacidade, registros in capacidades.items():
        if not isinstance(registros, list) or not registros:
            continue

        df = pd.DataFrame(registros)

        coluna_valor = (
            "value" if "value" in df.columns
            else capacidade if capacidade in df.columns
            else None
        )

        if coluna_valor is None or "date" not in df.columns:
            continue

        df = df[["date", coluna_valor]].copy()
        df.columns = ["Data", "Valor"]

        df["Data"] = pd.to_datetime(
            df["Data"],
            errors="coerce",
            utc=True,
        )

        df["Valor"] = pd.to_numeric(
            df["Valor"],
            errors="coerce",
        )

        df["Variável"] = capacidade

        tabelas.append(df)

    if not tabelas:
        return pd.DataFrame(
            columns=["Data", "Valor", "Variável"]
        )

    historico = pd.concat(tabelas, ignore_index=True)

    historico = historico.dropna(
        subset=["Data", "Valor"]
    )

    return historico.sort_values("Data").reset_index(drop=True)


def buscar_historico(maquina, uuid):
    dados = consultar_dados(uuid)

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
                    <div style="
                        padding: 10px 12px;
                        border-radius: 8px;
                        background: #F4F6F4;
                        border-left: 4px solid {item['Cor']};
                    ">
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
            template="simple_white",
            height=430,
            hovermode="x unified",
            margin=dict(l=10, r=10, t=25, b=10),
        )

        fig.update_yaxes(
            title=cfg["unidade"] if cfg else variavel,
            gridcolor="#E9ECE9",
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
                    "color: #C0392B; font-weight: bold;"
                )

                if "Situação" in linha.index:
                    estilos[linha.index.get_loc("Situação")] = (
                        "color: #C0392B; font-weight: bold;"
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
