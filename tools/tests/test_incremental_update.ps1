$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$text = [IO.File]::ReadAllText((Join-Path $root 'tools/update_ground.ps1'), [Text.Encoding]::UTF8).TrimStart([char]0xFEFF)
$update = [scriptblock]::Create($text)
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ('zhixin-update-test-' + [guid]::NewGuid().ToString('N'))
$destination = Join-Path $fixtureRoot 'deployment'
$utf8 = [Text.UTF8Encoding]::new($false)
$script:remote = [ordered]@{}
$script:requests = [Collections.Generic.List[string]]::new()
$script:downloads = [Collections.Generic.List[string]]::new()
$script:version = 'a' * 40
$script:corrupt = ''
$script:copyFailure = ''

function Set-Local([string]$Path, [string]$Content) {
    $file = Join-Path $destination $Path
    New-Item -ItemType Directory -Path (Split-Path $file -Parent) -Force | Out-Null
    [IO.File]::WriteAllText($file, $Content, $utf8)
}
function Set-Remote([string]$Path, [string]$Content) { $script:remote[$Path] = $utf8.GetBytes($Content) }
function Blob-Sha([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA1]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([byte[]]([Text.Encoding]::ASCII.GetBytes('blob ' + $Bytes.Length + [char]0) + $Bytes)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Json-Response($Value) {
    return [pscustomobject]@{RawContentStream=[IO.MemoryStream]::new($utf8.GetBytes(($Value | ConvertTo-Json -Depth 10)))}
}
function Invoke-WebRequest {
    param([string]$Uri, [switch]$UseBasicParsing, [int]$TimeoutSec, [hashtable]$Headers, [string]$OutFile)
    $script:requests.Add($Uri)
    if ($Uri -match '/commits/') { return Json-Response @{sha=$script:version; commit=@{tree=@{sha=('b'*40)}}} }
    if ($Uri -match '/git/trees/') {
        $entries = @($script:remote.GetEnumerator() | ForEach-Object { @{path=$_.Key; type='blob'; mode='100644'; sha=(Blob-Sha $_.Value); size=$_.Value.Length} })
        return Json-Response @{tree=$entries; truncated=$false}
    }
    $prefix = "https://raw.githubusercontent.com/fixture/repo/$script:version/"
    if (-not $Uri.StartsWith($prefix) -or -not $OutFile) { throw "未按固定版本下载：$Uri" }
    $path = [Uri]::UnescapeDataString($Uri.Substring($prefix.Length))
    if (-not $script:remote.Contains($path)) { throw "非预期下载：$path" }
    $script:downloads.Add($path)
    $bytes = if ($path -eq $script:corrupt) { $utf8.GetBytes('corrupt') } else { $script:remote[$path] }
    [IO.File]::WriteAllBytes($OutFile, $bytes)
}
function Copy-Item {
    param([string]$LiteralPath, [string]$Destination, [switch]$Force)
    if ($script:copyFailure -and $Destination -eq $script:copyFailure -and $LiteralPath -match '[\\/]staged[\\/]') { $script:copyFailure=''; throw '模拟文件被占用' }
    Microsoft.PowerShell.Management\Copy-Item @PSBoundParameters
}
function Assert-True($Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Read-Local([string]$Path) { return [IO.File]::ReadAllText((Join-Path $destination $Path), $utf8) }
function Run-Update { & $update -Repository 'fixture/repo' -Branch 'test/incremental' -Destination $destination }
function Expect-Failure {
    $failed = $false
    try { Run-Update } catch { $failed = $true }
    Assert-True $failed '预期更新失败但实际未失败'
}

try {
    Set-Local 'tools/install_ground_station.ps1' 'Write-Host "fixture"'
    Set-Remote 'tools/install_ground_station.ps1' 'Write-Host "fixture"'
    Set-Local 'a.txt' 'old'; Set-Remote 'a.txt' 'new'
    Set-Local 'z.txt' 'old-z'; Set-Remote 'z.txt' 'old-z'
    Set-Local 'same.py' "line1`r`nline2`r`n"; Set-Remote 'same.py' "line1`nline2`n"
    Set-Local '.editorconfig' "root=true`r`n"; Set-Remote '.editorconfig' "root=true`n"
    Set-Local 'map.geojson' "{}`r`n"; Set-Remote 'map.geojson' "{}`n"
    Set-Local 'config/fleet.json' '{"local":true}'; Set-Remote 'config/fleet.json' '{"local":false}'
    Set-Local 'config/onboard_programs.json' '{"commands":{"flight":"local-command"}}'; Set-Remote 'config/onboard_programs.json' '{"commands":{}}'
    Set-Local 'third_party/mediamtx/mediamtx.exe' 'existing-binary'; Set-Remote 'third_party/mediamtx/mediamtx.exe' 'other-binary'
    Set-Local 'tools/local_tokens.ps1' 'private-token'; Set-Remote 'tools/local_tokens.ps1' 'must-not-copy'
    Set-Local 'received_images/example.txt' 'private-image'; Set-Remote 'received_images/example.txt' 'must-not-copy'
    Set-Local 'private.txt' 'untracked-data'
    Run-Update
    Assert-True ($script:downloads.Count -eq 1 -and $script:downloads[0] -eq 'a.txt') '首次增量更新重复下载了未变化文件或本机配置'
    Assert-True ((Read-Local 'a.txt') -eq 'new') '变化文件未更新'
    Assert-True ((Read-Local 'config/fleet.json') -eq '{"local":true}') '本机机队配置被覆盖'
    Assert-True ((Read-Local 'config/onboard_programs.json') -eq '{"commands":{"flight":"local-command"}}') '本机启动指令被覆盖'
    Assert-True ((Read-Local 'tools/local_tokens.ps1') -eq 'private-token') '令牌被覆盖'
    Assert-True ((Read-Local 'received_images/example.txt') -eq 'private-image') '图片被覆盖'
    Assert-True ((Read-Local 'third_party/mediamtx/mediamtx.exe') -eq 'existing-binary') '重复更新了现有第三方程序'
    $script:downloads.Clear(); $script:requests.Clear()
    Run-Update
    Assert-True ($script:downloads.Count -eq 0 -and $script:requests.Count -eq 2) '无变化时仍下载源码或整包'

    # 校验失败：已经下载成功的另一个文件也不能提前替换。
    Set-Remote 'a.txt' 'next'; Set-Remote 'z.txt' 'next-z'
    $script:corrupt='z.txt'
    Expect-Failure
    Assert-True ((Read-Local 'a.txt') -eq 'new' -and (Read-Local 'z.txt') -eq 'old-z') '下载校验失败破坏了原代码'
    $script:corrupt=''
    $script:copyFailure=Join-Path $destination 'z.txt'
    Expect-Failure
    Assert-True ((Read-Local 'a.txt') -eq 'new' -and (Read-Local 'z.txt') -eq 'old-z') '替换失败没有恢复已修改文件'
    Run-Update
    Assert-True ((Read-Local 'a.txt') -eq 'next' -and (Read-Local 'z.txt') -eq 'next-z') '失败后重试不能完成更新'

    # 上游删除：删除已管理且未改动的旧文件，保留本机修改和未跟踪文件。
    Set-Local 'a.txt' 'operator-change'
    $script:remote.Remove('a.txt'); $script:remote.Remove('z.txt')
    Run-Update
    Assert-True ((Read-Local 'a.txt') -eq 'operator-change') '误删了本机修改的旧文件'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $destination 'z.txt'))) '没有删除已停用的受管文件'
    Assert-True ((Read-Local 'private.txt') -eq 'untracked-data') '误删了未跟踪数据'

    Set-Remote '../escape.txt' 'outside'
    Expect-Failure
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixtureRoot 'escape.txt'))) '路径越界写入'
    $script:remote.Remove('../escape.txt')
    Set-Remote 'tools/broken.ps1' 'if ('
    Expect-Failure
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $destination 'tools/broken.ps1'))) '解析错误脚本被安装'
    $script:remote.Remove('tools/broken.ps1')

    $heldLock = [IO.File]::Open((Join-Path $destination '.runtime/ground_update.lock'), [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try { Expect-Failure } finally { $heldLock.Dispose() }
    $logs = @(Get-ChildItem -LiteralPath (Join-Path $destination 'ground_logs') -Filter 'update_*.log')
    $allLogs = ($logs | ForEach-Object { [IO.File]::ReadAllText($_.FullName, $utf8) }) -join "`n"
    Assert-True ($allLogs.Contains('文件校验失败') -and $allLogs.Contains('模拟文件被占用') -and -not $allLogs.Contains('private-token')) '错误未保留或日志泄漏了令牌'
    # 原有 README 本地命令必须转入增量脚本，不能下载 ZIP。
    Set-Local 'tools/update_ground.ps1' 'param($Repository,$Branch,$Destination); "INCREMENTAL_ROUTE"'
    $requestCount = $script:requests.Count
    $routed = & (Join-Path $root 'tools/bootstrap_ground.ps1') -Repository 'fixture/repo' -Branch 'test/incremental' -Destination $destination -SkipInstall
    Assert-True ($routed -eq 'INCREMENTAL_ROUTE' -and $script:requests.Count -eq $requestCount) '原更新命令没有转入增量更新'
    Write-Host '增量下载、零变化、换行兼容、配置与软件保留、校验失败、替换恢复、文件删除、路径与并发保护及旧指令兼容检查通过。'
} finally {
    Remove-Item Function:\Invoke-WebRequest,Function:\Copy-Item -ErrorAction SilentlyContinue
    $absolute = [IO.Path]::GetFullPath($fixtureRoot)
    $prefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $absolute.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw '测试清理路径无效' }
    if (Test-Path -LiteralPath $absolute) { Remove-Item -LiteralPath $absolute -Recurse -Force }
}
