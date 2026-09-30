$ErrorActionPreference = 'Stop'
$project = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$setup = Join-Path $project 'tools\setup_onboard_ssh.ps1'
$previousProfile = $env:USERPROFILE
$testProfile = Join-Path ([IO.Path]::GetTempPath()) ('zhixin_ssh_test_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testProfile | Out-Null
$global:testAuthorized = $false
$global:testAllowPassword = $true
$global:testSshCalls = @()
function ssh {
    $global:testSshCalls += ,(@($args))
    if ($args -contains 'PubkeyAuthentication=no') {
        if ($global:testAllowPassword) { $global:testAuthorized = $true; $global:LASTEXITCODE = 0 }
        else { $global:LASTEXITCODE = 255 }
    } elseif ($global:testAuthorized) {
        $global:LASTEXITCODE = 0
        return 'SSH_KEY_OK'
    } else { $global:LASTEXITCODE = 255 }
}
try {
    $env:USERPROFILE = $testProfile
    & $setup -UavAddress '192.168.1.202' | Out-Null
    $key = Join-Path $testProfile '.ssh\zhixin_competition_ed25519'
    if (-not (Test-Path -LiteralPath $key) -or -not (Test-Path -LiteralPath ($key + '.pub'))) { throw '未生成竞赛专用密钥对' }
    if ((Test-Path -LiteralPath (Join-Path $testProfile '.ssh\id_ed25519'))) { throw '不应生成或改动默认 SSH 私钥' }
    if ($global:testSshCalls.Count -ne 3 -or -not ($global:testSshCalls[1] -contains 'PubkeyAuthentication=no')) { throw '首次授权必须只用一次密码认证并验证公钥' }
    $installLine = [string]$global:testSshCalls[1][-1]
    if ($installLine -notmatch '^printf %s ([A-Za-z0-9+/=]+) \| base64 -d \| bash$') { throw '无法读取机载公钥安装命令' }
    $installBody = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Matches[1]))
    if (-not $installBody.Contains('grep -qxF "$key"') -or $installBody.Contains('\"')) { throw '机载公钥安装命令引号不正确' }
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
        if ($_.Exception.Message -notmatch '公钥尚未安装|authorized_keys') { throw }
    }
    if ($global:testSshCalls.Count -ne 2) { throw '密码登录失败后不应继续重试' }
    Write-Output '竞赛 SSH 专用密钥生成、ACL、重复授权和密码失败路径通过。'
} finally {
    $env:USERPROFILE = $previousProfile
    Remove-Variable -Name testAuthorized,testAllowPassword,testSshCalls -Scope Global -ErrorAction SilentlyContinue
    $resolved = [IO.Path]::GetFullPath($testProfile)
    $temporary = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (-not $resolved.StartsWith($temporary, [StringComparison]::OrdinalIgnoreCase)) { throw '测试临时目录越界' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
