param(
    [ValidateRange(0, 6)][int]$LocalUavId = 0,
    [string]$AuthToken = $env:SU17_AUTH_TOKEN,
    [string]$PeerToken = $env:SU17_PEER_TOKEN,
    [string]$UavSshHost = "",
    [string]$VideoRtspSource = "rtsp://192.168.1.99:1234/test.sdp",
    [int]$VideoWebRtcPort = 8891,
    [ValidateSet("onboard", "groundstation_relay", "groundstation_shared")][string]$PointCloudSource = "groundstation_shared",
    [string]$PointCloudRosbridgeHosts = "",
    [string]$PointCloudTopics = "",
    [int]$PointCloudRosbridgePort = 9090,
    [string]$PointCloudRelayHost = "127.0.0.1",
    [int]$PointCloudRelayPort = 9090,
    [string]$PointCloudRelayUavIds = "1,2,3,4,5,6",
    [string]$PointCloudRelayTopicTemplate = "/uav{uav_id}/octomap_point_cloud_centers/reduce_the_frequency",
    [double]$TrafficInterval = 1.0,
    [string]$UavTrafficHosts = "",
    [string]$TrafficReportToken = "",
    [string]$PointCloudIngestToken = "",
    [string]$FleetConfig = "",
    [switch]$ConfirmLiveConfig,
    [switch]$CheckOnly
)


$ErrorActionPreference = "Stop"
if (-not $AuthToken) { $AuthToken = $env:AUTH_TOKEN }
if (-not $PeerToken) { $PeerToken = $env:PEER_TOKEN }
if ($LocalUavId -ne 0) {
    Write-Host "旧启动入口已接入统一流程：请在网页中选择地面终端 $LocalUavId。" -ForegroundColor Cyan
}
$legacyOverrides = @($PSBoundParameters.Keys | Where-Object {
    $_ -notin @("LocalUavId", "AuthToken", "PeerToken", "FleetConfig", "ConfirmLiveConfig", "CheckOnly")
})
if ($legacyOverrides.Count -gt 0) {
    Write-Host "通信、视频和点云参数统一读取 config/fleet.json；旧独立覆盖参数不再生效，请由任务发布端在机队配置页修改。" -ForegroundColor Yellow
}
$arguments = @{}
if ($FleetConfig) { $arguments.FleetConfig = $FleetConfig }
if ($AuthToken) { $arguments.AuthToken = $AuthToken }
if ($PeerToken) { $arguments.PeerToken = $PeerToken }
if ($ConfirmLiveConfig) { $arguments.ConfirmLiveConfig = $true }
if ($CheckOnly) { $arguments.CheckOnly = $true }
& (Join-Path $PSScriptRoot "start_ground.ps1") @arguments
