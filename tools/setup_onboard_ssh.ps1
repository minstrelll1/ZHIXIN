param(
    [Parameter(Mandatory=$true)][string]$UavAddress,
    [string]$UavUser = 'amov'
)
$ErrorActionPreference = 'Stop'
if ($UavAddress -notmatch '^[A-Za-z0-9.:-]+$' -or $UavAddress.StartsWith('-') -or $UavUser -notmatch '^[a-z_][a-z0-9_-]*$') { throw 'SSH 地址或用户名无效。' }
$sshDirectory = Join-Path $env:USERPROFILE '.ssh'
New-Item -ItemType Directory -Force -Path $sshDirectory | Out-Null
$key = Join-Path $sshDirectory 'zhixin_competition_ed25519'
if (-not (Test-Path -LiteralPath $key)) {
    # 竞赛程序使用独立密钥，避免复用本机已有、可能设置了口令的 id_ed25519。
    # Windows PowerShell 5.1 向原生程序传递空口令需要保留双引号。
    & ssh-keygen.exe -q -t ed25519 -f $key -N '""'
    if ($LASTEXITCODE -ne 0) { throw '生成竞赛专用 SSH 密钥失败，请检查本机 .ssh 目录权限。' }
}
# Windows OpenSSH 允许当前用户、SYSTEM 和管理员读取私钥；不保留其他账户的访问权。
try {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent()
    & icacls.exe $key '/inheritance:r' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '无法关闭继承权限' }
    & icacls.exe $key '/grant:r' "${user.Name}:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '无法设置当前用户权限' }
    $allowed = @($user.User.Value, 'S-1-5-18', 'S-1-5-32-544')
    foreach ($rule in @((Get-Acl -LiteralPath $key).Access)) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($rule.AccessControlType -eq 'Allow' -and $sid -notin $allowed) {
            & icacls.exe $key '/remove:g' "*$sid" | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "无法移除多余账户的读取权限：$sid" }
        }
    }
} catch {
    throw "无法将竞赛专用私钥权限限制为当前 Windows 用户：$key。请检查文件所有者和 NTFS 权限。原因：$($_.Exception.Message)"
}
$publicFile = $key + '.pub'
$savedPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try { $recovered = & ssh-keygen.exe -y -P '""' -f $key 2>$null; $keyStatus = $LASTEXITCODE }
finally { $ErrorActionPreference = $savedPreference }
if ($keyStatus -ne 0 -or -not $recovered) {
    throw "竞赛专用私钥不可用：$key。请检查文件是否损坏或设置了口令；程序不会覆盖已有密钥。"
}
$public = ([string]$recovered).Trim()
if (-not (Test-Path -LiteralPath $publicFile) -or [IO.File]::ReadAllText($publicFile).Trim() -ne $public) {
    [IO.File]::WriteAllText($publicFile, $public + "`n", (New-Object Text.UTF8Encoding($false)))
}
$target = "${UavUser}@${UavAddress}"
$identity = @('-i', $key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=accept-new')
$ErrorActionPreference = 'Continue'
try { $check = & ssh @identity $target 'printf SSH_KEY_OK' 2>$null; $checkStatus = $LASTEXITCODE }
finally { $ErrorActionPreference = $savedPreference }
if ($checkStatus -eq 0 -and ([string]$check).Contains('SSH_KEY_OK')) {
    Write-Host '竞赛专用密钥已获机载端授权，SSH 免密码登录就绪。' -ForegroundColor Green
    return
}
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($public))
$remoteCommand = 'set -e; umask 077; mkdir -p ~/.ssh; chmod 700 ~/.ssh; key=$(printf %s ' + $encoded + ' | base64 -d); touch ~/.ssh/authorized_keys; grep -qxF "$key" ~/.ssh/authorized_keys || printf "\n%s\n" "$key" >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys'
Write-Host "正在首次授权 $target，请输入机载 Ubuntu 用户密码一次；本机原有 SSH 私钥口令不需要输入。" -ForegroundColor Cyan
$scriptEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteCommand))
$ErrorActionPreference = 'Continue'
try {
    & ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ConnectionAttempts=1 -o PubkeyAuthentication=no -o PreferredAuthentications=keyboard-interactive,password -o NumberOfPasswordPrompts=1 $target ('printf %s ' + $scriptEncoded + ' | base64 -d | bash')
    $installStatus = $LASTEXITCODE
} finally { $ErrorActionPreference = $savedPreference }
if ($installStatus -ne 0) {
    throw "机载 Ubuntu 登录未通过，公钥尚未安装。请核对 $target 的密码；若 SSH 服务器禁用密码登录，须先通过已有授权方式将 $publicFile 加入机载 ~/.ssh/authorized_keys。"
}
$ErrorActionPreference = 'Continue'
try { $check = & ssh @identity $target 'printf SSH_KEY_OK'; $checkStatus = $LASTEXITCODE }
finally { $ErrorActionPreference = $savedPreference }
if ($checkStatus -ne 0 -or -not ([string]$check).Contains('SSH_KEY_OK')) {
    throw "公钥已发送，但免密码登录验证失败。请检查机载 ~/.ssh/authorized_keys 权限与 sshd 公钥认证设置；本机专用公钥：$publicFile。"
}
Write-Host "`n竞赛专用 SSH 免密码登录已就绪。" -ForegroundColor Green
