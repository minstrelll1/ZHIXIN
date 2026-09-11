param(
    [string]$PythonCommand = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$BackendRoot = Join-Path $ProjectRoot "competition_backend"
$VenvPython = Join-Path (Join-Path $BackendRoot ".venv") "Scripts\python.exe"

if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {
    throw "Python 3 was not found. Install Python 3 and enable Add Python to PATH."
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

Write-Host "Ground station dependencies installed." -ForegroundColor Green
Write-Host "Project root: $ProjectRoot"
