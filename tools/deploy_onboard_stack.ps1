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
. (Join-Path $PSScriptRoot 'deploy_transport.ps1')
$deployLogDirectory = Join-Path $ProjectRoot 'ground_logs'
New-Item -ItemType Directory -Force -Path $deployLogDirectory | Out-Null
$deployLogPath = Join-Path $deployLogDirectory ('deploy_onboard_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + [guid]::NewGuid().ToString('N').Substring(0, 8) + '.log')
Write-OnboardDeployLog $deployLogPath "部署目标：$Remote；机型：$Model；诊断日志：$deployLogPath"
if ($SyncConfig -and -not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'config/fleet.json') -PathType Leaf)) {
    throw '本机 config/fleet.json 不存在，尚未上传代码。'
}
Write-Host "正在检查并配置机载 SSH 公钥授权。"
& (Join-Path $PSScriptRoot 'setup_onboard_ssh.ps1') -UavAddress $UavAddress -UavUser $UavUser
$competitionKey = Join-Path $env:USERPROFILE '.ssh\zhixin_competition_ed25519'
$sshIdentityArgs = @()
if (Test-Path -LiteralPath $competitionKey -PathType Leaf) {
    $sshIdentityArgs = @('-i', $competitionKey, '-o', 'IdentitiesOnly=yes')
}
# 首次授权已经在上一步完成；后续不再隐式等待密码或主机确认输入。
$sshConnectionArgs = @('-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1', '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=3')
Write-OnboardDeployLog $deployLogPath "正在准备机载目录和配置（SSH 整体检查上限 20 秒）。"
$prepareCommand = "mkdir -p '$RemoteWorkspace/src' '$RemoteWorkspace/tools' '$RemoteWorkspace/config'"
if ($SyncConfig) {
    Write-OnboardDeployLog $deployLogPath '已指定 -SyncConfig，将直接同步本机机队配置，跳过远端旧配置查询。'
    $prepareCommand += " && printf '\nZHIXIN_DEPLOY_READY\n'"
} else {
    $prepareCommand += " && if test -f '$RemoteWorkspace/config/fleet.json'; then printf '\nZHIXIN_FLEET_EXISTS\n'; else printf '\nZHIXIN_FLEET_MISSING\n'; fi"
}
$sshExecutable = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop).Source
$probe = Invoke-OnboardDeployProbe -FilePath $sshExecutable -ArgumentList (@('-T', '-v') + $sshConnectionArgs + $sshIdentityArgs + @($Remote, $prepareCommand)) -LogPath $deployLogPath
$probeLines = @($probe.Output -split '[\r\n]+' | ForEach-Object { $_.Trim() })
$ConfigExists = $false
if ($SyncConfig) {
    if ($probeLines -notcontains 'ZHIXIN_DEPLOY_READY') { throw "机载目录检查未返回完成标记，停止上传。日志：$deployLogPath" }
} elseif ($probeLines -contains 'ZHIXIN_FLEET_EXISTS') {
    $ConfigExists = $true
    Write-OnboardDeployLog $deployLogPath '机载已有 fleet.json，本次保留；如需覆盖请指定 -SyncConfig。'
} elseif ($probeLines -notcontains 'ZHIXIN_FLEET_MISSING') {
    throw "未取得明确的机队配置检查结果，停止上传。日志：$deployLogPath"
}
Write-OnboardDeployLog $deployLogPath "机载竞赛目录已就绪，开始上传代码。"
foreach ($Package in @("su17_competition_executor", "su17_image_transfer", "su17_pointcloud_bridge")) {
    Write-OnboardDeployLog $deployLogPath "正在上传竞赛功能包：$Package"
    & scp @sshConnectionArgs @sshIdentityArgs -r (Join-Path $ProjectRoot "src/$Package") "${Remote}:$RemoteWorkspace/src/"
    if ($LASTEXITCODE -ne 0) { throw "上传 $Package 失败。" }
}
Write-OnboardDeployLog $deployLogPath "正在上传统一配置模块。"
& scp @sshConnectionArgs @sshIdentityArgs -r (Join-Path $ProjectRoot "competition_shared") "${Remote}:$RemoteWorkspace/"
if ($LASTEXITCODE -ne 0) { throw "上传统一配置模块失败。" }
foreach ($File in @("start_onboard_stack.sh", "build_onboard.sh", "check_onboard_messages.py", "onboard_preflight.py", "forward_groundstation_pointcloud.py", "local_tokens.env")) {
    $Source = Join-Path $PSScriptRoot $File
    if (Test-Path -LiteralPath $Source) {
        Write-OnboardDeployLog $deployLogPath "正在上传工具文件：$File"
        & scp @sshConnectionArgs @sshIdentityArgs $Source "${Remote}:$RemoteWorkspace/tools/"
        if ($LASTEXITCODE -ne 0) { throw "上传 $File 失败。" }
    }
}
if ($SyncConfig -or -not $ConfigExists) {
    Write-OnboardDeployLog $deployLogPath "正在上传机队配置。"
    & scp @sshConnectionArgs @sshIdentityArgs (Join-Path $ProjectRoot "config/fleet.json") "${Remote}:$RemoteWorkspace/config/fleet.json"
    if ($LASTEXITCODE -ne 0) { throw "上传机队配置失败。" }
}
Write-OnboardDeployLog $deployLogPath "代码上传完成，正在机载端编译竞赛程序（$Model）；请等待编译结束。"
& ssh @sshConnectionArgs @sshIdentityArgs $Remote "cd '$RemoteWorkspace' && bash ./tools/build_onboard.sh --model $Model"
if ($LASTEXITCODE -ne 0) { throw "竞赛包编译失败，请保留终端输出。" }
Write-OnboardDeployLog $deployLogPath "竞赛程序编译完成。"
Write-Host "竞赛程序已部署（$Model），厂商目录未改动。固定机队启动可执行 --direct --check；需要严格核验时再执行不带 --direct 的 --check。" -ForegroundColor Green
