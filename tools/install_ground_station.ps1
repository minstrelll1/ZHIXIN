param(
    [string]$PythonCommand = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$BackendRoot = Join-Path $ProjectRoot "competition_backend"
$VenvPython = Join-Path (Join-Path $BackendRoot ".venv") "Scripts\python.exe"
$MediaMtx = Join-Path $ProjectRoot "third_party\mediamtx\mediamtx.exe"

if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {
    throw "Python 3 was not found. Install Python 3 and enable Add Python to PATH."
}
if (-not (Test-Path -LiteralPath $MediaMtx)) {
    throw "缺少相对路径 third_party\mediamtx\mediamtx.exe。请先获取完整地面端部署包。"
}

Push-Location $BackendRoot
try {
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        & $PythonCommand -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Unable to create the Python virtual environment." }
    }
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Unable to update pip." }
    & $VenvPython -m pip install -e .
    if ($LASTEXITCODE -ne 0) { throw "Unable to install the competition backend." }
}
finally {
    Pop-Location
}

Write-Host "地面端依赖和 MediaMTX 检查完成。" -ForegroundColor Green
Write-Host "Project root: $ProjectRoot"
