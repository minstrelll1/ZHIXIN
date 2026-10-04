$ErrorActionPreference = 'Stop'
$stageRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('zhixin-usb-test-' + [Guid]::NewGuid().ToString('N'))
$source = Join-Path $fixtureRoot 'source\competition_development'
$usb = Join-Path $fixtureRoot 'usb\competition_development'
$target = Join-Path $fixtureRoot 'target\competition_development'

function Set-FixtureFile([string]$Path, [string]$Content) {
    New-Item -ItemType Directory -Path (Split-Path $Path -Parent) -Force | Out-Null
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($true))
}

try {
    foreach ($relative in @('tools\bootstrap_ground_impl.ps1', 'tools\prepare_ground_usb.ps1')) {
        $path = Join-Path $source $relative
        New-Item -ItemType Directory -Path (Split-Path $path -Parent) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $stageRoot $relative) -Destination $path
    }
    Set-FixtureFile (Join-Path $source 'tools\install_ground_station.ps1') 'fixture install'
    Set-FixtureFile (Join-Path $source 'tools\launch_ground_app.ps1') 'fixture launcher'
    Set-FixtureFile (Join-Path $source 'third_party\mediamtx\mediamtx.exe') 'fixture mediamtx'
    Set-FixtureFile (Join-Path $source '智信竞赛.exe') 'fixture app'
    Set-FixtureFile (Join-Path $source 'src\task.py') 'fixture code'
    Set-FixtureFile (Join-Path $source 'config\fleet.json') 'usb fleet'
    Set-FixtureFile (Join-Path $source 'config\onboard_programs.json') 'usb programs'
    Set-FixtureFile (Join-Path $source 'tools\local_tokens.ps1') 'source secret'
    Set-FixtureFile (Join-Path $source 'ground_logs\previous.log') 'source log'
    Set-FixtureFile (Join-Path $source 'received_images\previous.jpg') 'source image'
    Set-FixtureFile (Join-Path $source 'competition_backend\.venv\Scripts\python.exe') 'source venv'

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $source 'tools\prepare_ground_usb.ps1') -Destination $usb
    if ($LASTEXITCODE -ne 0) { throw 'U 盘准备失败。' }
    foreach ($relative in @('tools\bootstrap_ground_impl.ps1', 'tools\launch_ground_app.ps1', '智信竞赛.exe', 'third_party\mediamtx\mediamtx.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $usb $relative) -PathType Leaf)) { throw "U 盘缺少 $relative" }
    }
    foreach ($relative in @('tools\local_tokens.ps1', 'ground_logs\previous.log', 'received_images\previous.jpg', 'competition_backend\.venv\Scripts\python.exe')) {
        if (Test-Path -LiteralPath (Join-Path $usb $relative)) { throw "U 盘包含本机文件：$relative" }
    }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $usb 'tools\bootstrap_ground_impl.ps1') -FromUsb -SkipInstall -Destination $target -AuthToken 'fixture-auth' -PeerToken 'fixture-peer'
    if ($LASTEXITCODE -ne 0) { throw 'U 盘首次部署失败。' }
    if (([IO.File]::ReadAllText((Join-Path $target 'src\task.py'))) -ne 'fixture code' -or
        -not (Test-Path -LiteralPath (Join-Path $target 'tools\local_tokens.ps1')) -or
        -not (Test-Path -LiteralPath (Join-Path $target '智信竞赛.exe'))) { throw 'U 盘部署缺少代码、令牌或应用。' }
    Set-FixtureFile (Join-Path $target 'config\fleet.json') 'local fleet'
    Set-FixtureFile (Join-Path $target 'ground_logs\local.log') 'local log'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $usb 'tools\bootstrap_ground_impl.ps1') -FromUsb -SkipInstall -Destination $target
    if ($LASTEXITCODE -ne 0 -or
        ([IO.File]::ReadAllText((Join-Path $target 'config\fleet.json'))) -ne 'local fleet' -or
        -not (Test-Path -LiteralPath (Join-Path $target 'ground_logs\local.log'))) { throw '重复部署覆盖了本机配置或数据。' }
    Write-Host 'U 盘准备、首次部署与本机配置保留检查通过。'
}
finally {
    $resolved = [IO.Path]::GetFullPath($fixtureRoot)
    $prefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw '测试清理路径异常。' }
    if (Test-Path -LiteralPath $resolved) { Remove-Item -LiteralPath $resolved -Recurse -Force }
}
