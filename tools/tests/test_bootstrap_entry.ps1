$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$source = Join-Path $ProjectRoot "tools\bootstrap_ground.ps1"
$bytes = [IO.File]::ReadAllBytes($source)
if (@($bytes | Where-Object { $_ -gt 127 }).Count) { throw "下载入口必须为无 BOM 的 ASCII，兼容原部署命令。" }
$entryText = [Text.Encoding]::UTF8.GetString($bytes)
[void][scriptblock]::Create($entryText)
$tokens = $null
$errors = $null
[void][Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "本地文件入口解析失败。" }
$fixture = @'
[CmdletBinding()]
param([string]$Repository="minstrelll1/ZHIXIN", [string]$Branch="codex/portable-ground-deployment",
      [string]$Destination, [string]$AuthToken="", [string]$PeerToken="",
      [switch]$SkipInstall, [switch]$ForceTokenConfig)
[pscustomobject]@{ Destination=$Destination; Repository=$Repository; Branch=$Branch;
                  SkipInstall=[bool]$SkipInstall; ForceTokenConfig=[bool]$ForceTokenConfig;
                  AuthToken=$AuthToken; PeerToken=$PeerToken }
'@
$script:downloadUrls = @()
$script:entryResponse = $entryText
$script:implementationResponse = [char]0xFEFF + $fixture
function Invoke-RestMethod {
    param([string]$Uri)
    $script:downloadUrls += $Uri
    if ($Uri.EndsWith('/bootstrap_ground.ps1')) { return $script:entryResponse }
    throw "入口访问了非预期地址。"
}
function Invoke-WebRequest {
    param([string]$Uri, [switch]$UseBasicParsing)
    $script:downloadUrls += $Uri
    if (-not $UseBasicParsing -or -not $Uri.EndsWith('/bootstrap_ground_impl.ps1')) { throw "实现访问了非预期地址。" }
    return [pscustomobject]@{ RawContentStream=[IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes($script:implementationResponse)) }
}
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ("zhixin-entry-test-" + [Guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $temporaryRoot -Force | Out-Null
    $destination = Join-Path $temporaryRoot "competition_development"
    # 原 README 指令，只替换成隔离的临时目标；模拟返回带 BOM 的实现。
    $remote = & ([scriptblock]::Create((Invoke-RestMethod 'https://raw.githubusercontent.com/minstrelll1/ZHIXIN/codex/portable-ground-deployment/tools/bootstrap_ground.ps1'))) -Destination $destination
    if ($remote.Destination -ne $destination -or $script:downloadUrls.Count -ne 2) { throw "首次下载入口或目标路径异常。" }
    $custom = & ([scriptblock]::Create($entryText)) -Repository "fixture/repo" -Branch "feature/demo" -Destination $destination -SkipInstall
    if ($custom.Repository -ne 'fixture/repo' -or $custom.Branch -ne 'feature/demo' -or -not $custom.SkipInstall -or $script:downloadUrls[-1] -ne 'https://raw.githubusercontent.com/fixture/repo/feature/demo/tools/bootstrap_ground_impl.ps1') { throw "自定义仓库、分支和开关未完整传递。" }
    $mockEntry = Join-Path $temporaryRoot "bootstrap_ground.ps1"
    $mockImplementation = Join-Path $temporaryRoot "bootstrap_ground_impl.ps1"
    [IO.File]::WriteAllText($mockEntry, $entryText, [Text.Encoding]::ASCII)
    [IO.File]::WriteAllText($mockImplementation, $fixture, [Text.UTF8Encoding]::new($true))
    $local = & $mockEntry -Destination $destination -SkipInstall -ForceTokenConfig -AuthToken "fixture-auth" -PeerToken "fixture-peer"
    if ($local.Destination -ne $destination -or -not $local.SkipInstall -or -not $local.ForceTokenConfig -or $local.AuthToken -ne 'fixture-auth' -or $local.PeerToken -ne 'fixture-peer') { throw "本地更新入口未完整传递参数。" }
    if ($script:downloadUrls.Count -ne 3) { throw "本地更新不应额外下载实现。" }
    Write-Host "原首次部署指令、下载 BOM 兼容、本地更新及参数传递检查通过。"
}
finally {
    Remove-Item Function:\Invoke-RestMethod -ErrorAction SilentlyContinue
    Remove-Item Function:\Invoke-WebRequest -ErrorAction SilentlyContinue
    $target = [IO.Path]::GetFullPath($temporaryRoot)
    $prefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "测试清理路径不安全。" }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
