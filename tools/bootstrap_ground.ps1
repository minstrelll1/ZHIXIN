# ASCII entry point: compatible with ScriptBlock.Create and Windows PowerShell 5.
[CmdletBinding()]
param(
    [string]$Repository = "minstrelll1/ZHIXIN",
    [string]$Branch = "codex/portable-ground-deployment",
    [string]$Destination = (Join-Path (Get-Location) "competition_development"),
    [string]$AuthToken = "",
    [string]$PeerToken = "",
    [switch]$SkipInstall,
    [switch]$ForceTokenConfig
)
$ErrorActionPreference = "Stop"
$implementationPath = if ($PSScriptRoot) { Join-Path $PSScriptRoot "bootstrap_ground_impl.ps1" } else { $null }
if ($implementationPath -and (Test-Path -LiteralPath $implementationPath)) {
    $implementationText = [IO.File]::ReadAllText($implementationPath, [Text.Encoding]::UTF8)
} else {
    $encodedRepo = $Repository -replace "^https?://github.com/", "" -replace "/$", ""
    if ($Branch -match '\.\.' -or $Branch.Contains('\') -or $Branch.Contains('"')) {
        throw ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('5YiG5pSv5ZCN5YyF5ZCr5LiN5a6J5YWo5a2X56ym44CC')))
    }
    $response = Invoke-WebRequest -UseBasicParsing -Uri "https://raw.githubusercontent.com/$encodedRepo/$Branch/tools/bootstrap_ground_impl.ps1"
    $buffer = [IO.MemoryStream]::new()
    try {
        $response.RawContentStream.Position = 0
        $response.RawContentStream.CopyTo($buffer)
        $implementationText = [Text.Encoding]::UTF8.GetString($buffer.ToArray())
    } finally {
        $buffer.Dispose()
        $response.RawContentStream.Dispose()
    }
}
# A downloaded BOM is data in a string; remove it before compiling.
$implementationBlock = [scriptblock]::Create($implementationText.TrimStart([char]0xFEFF))
& $implementationBlock @PSBoundParameters
