param(
    [ValidateSet(0, 1, 3)][int]$LocalUavId = 0,
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
    [string]$PointCloudIngestToken = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent

if ($LocalUavId -eq 0) {
    $LocalAddresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -in @("192.168.2.121", "192.168.2.122", "192.168.2.123", "192.168.2.124", "192.168.2.125", "192.168.2.126") } |
        Select-Object -ExpandProperty IPAddress)
    $Detected = $LocalAddresses | Select-Object -First 1
    if ($Detected) { $LocalUavId = [int]$Detected.Split('.')[-1] - 120 }
    else { throw "Cannot detect this ground node. Use -LocalUavId 1 or -LocalUavId 3." }
}

if (-not $AuthToken) { throw "AuthToken is empty. Set SU17_AUTH_TOKEN or pass -AuthToken." }
if (-not $PeerToken) { throw "PeerToken is empty. Set SU17_PEER_TOKEN or pass -PeerToken." }
if (-not $PointCloudIngestToken) { $PointCloudIngestToken = $AuthToken }

$Peers = "1=http://192.168.2.121:8000;2=http://192.168.2.122:8000;3=http://192.168.2.123:8000;4=http://192.168.2.124:8000;5=http://192.168.2.125:8000;6=http://192.168.2.126:8000"
$GroundNodeId = "ground-uav$LocalUavId"
$VideoSources = "$LocalUavId=http://127.0.0.1:$VideoWebRtcPort/uav$LocalUavId/"
if (-not $UavSshHost -and $LocalUavId -eq 3) { $UavSshHost = "192.168.1.88" }
$RecordingSshHosts = $(if ($UavSshHost) { "$LocalUavId=$UavSshHost" } else { "" })
if (-not $UavTrafficHosts -and $UavSshHost -match '^\d{1,3}(\.\d{1,3}){3}$') {
    $UavTrafficHosts = "$LocalUavId=$UavSshHost"
}

$Installer = Join-Path $PSScriptRoot "install_ground_station.ps1"
$PythonExe = Join-Path (Join-Path (Join-Path $ProjectRoot "competition_backend") ".venv") "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonExe)) {
    Write-Host "Installing ground-station Python environment..." -ForegroundColor Yellow
    & $Installer
}

$Launcher = Join-Path $PSScriptRoot "start_competition_backend_tcp.ps1"
& $Launcher `
    -LocalUavId $LocalUavId `
    -GroundNodeId $GroundNodeId `
    -GroundPeers $Peers `
    -AuthToken $AuthToken `
    -PeerToken $PeerToken `
    -ImageAuthToken $AuthToken `
    -VideoSources $VideoSources `
    -VideoRtspSource $VideoRtspSource `
    -VideoWebRtcPort $VideoWebRtcPort `
    -PointCloudSource $PointCloudSource `
    -PointCloudRosbridgeHosts $PointCloudRosbridgeHosts `
    -PointCloudTopics $PointCloudTopics `
    -PointCloudRosbridgePort $PointCloudRosbridgePort `
    -PointCloudRelayHost $PointCloudRelayHost `
    -PointCloudRelayPort $PointCloudRelayPort `
    -PointCloudRelayUavIds $PointCloudRelayUavIds `
    -PointCloudRelayTopicTemplate $PointCloudRelayTopicTemplate `
    -TrafficInterval $TrafficInterval `
    -UavTrafficHosts $UavTrafficHosts `
    -TrafficReportToken $TrafficReportToken `
    -PointCloudIngestToken $PointCloudIngestToken `
    -RecordingSshHosts $RecordingSshHosts `
    -ConfirmLiveConfig
