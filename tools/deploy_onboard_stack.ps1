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
Write-Host "正在检查并配置机载 SSH 公钥授权。"
& (Join-Path $PSScriptRoot 'setup_onboard_ssh.ps1') -UavAddress $UavAddress -UavUser $UavUser
$competitionKey = Join-Path $env:USERPROFILE '.ssh\zhixin_competition_ed25519'
$sshIdentityArgs = @()
if (Test-Path -LiteralPath $competitionKey -PathType Leaf) {
    $sshIdentityArgs = @('-i', $competitionKey, '-o', 'IdentitiesOnly=yes')
}
$sshConnectionArgs = @('-o', 'ConnectTimeout=15', '-o', 'ConnectionAttempts=1', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3')
Write-Host "正在连接机载端 $Remote；首次连接如提示确认主机指纹、密码或密钥口令，请在此窗口输入。"
& ssh @sshConnectionArgs @sshIdentityArgs $Remote "mkdir -p '$RemoteWorkspace/src' '$RemoteWorkspace/tools' '$RemoteWorkspace/config'"
if ($LASTEXITCODE -ne 0) { throw "连接或登录机载端失败。请先单独执行 ssh $Remote，完成主机指纹确认和登录检查。" }
Write-Host "机载竞赛目录已就绪，开始上传代码。"
foreach ($Package in @("su17_competition_executor", "su17_image_transfer", "su17_pointcloud_bridge")) {
    Write-Host "正在上传竞赛功能包：$Package"
    & scp @sshConnectionArgs @sshIdentityArgs -r (Join-Path $ProjectRoot "src/$Package") "${Remote}:$RemoteWorkspace/src/"
    if ($LASTEXITCODE -ne 0) { throw "上传 $Package 失败。" }
}
Write-Host "正在上传统一配置模块。"
& scp @sshConnectionArgs @sshIdentityArgs -r (Join-Path $ProjectRoot "competition_shared") "${Remote}:$RemoteWorkspace/"
if ($LASTEXITCODE -ne 0) { throw "上传统一配置模块失败。" }
foreach ($File in @("start_onboard_stack.sh", "build_onboard.sh", "check_onboard_messages.py", "onboard_preflight.py", "forward_groundstation_pointcloud.py", "local_tokens.env")) {
    $Source = Join-Path $PSScriptRoot $File
    if (Test-Path -LiteralPath $Source) {
        Write-Host "正在上传工具文件：$File"
        & scp @sshConnectionArgs @sshIdentityArgs $Source "${Remote}:$RemoteWorkspace/tools/"
        if ($LASTEXITCODE -ne 0) { throw "上传 $File 失败。" }
    }
}
Write-Host "正在检查机队配置。"
& ssh @sshConnectionArgs @sshIdentityArgs $Remote "test -f '$RemoteWorkspace/config/fleet.json'"
if ($LASTEXITCODE -notin @(0, 1)) { throw "检查机队配置时 SSH 连接失败，请检查机载端登录状态。" }
$ConfigExists = $LASTEXITCODE -eq 0
if ($SyncConfig -or -not $ConfigExists) {
    Write-Host "正在上传机队配置。"
    & scp @sshConnectionArgs @sshIdentityArgs (Join-Path $ProjectRoot "config/fleet.json") "${Remote}:$RemoteWorkspace/config/fleet.json"
    if ($LASTEXITCODE -ne 0) { throw "上传机队配置失败。" }
}
Write-Host "代码上传完成，正在机载端编译竞赛程序（$Model）；请等待编译结束。"
& ssh @sshConnectionArgs @sshIdentityArgs $Remote "cd '$RemoteWorkspace' && bash ./tools/build_onboard.sh --model $Model"
if ($LASTEXITCODE -ne 0) { throw "竞赛包编译失败，请保留终端输出。" }
Write-Host "竞赛程序已部署（$Model），厂商目录未改动。固定机队启动可执行 --direct --check；需要严格核验时再执行不带 --direct 的 --check。" -ForegroundColor Green
