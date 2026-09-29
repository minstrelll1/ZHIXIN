param(
    [Parameter(Mandatory=$true)][string]$UavAddress,
    [string]$UavUser = 'amov'
)
$ErrorActionPreference = 'Stop'
if ($UavAddress -notmatch '^[A-Za-z0-9.:-]+$' -or $UavAddress.StartsWith('-') -or $UavUser -notmatch '^[a-z_][a-z0-9_-]*$') { throw 'SSH 地址或用户名无效。' }
$sshDirectory = Join-Path $env:USERPROFILE '.ssh'
New-Item -ItemType Directory -Force -Path $sshDirectory | Out-Null
$key = Join-Path $sshDirectory 'id_ed25519'
if (-not (Test-Path -LiteralPath $key)) {
    # Windows PowerShell 5.1 向原生程序传递空密码参数需要保留双引号。
    & ssh-keygen.exe -q -t ed25519 -f $key -N '""'
    if ($LASTEXITCODE -ne 0) { throw '生成本机 SSH 密钥失败。' }
}
$publicFile = $key + '.pub'
if (-not (Test-Path -LiteralPath $publicFile)) { throw '本机公钥文件缺失，请恢复 .ssh/id_ed25519.pub。' }
$public = [IO.File]::ReadAllText($publicFile).Trim()
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($public))
$remoteCommand = 'umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; key=$(printf %s ' + $encoded + ' | base64 -d); touch ~/.ssh/authorized_keys; grep -qxF "$key" ~/.ssh/authorized_keys || printf "\n%s\n" "$key" >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys'
Write-Host '首次配置时请输入机载 Ubuntu 登录密码。'
$scriptEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteCommand))
& ssh -o StrictHostKeyChecking=accept-new "${UavUser}@${UavAddress}" ('printf %s ' + $scriptEncoded + ' | base64 -d | bash')
if ($LASTEXITCODE -ne 0) { throw '安装公钥失败。' }
& ssh -o BatchMode=yes -o ConnectTimeout=5 "${UavUser}@${UavAddress}" 'printf SSH_KEY_OK'
if ($LASTEXITCODE -ne 0) { throw '免密码登录验证失败，请检查本机私钥是否设置了密码并已加载到 ssh-agent。' }
Write-Host "`nSSH 免密码登录已就绪。"
