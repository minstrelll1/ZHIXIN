$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$helper = Join-Path $root 'tools\configure_ground_firewall.ps1'
# 全部防火墙读写和提权都在内存模拟，不改动本机规则，不显示 UAC。
$newParameters = (Get-Command NetSecurity\New-NetFirewallRule).ParameterSets
$setParameters = (Get-Command NetSecurity\Set-NetFirewallRule).ParameterSets
. $helper
$testDirectory = Join-Path ([IO.Path]::GetTempPath()) ('zhixin_firewall_test_' + [guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($testDirectory) | Out-Null
$configFile = Join-Path $testDirectory 'fleet.json'
$script:GroundFirewallLogPath = Join-Path $testDirectory 'firewall.log'
$script:Rules = @{}; $script:Writes = 0; $script:Elevations = 0
$script:Admin = $true; $script:DenyElevation = $false; $script:IgnoreWrites = $false

function Assert($Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Assert-Throws([scriptblock]$Action, [string]$Message) {
    $threw = $false
    try { & $Action | Out-Null } catch { $threw = $true }
    Assert $threw $Message
}
function Assert-Parameters($Bound, $Sets) {
    $valid = @($Sets | Where-Object {
        $names = @($_.Parameters.Name)
        @($Bound.Keys | Where-Object { $_ -notin $names }).Count -eq 0
    })
    Assert ($valid.Count -gt 0) '防火墙参数与本机 NetSecurity 命令参数集不兼容。'
}
function Get-NetFirewallRule {
    param($Name, $PolicyStore, $ErrorAction)
    if ($script:Rules.ContainsKey($Name)) { [pscustomobject]$script:Rules[$Name] }
}
function New-NetFirewallRule {
    [CmdletBinding()]
    param($Name,$DisplayName,$Group,$Description,$Direction,$Action,$Enabled,$Profile,
        $LocalAddress,$RemoteAddress,$Program,$Service,$InterfaceAlias,$InterfaceType,
        $EdgeTraversalPolicy,$Protocol,$LocalPort,$RemotePort,$IcmpType,$PolicyStore)
    Assert-Parameters $PSBoundParameters $newParameters
    Assert (-not $script:Rules.ContainsKey($Name)) '重复创建了同名规则。'
    $script:Writes++
    if (-not $script:IgnoreWrites) { $script:Rules[$Name] = @{} + $PSBoundParameters }
}
function Set-NetFirewallRule {
    [CmdletBinding()]
    param($Name,$NewDisplayName,$Description,$Direction,$Action,$Enabled,$Profile,
        $LocalAddress,$RemoteAddress,$Program,$Service,$InterfaceAlias,$InterfaceType,
        $EdgeTraversalPolicy,$Protocol,$LocalPort,$RemotePort,$IcmpType,$PolicyStore)
    Assert-Parameters $PSBoundParameters $setParameters
    Assert ($script:Rules.ContainsKey($Name)) '更新了不存在的规则。'
    $script:Writes++
    if (-not $script:IgnoreWrites) {
        foreach ($key in $PSBoundParameters.Keys) { $script:Rules[$Name][$key] = $PSBoundParameters[$key] }
        $script:Rules[$Name].DisplayName = $NewDisplayName
    }
}
function Get-NetFirewallAddressFilter {
    [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule)
    process { $Rule }
}
function Get-NetFirewallPortFilter {
    [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule)
    process { $Rule }
}
function Get-NetFirewallApplicationFilter {
    [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule)
    process { $Rule }
}
function Get-NetFirewallServiceFilter {
    [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule)
    process { $Rule }
}
function Get-NetFirewallInterfaceFilter {
    [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule)
    process { $Rule }
}
function Get-NetFirewallInterfaceTypeFilter {
    [CmdletBinding()]param([Parameter(ValueFromPipeline=$true)]$Rule)
    process { $Rule }
}
function Test-GroundFirewallAdministrator { return $script:Admin }
function Request-GroundFirewallElevation([string]$ConfigPath, [int]$Port) {
    $script:Elevations++
    if ($script:DenyElevation) { throw '用户取消了管理员授权。' }
    Set-GroundFirewallRules @(Get-GroundFirewallSpecs $ConfigPath $Port)
}
function Save-Fleet($Fleet) {
    [IO.File]::WriteAllText($configFile, ($Fleet | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
}

try {
    $fleet = @{ schema_version=1; vehicles=@(1..6 | ForEach-Object {
        @{ground_terminal_id=$_; peer_host=('192.168.2.' + (197 + 5 * $_)); ground_host='192.168.1.230'}
    }) }
    Save-Fleet $fleet
    $specs = @(Get-GroundFirewallSpecs $configFile 8000)
    $ips = @('192.168.2.202','192.168.2.207','192.168.2.212','192.168.2.217','192.168.2.222','192.168.2.227')
    Assert ($specs.Count -eq 2) '规则数量不是两条。'
    foreach ($spec in $specs) {
        Assert (Test-GroundFirewallSet $spec.LocalAddress $ips) '本地地址范围错误。'
        Assert (Test-GroundFirewallSet $spec.RemoteAddress $ips) '远端地址范围错误。'
        Assert ($spec.Profile -eq 'Any' -and $spec.Direction -eq 'Inbound' -and $spec.Action -eq 'Allow') '规则策略错误。'
    }
    Assert ($specs[0].LocalPort -eq '8000' -and $specs[1].IcmpType -eq '8') '端口或 ICMP 类型错误。'
    $script:Rules['Unrelated'] = @{Name='Unrelated'; Enabled='False'}
    Assert (Invoke-GroundFirewallSetup $configFile 8000) '首次设置失败。'
    Assert ($script:Writes -eq 2 -and $script:Rules.Count -eq 3) '首次未准确创建两条规则。'
    Assert (Invoke-GroundFirewallSetup $configFile 8000) '重复检查失败。'
    Assert ($script:Writes -eq 2 -and $script:Elevations -eq 0) '重复运行改动了有效规则。'

    $script:Rules['ZhiXin-Ground-Peer-TCP'].RemoteAddress = @('Any')
    $script:Rules['ZhiXin-Ground-Peer-ICMPv4'].Enabled = 'False'
    Assert (Invoke-GroundFirewallSetup $configFile 8010) '规则漂移修复失败。'
    Assert ($script:Writes -eq 4) '漂移修复未更新两条规则。'
    Assert ($script:Rules['Unrelated'].Enabled -eq 'False') '改动了其他规则。'
    Assert ($script:Rules['ZhiXin-Ground-Peer-TCP'].DisplayName -eq '智信-地面互联-TCP8010') '端口变化未更新名称。'

    $script:Admin = $false
    Assert (Invoke-GroundFirewallSetup $configFile 8010) '普通用户读取就绪规则失败。'
    Assert ($script:Elevations -eq 0) '已有正确规则时请求了管理员权限。'
    Assert (-not (Invoke-GroundFirewallSetup $configFile 8020 -InspectOnly)) '只读检查误报成功。'
    Assert ($script:Writes -eq 4 -and $script:Elevations -eq 0) '只读检查修改了规则。'
    Assert (Invoke-GroundFirewallSetup $configFile 8020) '提权配置后检查失败。'
    Assert ($script:Elevations -eq 1 -and $script:Writes -eq 5) '提权次数或修复范围错误。'
    $script:DenyElevation = $true
    Assert-Throws { Invoke-GroundFirewallSetup $configFile 8030 } '取消授权被误报为成功。'
    Assert-Throws { Invoke-GroundFirewallSetup $configFile 8030 -ApplyOnly } '子助手无管理员权限时未拒绝。'
    $script:Admin = $true; $script:IgnoreWrites = $true
    Assert-Throws { Invoke-GroundFirewallSetup $configFile 8030 } '写入未生效时误报了成功。'

    $fleet.vehicles[0].peer_host = ''
    Save-Fleet $fleet
    Assert (Test-GroundFirewallSet (Get-GroundFirewallSpecs $configFile 8000)[0].RemoteAddress $ips) '空地址未使用固定终端映射。'
    $fleet.vehicles[0].peer_host = '10.20.30.40'
    Save-Fleet $fleet
    $custom = (Get-GroundFirewallSpecs $configFile 9000)[0]
    Assert ($custom.RemoteAddress -contains '10.20.30.40' -and $custom.RemoteAddress -notcontains '192.168.2.202') '未按本机配置更新互联地址。'
    foreach ($bad in @('Any','192.168.2.0/24','127.0.0.1','224.0.0.1','192.168.2.207')) {
        $fleet.vehicles[0].peer_host = $bad
        Save-Fleet $fleet
        Assert-Throws { Get-GroundFirewallSpecs $configFile 8000 } "错误地址未拒绝：$bad"
    }

    # 正常入口配置失败后不终止调用方；无效配置在任何防火墙操作之前拒绝。
    $before = $script:Writes
    $entrySource = [IO.File]::ReadAllText($helper).Replace(
        '$script:GroundFirewallRoot = Split-Path $PSScriptRoot -Parent',
        ('$script:GroundFirewallRoot = ''' + $testDirectory.Replace("'", "''") + ''''))
    & ([scriptblock]::Create($entrySource)) -FleetConfig $configFile -WebPort 8000 -WarningVariable setupWarnings
    Assert ($script:Writes -eq $before) '错误配置导致了防火墙写入。'
    Assert (($setupWarnings -join '') -match '地面程序继续启动') '未输出可继续启动的中文错误说明。'

    $start = [IO.File]::ReadAllText((Join-Path $root 'tools\start_ground.ps1'))
    Assert ($start.IndexOf('if ($CheckOnly) { return }') -lt $start.IndexOf('configure_ground_firewall.ps1')) '启动只读检查会触发防火墙配置。'
    Assert ($start.Contains('-FleetConfig $FleetConfig -WebPort $WebPort')) '命令行启动未传递实际配置和端口。'
    $launcher = [IO.File]::ReadAllText((Join-Path $root 'tools\launch_ground_app.ps1'))
    Assert ($launcher.Contains('configure_ground_firewall.ps1')) '复用后台时缺少防火墙检查。'
    Write-Host '地面防火墙测试通过：范围限制、首次创建、重复启动、规则修复、自定义配置、只读检查、授权失败及启动入口。'
} finally {
    # 仅清理本测试在系统临时目录创建的配置和日志，不递归删除。
    foreach ($name in @('fleet.json','firewall.log','ground_logs\firewall.log')) {
        $file = Join-Path $testDirectory $name
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
    }
    $logDirectory = Join-Path $testDirectory 'ground_logs'
    if ([IO.Directory]::Exists($logDirectory)) { [IO.Directory]::Delete($logDirectory) }
    [IO.Directory]::Delete($testDirectory)
}
