param(
    [ValidateRange(1, 6)][int]$LocalUavId = 1,
    [string]$GroundNodeId = "",
    [string]$GroundPeers = "",
    [string]$BindAddress = "0.0.0.0",
    [int]$TcpPort = 56100,
    [string]$WebAddress = "0.0.0.0",
    [int]$WebPort = 8000,
    [string]$AuthToken = "",
    [string]$PeerToken = "",
    [string]$ActiveUavIds = "1,2,3,4,5,6",
    [string]$VideoSources = "",
    [int]$VideoWebRtcPort = 8891,
    [int]$ImageReceiverPort = 56010,
    [string]$ImageAuthToken = "",
    [string]$RecordingSshHosts = "",
    [string]$RecordingSshUser = "amov",
    [ValidateSet("onboard", "groundstation_relay", "groundstation_shared")][string]$PointCloudSource = "groundstation_shared",
    [string]$PointCloudRosbridgeHosts = "",
    [string]$PointCloudTopics = "",
    [int]$PointCloudRosbridgePort = 9090,
    [string]$PointCloudRelayHost = "127.0.0.1",
    [int]$PointCloudRelayPort = 9090,
    [string]$PointCloudRelayUavIds = "1,2,3,4,5,6",
    [string]$PointCloudRelayTopicTemplate = "/uav{uav_id}/octomap_point_cloud_centers/reduce_the_frequency",
    [int]$PointCloudMaxPoints = 20000,
    [double]$PointCloudSaveInterval = 1.0,
    [double]$TrafficInterval = 1.0,
    [string]$UavTrafficHosts = "",
    [string]$TrafficReportToken = "",
    [string]$PointCloudIngestToken = "",
    [string]$MediaMtxDirectory = "",
    [string]$MediaMtxConfig = "",
    [string]$VideoRtspSource = "rtsp://192.168.1.99:1234/test.sdp",
    [switch]$DisableMediaMtx,
    [switch]$DisableImageReceiver,
    [switch]$LegacySingleComputer,
    [switch]$ConfirmLiveConfig
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$BackendRoot = Join-Path $ProjectRoot "competition_backend"
$PythonExe = Join-Path (Join-Path $BackendRoot ".venv") "Scripts\python.exe"
$Receiver = Join-Path $ProjectRoot "src\su17_image_transfer\ground\ground_image_receiver.py"
$ImageRoot = Join-Path $ProjectRoot "received_images"
$LogRoot = Join-Path $ProjectRoot "ground_logs"
$RuntimeRoot = Join-Path $ProjectRoot "ground_runtime"

if (-not $MediaMtxDirectory) {
    $MediaMtxDirectory = Join-Path $ProjectRoot "third_party\mediamtx"
}

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Backend virtual environment not found: $PythonExe. Run .\tools\install_ground_station.ps1 first."
}
if (-not $GroundNodeId) { $GroundNodeId = "ground-uav$LocalUavId" }
if (-not $PeerToken) { $PeerToken = $AuthToken }
if (-not $MediaMtxConfig) {
    $MediaMtxTemplate = Join-Path $MediaMtxDirectory "mediamtx.template.yml"
    if (-not (Test-Path -LiteralPath $MediaMtxTemplate)) {
        throw "MediaMTX template not found: $MediaMtxTemplate"
    }
    New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
    $MediaMtxConfig = Join-Path $RuntimeRoot "mediamtx_uav$LocalUavId.yml"
    $RenderedMediaMtx = (Get-Content -Raw -LiteralPath $MediaMtxTemplate).Replace("__UAV_PATH__", "uav$LocalUavId").Replace("__RTSP_SOURCE__", $VideoRtspSource).Replace("__WEBRTC_PORT__", [string]$VideoWebRtcPort)
    [System.IO.File]::WriteAllText(
        $MediaMtxConfig,
        $RenderedMediaMtx,
        (New-Object System.Text.UTF8Encoding($false))
    )
}
if (-not $VideoSources) {
    $VideoSources = "$LocalUavId=http://127.0.0.1:$VideoWebRtcPort/uav$LocalUavId/"
}

function Stop-ExistingCompetitionBackend {
    param([int]$Port)

    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if (-not $listeners) { return }
    foreach ($listener in $listeners) {
        $ownerPid = [int]$listener.OwningProcess
        if ($ownerPid -eq $PID) { continue }
        $process = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
        $commandLine = ""
        try {
            $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction Stop
            $commandLine = [string]$processInfo.CommandLine
        } catch {
            $commandLine = ""
        }
        $isCompetitionBackend = $commandLine -match "competition_backend"
        if (-not $isCompetitionBackend -and $process -and
            $process.ProcessName -match "python|uvicorn") {
            # Non-elevated PowerShell may not be allowed to read Win32_Process
            # command lines. Confirm the listener is our FastAPI backend before
            # stopping it, using the service's distinctive health response.
            for ($attempt = 0; $attempt -lt 5 -and -not $isCompetitionBackend; $attempt++) {
                try {
                    $health = (Invoke-WebRequest -UseBasicParsing `
                        -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 1).Content
                    $isCompetitionBackend = $health -match '"ok"\s*:\s*true' -and
                        $health -match '"adapter"\s*:'
                } catch {
                    if ($attempt -lt 4) { Start-Sleep -Milliseconds 200 }
                }
            }
        }
        if (-not $isCompetitionBackend) {
            $name = if ($process) { $process.ProcessName } else { "PID $ownerPid" }
            throw "Web port $Port is occupied by $name (PID $ownerPid). Stop that program or choose -WebPort."
        }
        Write-Host "Stopping previous competition backend (PID $ownerPid)..." -ForegroundColor Yellow
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 200
        $remaining = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } while ($remaining -and (Get-Date) -lt $deadline)
    if ($remaining) {
        $pids = ($remaining | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
        throw "Web port $Port is still occupied after stopping the old backend (PID $pids)."
    }
}

Stop-ExistingCompetitionBackend -Port $WebPort

New-Item -ItemType Directory -Path $ImageRoot -Force | Out-Null
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

$env:COMPETITION_ADAPTER = $(if ($LegacySingleComputer) { "tcp" } else { "distributed" })
$env:COMPETITION_GROUND_NODE_ID = $GroundNodeId
$env:COMPETITION_LOCAL_UAV_ID = [string]$LocalUavId
$env:COMPETITION_GROUND_PEERS = $GroundPeers
$env:COMPETITION_PEER_TOKEN = $PeerToken
$env:COMPETITION_TCP_BIND = $BindAddress
$env:COMPETITION_TCP_PORT = [string]$TcpPort
$env:COMPETITION_TCP_TOKEN = $AuthToken
$env:COMPETITION_HOST = $WebAddress
$env:COMPETITION_PORT = [string]$WebPort
$env:COMPETITION_ACTIVE_UAV_IDS = $(if ($LegacySingleComputer) { $ActiveUavIds } else { "1,2,3,4,5,6" })
$env:COMPETITION_VIDEO_SOURCES = $VideoSources
$env:COMPETITION_VIDEO_RTSP_SOURCE = $VideoRtspSource
$env:COMPETITION_IMAGE_ROOT = $ImageRoot
$env:COMPETITION_UAV_SSH_HOSTS = $RecordingSshHosts
$env:COMPETITION_UAV_SSH_USER = $RecordingSshUser
$env:COMPETITION_POINTCLOUD_SOURCE = $PointCloudSource
$env:COMPETITION_POINTCLOUD_ROSBRIDGE_HOSTS = $PointCloudRosbridgeHosts
$env:COMPETITION_POINTCLOUD_TOPICS = $PointCloudTopics
$env:COMPETITION_POINTCLOUD_ROSBRIDGE_PORT = [string]$PointCloudRosbridgePort
$env:COMPETITION_POINTCLOUD_RELAY_HOST = $PointCloudRelayHost
$env:COMPETITION_POINTCLOUD_RELAY_PORT = [string]$PointCloudRelayPort
$env:COMPETITION_POINTCLOUD_RELAY_UAV_IDS = $PointCloudRelayUavIds
$env:COMPETITION_POINTCLOUD_RELAY_TOPIC_TEMPLATE = $PointCloudRelayTopicTemplate
$env:COMPETITION_POINTCLOUD_MAX_POINTS = [string]$PointCloudMaxPoints
$env:COMPETITION_POINTCLOUD_SAVE_INTERVAL = [string]$PointCloudSaveInterval
$env:COMPETITION_POINTCLOUD_INGEST_TOKEN = $PointCloudIngestToken
$env:COMPETITION_TRAFFIC_INTERVAL = [string]$TrafficInterval
$env:COMPETITION_UAV_TRAFFIC_HOSTS = $UavTrafficHosts
$env:COMPETITION_TRAFFIC_REPORT_TOKEN = $TrafficReportToken
if ($ConfirmLiveConfig) { $env:COMPETITION_CONFIRM_LIVE_CONFIG = "true" }
else { Remove-Item Env:COMPETITION_CONFIRM_LIVE_CONFIG -ErrorAction SilentlyContinue }

$OwnedProcesses = @()
try {
    if (-not $DisableMediaMtx) {
        $ExistingMediaMtx = Get-Process -Name "mediamtx" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($ExistingMediaMtx) { Write-Host "MediaMTX:   already running (PID $($ExistingMediaMtx.Id))" }
        else {
            $MediaMtxExe = Join-Path $MediaMtxDirectory "mediamtx.exe"
            if (-not (Test-Path -LiteralPath $MediaMtxExe)) { throw "MediaMTX executable not found: $MediaMtxExe" }
            if (-not (Test-Path -LiteralPath $MediaMtxConfig)) { throw "MediaMTX config not found: $MediaMtxConfig" }
            $MediaMtxProcess = Start-Process -FilePath $MediaMtxExe -ArgumentList @($MediaMtxConfig) -WorkingDirectory $MediaMtxDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot "mediamtx.stdout.log") -RedirectStandardError (Join-Path $LogRoot "mediamtx.stderr.log") -PassThru
            $OwnedProcesses += $MediaMtxProcess
            Write-Host "MediaMTX:   started (PID $($MediaMtxProcess.Id))"
        }
    }
    if (-not $DisableImageReceiver) {
        if (-not (Test-Path -LiteralPath $Receiver)) { throw "Ground image receiver not found: $Receiver" }
        $ReceiverUavIds = $(if ($LegacySingleComputer) { $ActiveUavIds } else { [string]$LocalUavId })
        $ReceiverArguments = @($Receiver, "--bind", "0.0.0.0", "--port", [string]$ImageReceiverPort, "--output", $ImageRoot, "--uav-ids", $ReceiverUavIds)
        if ($ImageAuthToken) { $ReceiverArguments += @("--token", $ImageAuthToken) }
        $ReceiverProcess = Start-Process -FilePath $PythonExe -ArgumentList $ReceiverArguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot "image_receiver_uav$LocalUavId.stdout.log") -RedirectStandardError (Join-Path $LogRoot "image_receiver_uav$LocalUavId.stderr.log") -PassThru
        $OwnedProcesses += $ReceiverProcess
        Write-Host "Image RX:   started for UAV$LocalUavId on port $ImageReceiverPort (PID $($ReceiverProcess.Id))"
    }
    Write-Host "Starting $(if ($LegacySingleComputer) { 'legacy single-computer' } else { 'distributed' }) Windows competition backend..."
    Write-Host "This node:   $GroundNodeId (direct UAV: UAV$LocalUavId only)"
    Write-Host "Web UI:      http://127.0.0.1:$WebPort/"
    Write-Host "UAV TCP:     ${BindAddress}:$TcpPort"
    Write-Host "Ground peers: $(if ($GroundPeers) { $GroundPeers } else { 'none configured' })"
    Write-Host "UAV auth:    $(if ($AuthToken) { 'enabled' } else { 'disabled (lab only)' })"
    Write-Host "Peer auth:   $(if ($PeerToken) { 'enabled' } else { 'disabled (lab only)' })"
    Write-Host "Local video: $VideoSources"
    $PointCloudDescription = if ($PointCloudSource -eq "groundstation_shared") {
        "GroundStation shared ROS topic -> existing web port $WebPort (no new rosbridge session)"
    } elseif ($PointCloudSource -eq "groundstation_relay") {
        "GroundStation local relay ws://${PointCloudRelayHost}:$PointCloudRelayPort ($PointCloudRelayUavIds)"
    } elseif ($PointCloudRosbridgeHosts) {
        "onboard ROSBridge $PointCloudRosbridgeHosts"
    } else {
        "disabled (set -PointCloudRosbridgeHosts or -PointCloudSource groundstation_shared)"
    }
    Write-Host "Pointcloud:  $PointCloudDescription"
    Write-Host "UAV traffic: $(if ($UavTrafficHosts) { $UavTrafficHosts } else { 'disabled (set -UavTrafficHosts)' })"
    Write-Host "RTSP source: $VideoRtspSource"
    Write-Host "Live config: $(if ($ConfirmLiveConfig) { 'CONFIRMED' } else { 'locked' })"
    Push-Location $BackendRoot
    try { & $PythonExe -m competition_backend.api }
    finally { Pop-Location }
}
finally {
    foreach ($Process in $OwnedProcesses) {
        if ($Process -and -not $Process.HasExited) { Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue }
    }
}
