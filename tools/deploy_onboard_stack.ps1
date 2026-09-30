param(
    [Parameter(Mandatory = $true)][string]$UavAddress,
    [string]$UavUser = "amov",
    [ValidateSet("p600", "su17")][string]$Model = "p600",
    [string]$RemoteWorkspace = "/home/amov/competition_development",
    [switch]$SyncConfig
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
if ($UavAddress -notmatch '^[A-Za-z0-9.-]+$' -or $UavAddress.StartsWith('-') -or $UavUser -notmatch '^[a-z_][a-z0-9_-]*$') { throw "SSH 地址或用户名无效。" }
if ($RemoteWorkspace -notmatch '^/[A-Za-z0-9_/-]+/competition_development$' -or $RemoteWorkspace.Contains('..') -or $RemoteWorkspace -match '(su17|p600)_experiment') { throw "远端路径必须为独立的 competition_development 目录，不能指向厂商源码。" }
$Remote = "${UavUser}@${UavAddress}"
$competitionKey = Join-Path $env:USERPROFILE '.ssh\zhixin_competition_ed25519'
$sshIdentityArgs = @()
if (Test-Path -LiteralPath $competitionKey -PathType Leaf) {
    $sshIdentityArgs = @('-i', $competitionKey, '-o', 'IdentitiesOnly=yes')
}
& ssh @sshIdentityArgs $Remote "mkdir -p '$RemoteWorkspace/src' '$RemoteWorkspace/tools' '$RemoteWorkspace/config'"
if ($LASTEXITCODE -ne 0) { throw "无法创建竞赛目录。" }
foreach ($Package in @("su17_competition_executor", "su17_image_transfer", "su17_pointcloud_bridge")) {
    & scp @sshIdentityArgs -r (Join-Path $ProjectRoot "src/$Package") "${Remote}:$RemoteWorkspace/src/"
    if ($LASTEXITCODE -ne 0) { throw "上传 $Package 失败。" }
}
& scp @sshIdentityArgs -r (Join-Path $ProjectRoot "competition_shared") "${Remote}:$RemoteWorkspace/"
if ($LASTEXITCODE -ne 0) { throw "上传统一配置模块失败。" }
foreach ($File in @("start_onboard_stack.sh", "build_onboard.sh", "onboard_preflight.py", "forward_groundstation_pointcloud.py", "local_tokens.env")) {
    $Source = Join-Path $PSScriptRoot $File
    if (Test-Path -LiteralPath $Source) {
        & scp @sshIdentityArgs $Source "${Remote}:$RemoteWorkspace/tools/"
        if ($LASTEXITCODE -ne 0) { throw "上传 $File 失败。" }
    }
}
& ssh @sshIdentityArgs $Remote "test -f '$RemoteWorkspace/config/fleet.json'"
$ConfigExists = $LASTEXITCODE -eq 0
if ($SyncConfig -or -not $ConfigExists) {
    & scp @sshIdentityArgs (Join-Path $ProjectRoot "config/fleet.json") "${Remote}:$RemoteWorkspace/config/fleet.json"
    if ($LASTEXITCODE -ne 0) { throw "上传机队配置失败。" }
}
& ssh @sshIdentityArgs $Remote "cd '$RemoteWorkspace' && bash ./tools/build_onboard.sh --model $Model"
if ($LASTEXITCODE -ne 0) { throw "竞赛包编译失败，请保留终端输出。" }
Write-Host "竞赛程序已部署（$Model），厂商目录未改动。固定机队启动可执行 --direct --check；需要严格核验时再执行不带 --direct 的 --check。" -ForegroundColor Green
