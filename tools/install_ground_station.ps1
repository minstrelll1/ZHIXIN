param(
    [string]$PythonCommand = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$BackendRoot = Join-Path $ProjectRoot "competition_backend"
$VenvPython = Join-Path (Join-Path $BackendRoot ".venv") "Scripts\python.exe"
$MediaMtx = Join-Path $ProjectRoot "third_party\mediamtx\mediamtx.exe"

if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {
    throw "未找到 Python 3，请安装 Python 3 并勾选添加到 PATH。"
}
if (-not (Test-Path -LiteralPath $MediaMtx)) {
    throw "缺少相对路径 third_party\mediamtx\mediamtx.exe。请先获取完整地面端部署包。"
}

Push-Location $BackendRoot
try {
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        & $PythonCommand -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "无法创建 Python 虚拟环境。" }
    }
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "无法更新 pip。" }
    & $VenvPython -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw "无法安装竞赛地面端依赖。" }
}
finally {
    Pop-Location
}

Write-Host "地面端依赖和 MediaMTX 检查完成。" -ForegroundColor Green
Write-Host "项目目录：$ProjectRoot"
