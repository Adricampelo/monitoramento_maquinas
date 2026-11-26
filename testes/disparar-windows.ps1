# Dispara o teste de carga AUTORIZADO a partir de um computador Windows.
#
# O cluster continua no Linux; o Windows só gera a carga (k6), o que libera
# CPU no Linux para o gateway.
#
# Pré-requisitos no Windows:
#   1. Instalar o k6:            winget install k6 --source winget
#   2. Ter esta pasta "testes" (git clone do projeto ou copiar a pasta)
#
# Pré-requisitos no Linux (antes de rodar este script):
#   make teste-recursos  e  make teste-autorizado-preparar
#
# Uso (PowerShell, dentro da pasta do projeto):
#   .\testes\disparar-windows.ps1 -IpLinux 192.168.0.10
#
# Se o PowerShell bloquear o script:
#   powershell -ExecutionPolicy Bypass -File .\testes\disparar-windows.ps1 -IpLinux 192.168.0.10

param(
    [Parameter(Mandatory = $true)]
    [string]$IpLinux,
    [string]$Maquinas = "maq-teste-01,maq-teste-02"
)

$ErrorActionPreference = "Stop"
$Url = "http://${IpLinux}:8000"
$Script = Join-Path $PSScriptRoot "carga_gateway.js"
$Resultado = Join-Path $PSScriptRoot "resultado-gateway-autorizado.json"

if (-not (Get-Command k6 -ErrorAction SilentlyContinue)) {
    Write-Host "k6 não encontrado. Instale com: winget install k6 --source winget" -ForegroundColor Red
    Write-Host "Depois feche e abra o PowerShell de novo."
    exit 1
}

# 1. Conferência: o gateway do Linux responde daqui?
Write-Host "== Conferindo o gateway em $Url ..."
try {
    $saude = Invoke-RestMethod -Uri "$Url/healthz" -TimeoutSec 5
    Write-Host "   ok: respondeu o pod $($saude.pod)" -ForegroundColor Green
} catch {
    Write-Host "   Não consegui acessar $Url/healthz" -ForegroundColor Red
    Write-Host "   - O IP do Linux está certo? (no Linux: ip -4 addr | grep inet)"
    Write-Host "   - O cluster está no ar? (no Linux: make ps)"
    Write-Host "   - Firewall do Linux liberando a porta 8000? (sudo ufw allow 8000/tcp)"
    exit 1
}

# 2. Confirmação
Write-Host @"

  Vai enviar ATÉ 1000 req/s ao InterSCity REAL por cerca de 9 minutos
  (~260 mil requisições) nas máquinas de teste: $Maquinas

  Degraus: 50 -> 100 -> 250 -> 500 -> 750 -> 1000 req/s (1 min cada),
           1000 req/s sustentados por 2 min, depois desce.
  Freio:   para sozinho se erro > 2% ou p95 > 2 s (após 30 s).
           Ctrl+C também interrompe a qualquer momento.

"@
$resposta = Read-Host "Digite SIM para disparar"
if ($resposta -ne "SIM") { Write-Host "cancelado"; exit 1 }

# 3. Carga
k6 run `
    -e CENARIO=autorizado `
    -e URL=$Url `
    -e MAQUINAS_TESTE=$Maquinas `
    --summary-export=$Resultado `
    $Script

Write-Host "`nResumo salvo em $Resultado"
