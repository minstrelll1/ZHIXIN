#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$FleetConfig = '',
    [ValidateRange(1, 65535)][int]$WebPort = 8000,
    [switch]$CheckOnly,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$script:GroundFirewallToolPath = $PSCommandPath
$script:GroundFirewallRoot = Split-Path $PSScriptRoot -Parent
$script:GroundFirewallLogPath = Join-Path $script:GroundFirewallRoot 'ground_logs\firewall.log'

function Write-GroundFirewallLog([string]$Text, [switch]$Warning) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    try {
        $directory = Split-Path $script:GroundFirewallLogPath -Parent
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        [IO.File]::AppendAllText($script:GroundFirewallLogPath, $line + [Environment]::NewLine,
            (New-Object Text.UTF8Encoding($false)))
    } catch { } # 日志目录不可写不妨碍继续启动地面服务。
    if ($Warning) { Write-Warning $Text } else { Write-Host $Text }
}

function Get-GroundFirewallSpecs([string]$ConfigPath, [int]$Port) {
    $fleet = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $vehicles = @($fleet.vehicles)
    if ($vehicles.Count -ne 6 -or $fleet.schema_version -ne 1) { throw '防火墙配置需要有效的六机 fleet.json。' }
    $defaults = @('192.168.2.202', '192.168.2.207', '192.168.2.212', '192.168.2.217', '192.168.2.222', '192.168.2.227')
    $addresses = @(); $ids = @()
    foreach ($vehicle in $vehicles) {
        $id = [int]$vehicle.ground_terminal_id
        if ($id -lt 1 -or $id -gt 6 -or $ids -contains $id) { throw '防火墙配置中的地面终端编号必须为不重复的1～6。' }
        $ids += $id
        $value = ([string]$vehicle.peer_host).Trim()
        if (-not $value) { $value = $defaults[$id - 1] }
        $parsed = $null
        if (-not [Net.IPAddress]::TryParse($value, [ref]$parsed) -or
            $parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or
            $parsed.ToString() -ne $value -or $value -match '^(0|127)\.' -or $parsed.GetAddressBytes()[0] -ge 224) {
            throw "地面互联地址不是有效的单播IPv4地址：$value"
        }
        if ($addresses -contains $value) { throw "地面互联地址重复：$value" }
        $addresses += $value
    }
    $addresses = @($addresses | Sort-Object)
    $common = @{
        Group = 'ZhiXin-Ground-Peer'
        Description = '智信竞赛地面互联：仅允许机队配置内的交换机网口地址。'
        Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'; Profile = 'Any'
        LocalAddress = $addresses; RemoteAddress = $addresses
        Program = 'Any'; Service = 'Any'; InterfaceAlias = 'Any'; InterfaceType = 'Any'
        EdgeTraversalPolicy = 'Block'
    }
    $tcp = $common.Clone()
    $tcp.Name = 'ZhiXin-Ground-Peer-TCP'
    $tcp.DisplayName = "智信-地面互联-TCP$Port"
    $tcp.Protocol = 'TCP'; $tcp.LocalPort = [string]$Port; $tcp.RemotePort = 'Any'
    $ping = $common.Clone()
    $ping.Name = 'ZhiXin-Ground-Peer-ICMPv4'
    $ping.DisplayName = '智信-地面互联-Ping'
    $ping.Protocol = 'ICMPv4'; $ping.IcmpType = '8'
    return @($tcp, $ping)
}

function Test-GroundFirewallSet($Actual, $Expected) {
    $left = @($Actual | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    $right = @($Expected | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    return (($left -join '|') -eq ($right -join '|'))
}

function Test-GroundFirewallRule($Spec) {
    $rules = @(Get-NetFirewallRule -Name $Spec.Name -PolicyStore ActiveStore -ErrorAction SilentlyContinue)
    if ($rules.Count -ne 1) { return $false }
    $rule = $rules[0]
    if ([string]$rule.Enabled -ne 'True' -or [string]$rule.Direction -ne 'Inbound' -or
        [string]$rule.Action -ne 'Allow' -or [string]$rule.Profile -ne 'Any' -or
        [string]$rule.EdgeTraversalPolicy -ne 'Block') { return $false }
    $address = $rule | Get-NetFirewallAddressFilter -ErrorAction Stop
    if (-not (Test-GroundFirewallSet $address.LocalAddress $Spec.LocalAddress) -or
        -not (Test-GroundFirewallSet $address.RemoteAddress $Spec.RemoteAddress)) { return $false }
    $port = $rule | Get-NetFirewallPortFilter -ErrorAction Stop
    if ($Spec.Protocol -eq 'TCP') {
        if ([string]$port.Protocol -notin @('TCP', '6') -or
            -not (Test-GroundFirewallSet $port.LocalPort @($Spec.LocalPort)) -or
            -not (Test-GroundFirewallSet $port.RemotePort @('Any'))) { return $false }
    } else {
        if ([string]$port.Protocol -notin @('ICMPv4', '1') -or
            -not (Test-GroundFirewallSet $port.IcmpType @('8'))) { return $false }
    }
    $app = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
    $service = $rule | Get-NetFirewallServiceFilter -ErrorAction Stop
    $interface = $rule | Get-NetFirewallInterfaceFilter -ErrorAction Stop
    $interfaceType = $rule | Get-NetFirewallInterfaceTypeFilter -ErrorAction Stop
    return ([string]$app.Program -eq 'Any' -and [string]$service.Service -eq 'Any' -and
            (Test-GroundFirewallSet $interface.InterfaceAlias @('Any')) -and
            [string]$interfaceType.InterfaceType -eq 'Any')
}

function Test-GroundFirewallReady($Specs) {
    foreach ($spec in $Specs) { if (-not (Test-GroundFirewallRule $spec)) { return $false } }
    return $true
}

function Test-GroundFirewallAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Set-GroundFirewallRules($Specs) {
    foreach ($spec in $Specs) {
        if (Test-GroundFirewallRule $spec) { continue }
        $existing = @(Get-NetFirewallRule -Name $spec.Name -PolicyStore PersistentStore -ErrorAction SilentlyContinue)
        if ($existing.Count -gt 0) {
            # 只更新本脚本固定名称的两条规则，不删除用户、厂商或系统的其他规则。
            $update = $spec.Clone()
            $update.NewDisplayName = $update.DisplayName
            $update.Remove('DisplayName')
            $update.Remove('Group') # Set 的 Group 是筛选条件，不能与 Name 一同用于改名。
            Set-NetFirewallRule @update -PolicyStore PersistentStore -ErrorAction Stop | Out-Null
        } else {
            New-NetFirewallRule @spec -PolicyStore PersistentStore -ErrorAction Stop | Out-Null
        }
    }
    if (-not (Test-GroundFirewallReady $Specs)) {
        throw '规则写入后未能在有效策略中验证，请检查 Windows 防火墙策略。'
    }
}

function Request-GroundFirewallElevation([string]$ConfigPath, [int]$Port) {
    $shell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -FleetConfig "{1}" -WebPort {2} -Apply' -f
        $script:GroundFirewallToolPath, $ConfigPath, $Port
    # 仅此短时助手提权，后台服务保持原权限；不传递认证令牌。
    $process = Start-Process -FilePath $shell -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru
    try {
        if (-not $process.WaitForExit(30000)) { throw '防火墙配置助手尚未完成，请查看 ground_logs\firewall.log。' }
        if ($process.ExitCode -ne 0) { throw '管理员防火墙配置未成功，请查看 ground_logs\firewall.log。' }
    } finally { $process.Dispose() }
}

function Invoke-GroundFirewallSetup([string]$ConfigPath, [int]$Port, [switch]$InspectOnly, [switch]$ApplyOnly) {
    if (-not $ConfigPath) { $ConfigPath = Join-Path $script:GroundFirewallRoot 'config\fleet.json' }
    $ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
    $specs = @(Get-GroundFirewallSpecs $ConfigPath $Port)
    if (Test-GroundFirewallReady $specs) {
        Write-GroundFirewallLog "地面互联防火墙规则已就绪：TCP $Port 和 Ping，仅限机队配置的六个互联IP。"
        return $true
    }
    if ($InspectOnly) {
        Write-GroundFirewallLog '地面互联防火墙规则需要配置；本次仅检查，未修改。'
        return $false
    }
    if (Test-GroundFirewallAdministrator) {
        Set-GroundFirewallRules $specs
    } elseif ($ApplyOnly) {
        throw '配置 Windows 防火墙需要管理员权限。'
    } else {
        Write-GroundFirewallLog '正在请求 Windows 管理员授权，以配置地面互联 TCP 和 Ping 规则。'
        Request-GroundFirewallElevation $ConfigPath $Port
        if (-not (Test-GroundFirewallReady $specs)) { throw '授权后规则仍未通过检查，请查看 ground_logs\firewall.log。' }
    }
    Write-GroundFirewallLog "地面互联防火墙规则已配置并验证：TCP $Port 和 Ping。"
    return $true
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $ready = Invoke-GroundFirewallSetup -ConfigPath $FleetConfig -Port $WebPort -InspectOnly:$CheckOnly -ApplyOnly:$Apply
        if ($CheckOnly) { [pscustomobject]@{ Ready = [bool]$ready; WebPort = $WebPort; LogFile = $script:GroundFirewallLogPath } }
    } catch {
        Write-GroundFirewallLog ("地面互联防火墙配置未完成：{0}；地面程序继续启动，下次启动时重试。日志：ground_logs\firewall.log" -f $_.Exception.Message) -Warning
        if ($Apply) { exit 1 }
    }
}
