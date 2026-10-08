$ErrorActionPreference = 'Stop'
$project = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$setup = Join-Path $project 'tools\setup_onboard_ssh.ps1'
$previousProfile = $env:USERPROFILE
$testProfile = Join-Path ([IO.Path]::GetTempPath()) ('zhixin_ssh_test_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testProfile | Out-Null
$global:testAuthorized = $false
$global:testAllowPassword = $true
$global:testAllowLegacy = $false
$global:testInstallCommands = @()
$global:testSshCalls = @()
function ssh {
    $global:testSshCalls += ,(@($args))
    if ($args -contains 'PubkeyAuthentication=no') {
        if ($global:testAllowPassword) { $global:testAuthorized = $true; $global:LASTEXITCODE = 0; $global:testInstallCommands += [string]$args[-1] }
        else { $global:LASTEXITCODE = 255 }
    } elseif ($args -contains 'PreferredAuthentications=publickey') {
        if ($global:testAllowLegacy) { $global:testAuthorized = $true; $global:LASTEXITCODE = 0; $global:testInstallCommands += [string]$args[-1] }
        else { $global:LASTEXITCODE = 255 }
    } elseif ($global:testAuthorized) {
        $global:LASTEXITCODE = 0
        if ([string]$args[-1] -eq 'printf SSH_KEY_OK') { return 'SSH_KEY_OK' }
        $global:testInstallCommands += [string]$args[-1]
    } else { $global:LASTEXITCODE = 255 }
}
try {
    $env:USERPROFILE = $testProfile
    & $setup -UavAddress '192.168.1.202' | Out-Null
    $key = Join-Path $testProfile '.ssh\zhixin_competition_ed25519'
    if (-not (Test-Path -LiteralPath $key) -or -not (Test-Path -LiteralPath ($key + '.pub'))) { throw '未生成竞赛专用密钥对' }
    if ((Test-Path -LiteralPath (Join-Path $testProfile '.ssh\id_ed25519'))) { throw '不应生成或改动默认 SSH 私钥' }
    if ($global:testSshCalls.Count -ne 4 -or -not ($global:testSshCalls[2] -contains 'PubkeyAuthentication=no') -or -not ($global:testSshCalls[2] -contains 'NumberOfPasswordPrompts=3')) { throw '首次授权应先复用旧公钥，再允许密码重试并验证专用公钥' }
    $installLine = [string]$global:testSshCalls[2][-1]
    if ($installLine -notmatch '^printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| bash$') { throw '无法读取机载公钥安装命令' }
    $installBody = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Matches[1]))
    if (-not $installBody.Contains('awk -v kind="$kind" -v blob="$blob"') -or $installBody.Contains('\"')) { throw '机载公钥安装命令引号不正确' }
    $keyRules = @((Get-Acl -LiteralPath $key).Access)
    $allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544')
    foreach ($rule in $keyRules) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($rule.IsInherited -or ($rule.AccessControlType -eq 'Allow' -and $sid -notin $allowed)) { throw '私钥 ACL 包含额外读取权限' }
    }
    $global:testSshCalls = @()
    & $setup -UavAddress '192.168.1.202' | Out-Null
    if ($global:testSshCalls.Count -ne 1) { throw '重复授权不应再次要求机载密码' }
    $global:testAuthorized = $false
    $global:testAllowPassword = $false
    $global:testSshCalls = @()
    try { & $setup -UavAddress '192.168.1.207' | Out-Null; throw '应该报告无法获得机载登录授权' }
    catch {
        if ($_.Exception.Message -notmatch '授权尚未完成|authorized_keys') { throw }
    }
    if ($global:testSshCalls.Count -ne 3) { throw '密码登录失败后不应继续重试' }
    # 已有默认密钥/agent 授权时，不再要求密码。
    $global:testAllowLegacy = $true
    $global:testSshCalls = @()
    & $setup -UavAddress '192.168.1.217' | Out-Null
    if ($global:testSshCalls.Count -ne 3 -or @($global:testSshCalls | Where-Object { $_ -contains 'PubkeyAuthentication=no' }).Count) { throw '已有公钥授权不应要求密码' }
    # 无需无人机连接即可导出；两台电脑的公钥能汇集，私钥不导出。
    $bundle = Join-Path $testProfile 'ground_ssh_keys'
    $global:testSshCalls = @()
    & $setup -ExportPublicKeyDirectory $bundle | Out-Null
    & $setup -ExportPublicKeyDirectory $bundle | Out-Null
    if ($global:testSshCalls.Count -ne 0 -or @(Get-ChildItem -LiteralPath $bundle).Count -ne 1) { throw '导出公钥应离线且幂等' }
    $secondProfile = Join-Path $testProfile 'second-computer'
    $env:USERPROFILE = $secondProfile
    & $setup -ExportPublicKeyDirectory $bundle | Out-Null
    $env:USERPROFILE = $testProfile
    if (@(Get-ChildItem -LiteralPath $bundle -Filter '*.pub').Count -ne 2 -or @(Get-ChildItem -LiteralPath $bundle | Where-Object Extension -ne '.pub').Count) { throw '公钥汇集内容不正确' }
    $global:testSshCalls = @()
    & $setup -UavAddress '192.168.1.217' -PublicKeyDirectory $bundle | Out-Null
    if ($global:testSshCalls.Count -ne 3) { throw '已授权电脑仍应安装汇集公钥并验证' }
    $line = $global:testInstallCommands[-1]
    if ($line -notmatch '^printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| bash$') { throw '公钥安装命令格式错误' }
    $body = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Matches[1]))
    if ($body.Contains("`r")) { throw 'Linux 安装脚本不能含 CRLF' }
    if ($body -notmatch 'printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| while') { throw '缺少汇集公钥内容' }
    $keys = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Matches[1])).Trim() -split "`n"
    if ($keys.Count -ne 2) { throw '汇集公钥未去重或未全部安装' }
    # 在隔离目录执行真正的 Linux shell 安装段，验证重复执行与已有带备注公钥。
    $gitCommand = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCommand) {
        $gitRoot = Split-Path (Split-Path $gitCommand.Source -Parent) -Parent
        $bashPath = Join-Path $gitRoot 'bin\bash.exe'
        if (Test-Path -LiteralPath $bashPath) {
            $remoteDirectory = Join-Path $testProfile 'remote-user'
            $authorizedDirectory = Join-Path $remoteDirectory '.ssh'
            New-Item -ItemType Directory -Force -Path $authorizedDirectory | Out-Null
            $authorizedPath = Join-Path $authorizedDirectory 'authorized_keys'
            $existing = $keys[0] + ' existing-ground-comment'
            $unrelated = '# preserve existing authorization configuration'
            [IO.File]::WriteAllText($authorizedPath, $unrelated + "`n" + $existing + "`n", (New-Object Text.UTF8Encoding($false)))
            $localBody = $body.Replace('~/', ("'" + $remoteDirectory.Replace('\','/') + "'/"))
            $shellPath = Join-Path $testProfile 'install_keys.sh'
            [IO.File]::WriteAllText($shellPath, $localBody, (New-Object Text.UTF8Encoding($false)))
            foreach ($attempt in 1..2) {
                & $bashPath --noprofile --norc $shellPath.Replace('\','/') | Out-Null
                if ($LASTEXITCODE -ne 0) { throw '公钥安装 shell 执行失败' }
            }
            $installed = @(Get-Content -LiteralPath $authorizedPath | Where-Object { $_ -match '^ssh-ed25519 ' })
            if ($installed.Count -ne 2 -or $installed -notcontains $existing -or -not ([IO.File]::ReadAllText($authorizedPath).Contains($unrelated))) { throw '公钥重复安装或覆盖已有授权' }
            Write-Output 'Linux shell 公钥追加、已有备注保留与重复安装检查通过。'
        }
    }
    # 带命令或私钥内容的 .pub 文件应在连接前被拒绝。
    [IO.File]::WriteAllText((Join-Path $bundle 'bad.pub'), '-----BEGIN OPENSSH PRIVATE KEY-----')
    $global:testSshCalls = @()
    try { & $setup -UavAddress '192.168.1.217' -PublicKeyDirectory $bundle | Out-Null; throw '无效公钥应被拒绝' }
    catch { if ($_.Exception.Message -notmatch '公钥文件格式不正确') { throw } }
    if ($global:testSshCalls.Count) { throw '无效公钥不应建立机载连接' }
    Write-Output '竞赛 SSH 密钥生成、ACL、授权复用、密码失败、公钥汇集安装与非法文件拒绝检查通过。'
} finally {
    $env:USERPROFILE = $previousProfile
    Remove-Variable -Name testAuthorized,testAllowPassword,testAllowLegacy,testInstallCommands,testSshCalls -Scope Global -ErrorAction SilentlyContinue
    $resolved = [IO.Path]::GetFullPath($testProfile)
    $temporary = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolved.StartsWith($temporary, [StringComparison]::OrdinalIgnoreCase)) { throw '测试临时目录越界' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
