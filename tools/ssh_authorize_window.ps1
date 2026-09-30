param([Parameter(Mandatory=$true)][string]$UavAddress, [string]$UavUser='amov')
$ErrorActionPreference='Stop'
$Host.UI.RawUI.WindowTitle="竞赛机载 SSH 授权 - $UavAddress"
$guard=New-Object System.Threading.Mutex($false, ('Local\ZhiXinSshAuth_' + $UavAddress.Replace(':','_')))
$held=$false
try {
    try { $held=$guard.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $held=$true }
    if (-not $held) { throw '此无人机的授权窗口已经打开，请在原窗口完成输入。' }
    Write-Host "正在配置本机与 $UavUser@$UavAddress 的 SSH 免密码登录。" -ForegroundColor Cyan
    Write-Host '若需要首次授权，请输入机载 Ubuntu 登录密码一次。密码不会显示，也不会保存到网页或日志。'
    & (Join-Path $PSScriptRoot 'setup_onboard_ssh.ps1') -UavAddress $UavAddress -UavUser $UavUser
    if ($LASTEXITCODE -ne 0) { throw 'SSH 授权未完成。' }
    Write-Host '授权成功，网页将继续启动竞赛相关程序。' -ForegroundColor Green
    exit 0
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host '按回车关闭此窗口，之后可在网页点击“重新连接并启动”' | Out-Null
    exit 1
} finally {
    if ($held) { $guard.ReleaseMutex() }
    $guard.Dispose()
}
