param(
    [int]$Port = 56010,
    [string]$OutputDirectory = "",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Receiver = Join-Path $ProjectRoot "src\su17_image_transfer\ground\ground_image_receiver.py"

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $ProjectRoot "received_images"
}

python $Receiver --bind 0.0.0.0 --port $Port --output $OutputDirectory --token $Token

