param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Split-Path $PSScriptRoot -Parent)
if (-not $OutputPath) {
    $OutputPath = Join-Path (Split-Path $ProjectRoot -Parent) "competition_development_ground_uav1.zip"
}
$OutputPath = [System.IO.Path]::GetFullPath($OutputPath)
$TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$StageRoot = Join-Path $TempRoot ("su17_ground_package_" + [guid]::NewGuid().ToString("N"))
$StageProject = Join-Path $StageRoot "competition_development"

try {
    New-Item -ItemType Directory -Path $StageProject -Force | Out-Null
    $Items = @(
        ".vscode",
        "competition_backend",
        "competition_shared",
        "config",
        "docs",
        "src",
        "tools",
        "third_party",
        ".gitignore",
        "competition_development.code-workspace",
        "DEPLOY_UAV1_GROUND.txt",
        "QUICK_START_TWO_GROUNDS.txt",
        "README.md",
        "部署到192.168.1.121.txt",
        "启动指令.txt",
        "SU17地面端与机载端启动指令.txt"
    )
    foreach ($Item in $Items) {
        $Source = Join-Path $ProjectRoot $Item
        if (Test-Path -LiteralPath $Source) {
            Copy-Item -LiteralPath $Source -Destination $StageProject -Recurse -Force
        }
    }

    $Excluded = @(
        (Join-Path $StageProject "tools\local_tokens.ps1"),
        (Join-Path $StageProject "tools\local_tokens.env"),
        (Join-Path $StageProject "competition_backend\.venv"),
        (Join-Path $StageProject "competition_backend\data"),
        (Join-Path $StageProject "tools\.codex_backup_20260827_command_actual_compare"),
        (Join-Path $StageProject "ground_runtime"),
        (Join-Path $StageProject "ground_logs"),
        (Join-Path $StageProject "flight_records"),
        (Join-Path $StageProject "received_images"),
        (Join-Path $StageProject "position_tests")
    )
    foreach ($Target in $Excluded) {
        $ResolvedTarget = [System.IO.Path]::GetFullPath($Target)
        if (-not $ResolvedTarget.StartsWith($StageRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsafe package cleanup path: $ResolvedTarget"
        }
        if (Test-Path -LiteralPath $ResolvedTarget) {
            Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
        }
    }
    $GeneratedDirectories = Get-ChildItem -LiteralPath $StageProject -Directory -Recurse -Force | Where-Object {
        $_.Name -like ".codex_backup*" -or
        $_.Name -like "*.egg-info" -or
        $_.Name -eq "__pycache__"
    } | Sort-Object { $_.FullName.Length } -Descending
    foreach ($Directory in $GeneratedDirectories) {
        $ResolvedTarget = [System.IO.Path]::GetFullPath($Directory.FullName)
        if (-not $ResolvedTarget.StartsWith($StageRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Unsafe generated-directory cleanup path: $ResolvedTarget"
        }
        if (Test-Path -LiteralPath $ResolvedTarget) {
            Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
        }
    }

    if (Test-Path -LiteralPath $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }
    Compress-Archive -LiteralPath $StageProject -DestinationPath $OutputPath -CompressionLevel Optimal
    Write-Host "Package created: $OutputPath" -ForegroundColor Green
}
finally {
    $ResolvedStage = [System.IO.Path]::GetFullPath($StageRoot)
    if ($ResolvedStage.StartsWith($TempRoot, [System.StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $ResolvedStage)) {
        Remove-Item -LiteralPath $ResolvedStage -Recurse -Force
    }
}
