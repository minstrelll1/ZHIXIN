[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Destination)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$destinationRoot = [IO.Path]::GetFullPath($Destination)
$projectPrefix = $projectRoot.TrimEnd('\') + '\'
$destinationPrefix = $destinationRoot.TrimEnd('\') + '\'
if ($destinationRoot -eq $projectRoot -or
    $destinationRoot.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $projectRoot.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'U 盘目标不能与当前项目目录重叠。'
}
foreach ($relative in @('tools\bootstrap_ground_impl.ps1', 'tools\install_ground_station.ps1',
        'tools\launch_ground_app.ps1', 'third_party\mediamtx\mediamtx.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relative) -PathType Leaf)) {
        throw "源项目缺少：$relative"
    }
}

New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
$excludedDirectories = @(
    '.git', '.venv', '__pycache__', '.runtime', '.codex_backup*',
    'competition_backend\data', 'ground_runtime', 'ground_logs', 'flight_records',
    'received_images', 'pointcloud_records', 'position_tests', 'onboard_source_backup'
)
$excludedFiles = @('local_tokens.ps1', 'local_tokens.env', 'auto.key', 'auto.crt', '*.pyc', '*.bag', '*.log')
& robocopy.exe $projectRoot $destinationRoot /E /XJ /R:1 /W:1 /NFL /NDL /NP /XD $excludedDirectories /XF $excludedFiles | Out-Null
if ($LASTEXITCODE -ge 8) { throw "U 盘复制失败，robocopy 退出码：$LASTEXITCODE" }

foreach ($relative in @('tools\bootstrap_ground_impl.ps1', 'tools\install_ground_station.ps1',
        'tools\launch_ground_app.ps1', 'third_party\mediamtx\mediamtx.exe', '智信竞赛.exe')) {
    $source = Join-Path $projectRoot $relative
    $target = Join-Path $destinationRoot $relative
    if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or
        (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash) {
        throw "U 盘文件缺失或复制校验失败：$relative"
    }
}
Write-Host "U 盘部署目录已备好：$destinationRoot" -ForegroundColor Green
