# 兼容原来的命令名称，统一从机队配置读取机型、地址和话题。
param(
    [string]$FleetConfig = "",
    [string]$AuthToken = $env:AUTH_TOKEN,
    [string]$PeerToken = $env:PEER_TOKEN,
    [switch]$ConfirmLiveConfig,
    [switch]$CheckOnly
)
$arguments = @{}
foreach ($name in @("FleetConfig", "AuthToken", "PeerToken", "ConfirmLiveConfig", "CheckOnly")) {
    if ($PSBoundParameters.ContainsKey($name)) { $arguments[$name] = $PSBoundParameters[$name] }
}
& (Join-Path $PSScriptRoot "start_ground.ps1") @arguments
