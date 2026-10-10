param(
    [string]$FleetConfig = "",
    [int]$WebPort = 8000,
    [string]$AuthToken = $env:AUTH_TOKEN,
    [string]$PeerToken = $env:PEER_TOKEN,
    [switch]$ConfirmLiveConfig,
    [switch]$CheckOnly
)
$ErrorActionPreference = "Stop"

# 中文控制台与 Python 子进程统一使用 UTF-8。
$utf8Encoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8Encoding
[Console]::InputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
if (-not $FleetConfig) { $FleetConfig = Join-Path $ProjectRoot "config\fleet.json" }
$FleetConfig = (Resolve-Path -LiteralPath $FleetConfig).Path
$PythonExe = Join-Path $ProjectRoot "competition_backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "请先运行 tools\install_ground_station.ps1 安装地面环境。" }
$env:PYTHONPATH = "$ProjectRoot;$ProjectRoot\competition_backend"
& $PythonExe -c "import sys; from competition_shared.fleet import FleetStore; FleetStore(sys.argv[1]); print('机队配置检查通过，终端编号将在网页中选择。')" $FleetConfig
if ($LASTEXITCODE -ne 0) { throw "机队配置检查失败。" }
if ($CheckOnly) { return }
$TokensFile = Join-Path $PSScriptRoot "local_tokens.ps1"
if (Test-Path -LiteralPath $TokensFile) { . $TokensFile }
if (-not $AuthToken) { $AuthToken = $env:AUTH_TOKEN }
if (-not $PeerToken) { $PeerToken = $env:PEER_TOKEN }
if (-not $AuthToken -or -not $PeerToken) { throw "请在 tools\local_tokens.ps1 或环境变量中设置 AUTH_TOKEN 和 PEER_TOKEN。" }

# 每台地面电脑自动检查本机互联规则；取消授权或配置失败不阻断地面服务。
try {
    & (Join-Path $PSScriptRoot 'configure_ground_firewall.ps1') -FleetConfig $FleetConfig -WebPort $WebPort
} catch {
    Write-Warning ("地面互联防火墙检查失败，继续启动地面服务：{0}" -f $_.Exception.Message)
}

function Test-ExistingCompetitionBackend {
    param([int]$Port)
    $urls = @(
        "http://127.0.0.1:$Port/health",
        "http://127.0.0.1:$Port/api/v1/status"
    )
    foreach ($url in $urls) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ([int]$response.StatusCode -ne 200) { continue }
            $body = [string]$response.Content
            if ($body -match '"ok"\s*:\s*true' -and
                ($body -match '"operator"' -or $body -match '"adapter"\s*:\s*"awaiting_selection"')) {
                return $true
            }
        } catch {
            # 端口可能属于其他程序，继续交给后面的占用错误处理。
        }
    }
    return $false
}

if (Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue) {
    if (Test-ExistingCompetitionBackend -Port $WebPort) {
        Write-Host "本项目地面后端已在端口 $WebPort 运行，本次跳过重复启动并复用现有服务。" -ForegroundColor Green
        Write-Host "网页入口：http://127.0.0.1:$WebPort/"
        return
    }
    throw "网页端口 $WebPort 已被其他程序占用，请检查端口后再启动；为避免误杀其他服务，本次未强制关闭它。"
}
$env:COMPETITION_FLEET_CONFIG = $FleetConfig
$env:COMPETITION_TCP_TOKEN = $AuthToken
$env:COMPETITION_PEER_TOKEN = $PeerToken
$env:COMPETITION_POINTCLOUD_INGEST_TOKEN = $AuthToken
$env:COMPETITION_TRAFFIC_REPORT_TOKEN = $AuthToken
$env:COMPETITION_HOST = "0.0.0.0"
$env:COMPETITION_PORT = [string]$WebPort
$env:COMPETITION_CONFIRM_LIVE_CONFIG = if ($ConfirmLiveConfig) { "true" } else { "false" }
Remove-Item Env:COMPETITION_GROUND_TERMINAL_ID -ErrorAction SilentlyContinue
Write-Host "网页入口：http://127.0.0.1:$WebPort/"
Write-Host "进入网页选择地面终端编号和任务发布角色；机型与机地 IP 按固定配置自动绑定。"
Write-Host "退出请点击程序日志中的停止程序，或双击项目目录的 stop_ground.cmd。"
$ProgramLogDirectory = Join-Path $ProjectRoot 'ground_logs\programs'
New-Item -ItemType Directory -Force -Path $ProgramLogDirectory | Out-Null
$startupText = "`n机队配置检查通过，终端编号将在网页中选择。`n网页入口：http://127.0.0.1:$WebPort/`n进入网页选择地面终端编号和任务发布角色；机型与机地 IP 按固定配置自动绑定。`n"
[IO.File]::AppendAllText((Join-Path $ProgramLogDirectory 'ground.log'), $startupText, $utf8Encoding)
Push-Location $ProjectRoot
try { & $PythonExe -m competition_backend.ground_entry }
finally { Pop-Location }
