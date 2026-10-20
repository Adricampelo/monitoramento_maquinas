# Dashboard de Monitoramento de Máquinas Industriais

Aplicação desenvolvida em Python com Streamlit para visualizar e acompanhar os dados de monitoramento de máquinas industriais registrados na plataforma InterSCity.

## Funcionalidades

- Visualização dos dados das máquinas cadastradas.
- Consulta ao histórico de medições.
- Gráficos para acompanhar a evolução das variáveis monitoradas.
- Comparação entre máquinas.
- Filtro por período de tempo.
- Definição de limites para identificação de valores acima do esperado.
- Exportação do histórico de medições em CSV.

## Tecnologias utilizadas

- Python
- Streamlit
- Pandas
- Requests
- Plotly
- InterSCity

## Requisitos

Antes de executar a aplicação, instale:

- Python 3.10 ou superior.
- Git (opcional, caso escolha clonar o repositório).

## Como obter o projeto

Você pode obter os arquivos de duas maneiras.

### Opção 1 — Clonar o repositório com Git

Abra o terminal ou PowerShell e execute:

```powershell
git clone https://github.com/Adricampelo/monitoramento_maquinas.git
cd monitoramento_maquinas
```

### Opção 2 — Baixar o projeto em ZIP

1. Acesse o [repositório no GitHub](https://github.com/Adricampelo/monitoramento_maquinas).
2. Clique no botão **Code**.
3. Selecione **Download ZIP**.
4. Extraia o arquivo ZIP para uma pasta do computador.
5. Abra o terminal nessa pasta, onde estão os arquivos `interface.py` e `requirements.txt`.

## Como executar no Windows

### 1. Criar um ambiente virtual

Na pasta do projeto, execute:

```powershell
python -m venv .venv
```

### 2. Ativar o ambiente virtual

```powershell
.\.venv\Scripts\Activate.ps1
```

Se a ativação for bloqueada pelas políticas do PowerShell, consulte as configurações do Python no seu computador ou utilize diretamente o executável Python do ambiente virtual.

### 3. Instalar as dependências

Com o ambiente virtual ativado, execute:

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Executar o dashboard

Execute:

```powershell
streamlit run interface.py
```

O Streamlit abrirá a aplicação no navegador. Caso isso não aconteça automaticamente, acesse:

[http://localhost:8501](http://localhost:8501)

Para encerrar a execução, pressione `Ctrl + C` no terminal.

## Fonte dos dados

O dashboard consulta a API da plataforma InterSCity para obter os dados das máquinas monitoradas.

Para que os dados sejam exibidos corretamente, é necessário que a API esteja acessível e que os recursos consultados possuam medições disponíveis.

## Estrutura do projeto

```text
monitoramento_maquinas/
├── interface.py
├── requirements.txt
└── README.md
```

