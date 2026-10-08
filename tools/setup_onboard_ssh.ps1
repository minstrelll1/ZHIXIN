param(
    [string]$UavAddress = '',
    [string]$UavUser = 'amov',
    [string]$ExportPublicKeyDirectory = '',
    [string]$PublicKeyDirectory = ''
)
$ErrorActionPreference = 'Stop'
if ($ExportPublicKeyDirectory -and ($UavAddress -or $PublicKeyDirectory)) { throw '导出公钥与机载授权请分开执行。' }
if (-not $ExportPublicKeyDirectory -and ($UavAddress -notmatch '^[A-Za-z0-9.:-]+$' -or $UavAddress.StartsWith('-') -or $UavUser -notmatch '^[a-z_][a-z0-9_-]*$')) { throw 'SSH 地址或用户名无效。' }
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
$public = (([string]$recovered).Trim() -split '\s+')[0..1] -join ' '
if (-not (Test-Path -LiteralPath $publicFile) -or [IO.File]::ReadAllText($publicFile).Trim() -ne $public) {
    [IO.File]::WriteAllText($publicFile, $public + "`n", (New-Object Text.UTF8Encoding($false)))
}
if ($ExportPublicKeyDirectory) {
    New-Item -ItemType Directory -Force -Path $ExportPublicKeyDirectory | Out-Null
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $fingerprint = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($public)))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
    $exported = Join-Path $ExportPublicKeyDirectory ('ground-' + $fingerprint.Substring(0, 16) + '.pub')
    [IO.File]::WriteAllText($exported, $public + "`n", (New-Object Text.UTF8Encoding($false)))
    Write-Host "本机公钥已导出：$exported。私钥仍保留在本机。" -ForegroundColor Green
    return
}
$publicKeys = @($public)
if ($PublicKeyDirectory) {
    if (-not (Test-Path -LiteralPath $PublicKeyDirectory -PathType Container)) { throw '公钥汇集目录不存在。' }
    $files = @(Get-ChildItem -LiteralPath $PublicKeyDirectory -Filter '*.pub' -File)
    if ($files.Count -eq 0) { throw '公钥汇集目录中没有 .pub 文件。' }
    foreach ($file in $files) {
        $candidate = [IO.File]::ReadAllText($file.FullName, [Text.Encoding]::UTF8).Trim()
        if ($candidate -notmatch '^ssh-ed25519 [A-Za-z0-9+/]+={0,3}(?: [^\r\n]*)?$') { throw "公钥文件格式不正确：$($file.Name)，只接受本工具导出的单个 Ed25519 公钥。" }
        $ErrorActionPreference = 'Continue'
        try { $null = & ssh-keygen.exe -l -f $file.FullName 2>$null; $publicStatus = $LASTEXITCODE }
        finally { $ErrorActionPreference = $savedPreference }
        if ($publicStatus -ne 0) { throw "公钥校验失败：$($file.Name)" }
        $publicKeys += (($candidate -split ' ')[0..1] -join ' ')
    }
}
$publicKeys = @($publicKeys | Select-Object -Unique)
$target = "${UavUser}@${UavAddress}"
$identity = @('-i', $key, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1', '-o', 'StrictHostKeyChecking=accept-new')
$ErrorActionPreference = 'Continue'
try { $check = & ssh @identity $target 'printf SSH_KEY_OK' 2>$null; $checkStatus = $LASTEXITCODE }
finally { $ErrorActionPreference = $savedPreference }
$dedicatedReady = $checkStatus -eq 0 -and ([string]$check).Contains('SSH_KEY_OK')
if ($dedicatedReady -and -not $PublicKeyDirectory) {
    Write-Host '竞赛专用密钥已获机载端授权，SSH 免密码登录就绪。' -ForegroundColor Green
    return
}
$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($publicKeys -join "`n") + "`n"))
# 仅追加公钥，不覆盖已有授权，也不改变 SSH 服务的认证策略。
$remoteCommand = @'
set -eu
umask 077
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
printf %s __KEYS__ | base64 -d | while IFS= read -r key; do
    kind=${key%% *}
    blob=${key#* }
    if ! awk -v kind="$kind" -v blob="$blob" '$1 == kind && $2 == blob {found=1} END {exit !found}' ~/.ssh/authorized_keys; then
        printf '\n%s\n' "$key" >> ~/.ssh/authorized_keys
    fi
done
chmod 600 ~/.ssh/authorized_keys
printf SSH_KEYS_INSTALLED
'@
$remoteCommand = $remoteCommand.Replace('__KEYS__', $encoded).Replace("`r`n", "`n")
$scriptEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteCommand))
$installCommand = 'printf %s ' + $scriptEncoded + ' | base64 -d | bash'
$installStatus = 255
if ($dedicatedReady) {
    Write-Host "正在使用已授权的竞赛密钥安装 $($publicKeys.Count) 个地面公钥。"
    $ErrorActionPreference = 'Continue'
    try { $null = & ssh @identity $target $installCommand; $installStatus = $LASTEXITCODE }
    finally { $ErrorActionPreference = $savedPreference }
} else {
    # 使用已有默认密钥、SSH 配置或已解锁的 agent，不弹出旧私钥口令。
    Write-Host '正在尝试本机已有的 SSH 公钥授权。'
    $ErrorActionPreference = 'Continue'
    try {
        $null = & ssh -o BatchMode=yes -o PreferredAuthentications=publickey -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o ConnectionAttempts=1 $target $installCommand 2>$null
        $installStatus = $LASTEXITCODE
    } finally { $ErrorActionPreference = $savedPreference }
    if ($installStatus -ne 0) {
        Write-Host "正在首次授权 $target，请输入机载 Ubuntu 用户密码；最多可输入三次，密码不保存。本机原有 SSH 私钥口令不需要输入。" -ForegroundColor Cyan
        $ErrorActionPreference = 'Continue'
        try {
            & ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ConnectionAttempts=1 -o PubkeyAuthentication=no -o PreferredAuthentications=keyboard-interactive,password -o NumberOfPasswordPrompts=3 $target $installCommand
            $installStatus = $LASTEXITCODE
        } finally { $ErrorActionPreference = $savedPreference }
    }
}
if ($installStatus -ne 0) {
    throw "机载登录或公钥安装未通过，授权尚未完成。请先验证 ssh $target 可登录；检查用户名、Ubuntu 密码或已有密钥授权。若密码登录被禁用，须通过机载本地终端或已有授权电脑安装公钥。可按 README 的六机公钥预授权步骤一次授权所有地面电脑。"
}
$ErrorActionPreference = 'Continue'
try { $check = & ssh @identity $target 'printf SSH_KEY_OK'; $checkStatus = $LASTEXITCODE }
finally { $ErrorActionPreference = $savedPreference }
if ($checkStatus -ne 0 -or -not ([string]$check).Contains('SSH_KEY_OK')) {
    throw "公钥已发送，但免密码登录验证失败。请检查机载 ~/.ssh/authorized_keys 权限与 sshd 公钥认证设置；本机专用公钥：$publicFile。"
}
Write-Host "`n已安装 $($publicKeys.Count) 个地面公钥，本机竞赛专用 SSH 免密码登录验证通过。" -ForegroundColor Green
