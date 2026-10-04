#requires -Version 5.1
[CmdletBinding()]
param(
    [int]$TerminalId = 0,
    [string]$ExternalAdapterName = '',
    [string]$InternalAdapterName = '',
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'

function Get-GroundAddress([int]$Id) {
    $suffixes = @(202, 207, 212, 217, 222, 227)
    if ($Id -lt 1 -or $Id -gt 6) { throw '地面终端编号只能是 1～6。' }
    return "192.168.2.$($suffixes[$Id - 1])"
}

function Get-EthernetCandidates {
    $physical = @(Get-NetAdapter -Physical -ErrorAction Stop)
    $cim = @(Get-CimInstance -ClassName Win32_NetworkAdapter -ErrorAction Stop)
    foreach ($adapter in $physical) {
        if ($adapter.Name -match '(?i)wi-?fi|wlan|wireless|bluetooth|无线' -or
            $adapter.InterfaceDescription -match '(?i)wi-?fi|wlan|wireless|bluetooth|802\.11|无线') { continue }
        $details = @($cim | Where-Object { $_.InterfaceIndex -eq $adapter.InterfaceIndex -and $_.PhysicalAdapter })
        if ($details.Count -ne 1 -or $details[0].AdapterTypeID -ne 0) { continue }
        [pscustomobject]@{
            Name = [string]$adapter.Name
            Index = [uint32]$adapter.InterfaceIndex
            Status = [string]$adapter.Status
            Description = [string]$adapter.InterfaceDescription
            PnpId = [string]$details[0].PNPDeviceID
        }
    }
}

function Select-GroundAdapter($Candidates, [string]$Name, [string]$Bus, [string]$Role) {
    if ($Name) {
        $selected = @($Candidates | Where-Object { $_.Name -eq $Name })
    } else {
        $selected = @($Candidates | Where-Object { $_.PnpId -match "^(?i:$Bus)\\" })
    }
    if ($selected.Count -ne 1) {
        $list = ($Candidates | ForEach-Object {
            "  $($_.Name) (接口 $($_.Index)，$($_.Status)，$($_.Description)，PNP=$($_.PnpId))"
        }) -join "`n"
        throw "无法唯一确定$Role网卡（匹配到 $($selected.Count) 个）。可用以太网卡：`n$list`n请用 -ExternalAdapterName 和 -InternalAdapterName 指定准确的网卡名称。未修改任何 IP。"
    }
    if ($selected[0].PnpId -notmatch "^(?i:$Bus)\\") {
        throw "$Role网卡 $($selected[0].Name) 不是 $Bus 总线设备。未修改任何 IP。"
    }
    if ($selected[0].Status -eq 'Disabled') { throw "$Role网卡已禁用：$($selected[0].Name)。未修改任何 IP。" }
    return $selected[0]
}

function Set-DedicatedIPv4($Adapter, [string]$Address) {
    $index = $Adapter.Index
    Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Disabled -ErrorAction Stop
    $existing = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue)
    foreach ($ip in $existing) {
        if ($ip.IPAddress -ne $Address -or $ip.PrefixLength -ne 24) {
            Remove-NetIPAddress -InputObject $ip -Confirm:$false -ErrorAction Stop
        }
    }
    $defaultRoutes = @(Get-NetRoute -InterfaceIndex $index -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
    foreach ($route in $defaultRoutes) {
        Remove-NetRoute -InputObject $route -Confirm:$false -ErrorAction Stop
    }
    $current = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $Address -and $_.PrefixLength -eq 24 })
    if ($current.Count -eq 0) {
        New-NetIPAddress -InterfaceIndex $index -IPAddress $Address -PrefixLength 24 -AddressFamily IPv4 -ErrorAction Stop | Out-Null
    }
    $verified = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -ErrorAction Stop |
        Where-Object { $_.IPAddress -eq $Address -and $_.PrefixLength -eq 24 })
    if ($verified.Count -ne 1) { throw "网卡 $($Adapter.Name) 的 $Address/24 配置后验证失败。" }
}

function Invoke-GroundNetworkConfiguration {
    if ($TerminalId -eq 0) {
        $answer = Read-Host '请输入本机地面终端编号（1～6）'
        if ($answer -notmatch '^[1-6]$') { throw '地面终端编号只能是 1～6。' }
        $script:TerminalId = [int]$answer
    }
    $peerAddress = Get-GroundAddress $TerminalId
    $videoAddress = '192.168.1.230'

    if (-not $Preview) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw '请在“以管理员身份运行”的 PowerShell 中执行。未修改任何 IP。'
        }
    }

    $candidates = @(Get-EthernetCandidates)
    $external = Select-GroundAdapter $candidates $ExternalAdapterName 'USB' '外置图传'
    $internal = Select-GroundAdapter $candidates $InternalAdapterName 'PCI' '内置交换机'
    if ($external.Index -eq $internal.Index) { throw '外置与内置网卡不能是同一个接口。未修改任何 IP。' }

    $allIPs = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop)
    foreach ($pair in @(@($external, $videoAddress), @($internal, $peerAddress))) {
        $conflicts = @($allIPs | Where-Object { $_.IPAddress -eq $pair[1] -and $_.InterfaceIndex -ne $pair[0].Index })
        if ($conflicts.Count -gt 0) { throw "地址 $($pair[1]) 已被其他网卡使用。未修改任何 IP。" }
    }

    Write-Host "地面终端 $TerminalId 的网卡配置："
    Write-Host "  外置图传：$($external.Name)（接口 $($external.Index)） -> $videoAddress/24"
    Write-Host "  内置互联：$($internal.Name)（接口 $($internal.Index)） -> $peerAddress/24"
    Write-Host '  两个专用网口均不配置默认网关；不修改其他网卡。'
    if ($Preview) { Write-Host '预览完成，未修改任何 IP。'; return }

    $projectRoot = Split-Path -Parent $PSScriptRoot
    $logDir = Join-Path $projectRoot 'ground_logs'
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $backup = Join-Path $logDir ("network_before_{0}.json" -f (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
    @($external, $internal) | ForEach-Object {
        [pscustomobject]@{
            Adapter = $_
            IPv4Interface = Get-NetIPInterface -InterfaceIndex $_.Index -AddressFamily IPv4 |
                Select-Object InterfaceIndex, Dhcp
            IPv4Addresses = @(Get-NetIPAddress -InterfaceIndex $_.Index -AddressFamily IPv4 -ErrorAction SilentlyContinue |
                Select-Object IPAddress, PrefixLength, PrefixOrigin, AddressState)
            DefaultRoutes = @(Get-NetRoute -InterfaceIndex $_.Index -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
                Select-Object DestinationPrefix, NextHop, RouteMetric, PolicyStore)
        }
    } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $backup -Encoding UTF8
    Write-Host "原网卡设置已记录：$backup"

    Set-DedicatedIPv4 $external $videoAddress
    Set-DedicatedIPv4 $internal $peerAddress
    Write-Host '两个网卡的静态 IPv4 地址已配置并验证。'
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-GroundNetworkConfiguration }
