$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$scriptPath = Join-Path $projectRoot 'tools\bootstrap_ground_impl.ps1'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw '首次部署脚本语法错误。' }
$function = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
                              $node.Name -eq 'Copy-GroundProjectFiles' }, $true)
if (-not $function) { throw '缺少逐文件部署函数。' }
. ([scriptblock]::Create($function.Extent.Text))

$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('zhixin-bootstrap-copy-' + [Guid]::NewGuid().ToString('N'))
$source = Join-Path $fixtureRoot '中文目录\extract\ZHIXIN-codex-portable-ground-deployment'
$target = Join-Path $fixtureRoot '中文目录\competition_development'
$script:copyAttempts = 0
$script:denyCopies = $false
try {
    foreach ($folder in @('tools', 'config')) {
        New-Item -ItemType Directory -Force -Path (Join-Path $source $folder), (Join-Path $target $folder) | Out-Null
    }
    [IO.File]::WriteAllText((Join-Path $source 'tools\launch_ground_app.ps1'), 'new launcher')
    [IO.File]::WriteAllText((Join-Path $source 'tools\bootstrap_ground.ps1'), 'new bootstrap')
    [IO.File]::WriteAllText((Join-Path $source 'tools\local_tokens.ps1'), 'remote secret')
    [IO.File]::WriteAllText((Join-Path $source 'tools\local_tokens.env'), 'remote secret')
    [IO.File]::WriteAllText((Join-Path $source 'config\fleet.json'), 'remote fleet')
    [IO.File]::WriteAllText((Join-Path $source 'config\onboard_programs.json'), 'remote commands')
    [IO.File]::WriteAllText((Join-Path $target 'tools\local_tokens.ps1'), 'local secret')
    [IO.File]::WriteAllText((Join-Path $target 'tools\local_tokens.env'), 'local secret')
    [IO.File]::WriteAllText((Join-Path $target 'config\fleet.json'), 'local fleet')
    [IO.File]::WriteAllText((Join-Path $target 'config\onboard_programs.json'), 'local commands')

    $archive = Join-Path $fixtureRoot 'source.zip'
    $extract = Join-Path $fixtureRoot 'archive_extract'
    Compress-Archive -LiteralPath $source -DestinationPath $archive
    Expand-Archive -LiteralPath $archive -DestinationPath $extract
    $source = Join-Path $extract (Split-Path $source -Leaf)

    function Copy-Item {
        param([string]$LiteralPath, [string]$Destination, [switch]$Force, [string]$ErrorAction)
        if ($LiteralPath.EndsWith('launch_ground_app.ps1')) {
            $script:copyAttempts++
            if ($script:denyCopies -or $script:copyAttempts -eq 1) {
                throw [UnauthorizedAccessException]::new('模拟文件暂时被占用')
            }
        }
        Microsoft.PowerShell.Management\Copy-Item -LiteralPath $LiteralPath -Destination $Destination -Force -ErrorAction Stop
    }

    Copy-GroundProjectFiles -SourceRoot $source -TargetRoot $target
    if ($script:copyAttempts -ne 2 -or [IO.File]::ReadAllText((Join-Path $target 'tools\launch_ground_app.ps1')) -ne 'new launcher') {
        throw '未能从临时拒绝访问中恢复，或启动脚本未部署到正确位置。'
    }
    if (Test-Path -LiteralPath (Join-Path $target 'tools\tools')) { throw '意外生成了嵌套 tools 目录。' }
    if ([IO.File]::ReadAllText((Join-Path $target 'tools\local_tokens.ps1')) -ne 'local secret' -or
        [IO.File]::ReadAllText((Join-Path $target 'tools\local_tokens.env')) -ne 'local secret' -or
        [IO.File]::ReadAllText((Join-Path $target 'config\fleet.json')) -ne 'local fleet' -or
        [IO.File]::ReadAllText((Join-Path $target 'config\onboard_programs.json')) -ne 'local commands') {
        throw '部署时覆盖了本机配置。'
    }

    $script:denyCopies = $true
    try {
        Copy-GroundProjectFiles -SourceRoot $source -TargetRoot $target
        throw '持续拒绝访问未向用户报告。'
    } catch {
        $message = $_.Exception.Message
        if (-not ($message.Contains('launch_ground_app.ps1') -and
                   $message.Contains('来源：') -and $message.Contains('目标：') -and
                   $message.Contains('原始错误：'))) {
            throw "持续拒绝访问未报告源、目标和原因：$message"
        }
    }
    Write-Host '首次部署逐文件复制、临时占用重试、本机配置保留与拒绝访问诊断检查通过。'
}
finally {
    Remove-Item Function:\Copy-Item -ErrorAction SilentlyContinue
    $resolved = [IO.Path]::GetFullPath($fixtureRoot)
    $prefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw '测试清理路径不安全。' }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
