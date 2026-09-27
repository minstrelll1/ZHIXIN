param(
    [int]$Port = 56010,
    [string]$OutputDirectory = "",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"

# 中文控制台与 Python 子进程统一使用 UTF-8。
$utf8Encoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8Encoding
[Console]::InputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Receiver = Join-Path $ProjectRoot "src\su17_image_transfer\ground\ground_image_receiver.py"

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $ProjectRoot "received_images"
}

python $Receiver --bind 0.0.0.0 --port $Port --output $OutputDirectory --token $Token

