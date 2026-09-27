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
    [string]$PointCloudCaptureLocalIp = "",
    [string]$PointCloudCaptureRemoteIp = "192.168.1.88",
    [int]$PointCloudCaptureRemotePort = 9090,
    [ValidateRange(1, 6)][int]$PointCloudCaptureUavId = 3,
    [string]$MediaMtxDirectory = "",
    [string]$MediaMtxConfig = "",
    [string]$VideoRtspSource = "rtsp://192.168.1.99:1234/test.sdp",
    [switch]$DisableMediaMtx,
    [switch]$DisableImageReceiver,
    [switch]$LegacySingleComputer,
    [switch]$ConfirmLiveConfig
)

$ErrorActionPreference = "Stop"

# 中文控制台与 Python 子进程统一使用 UTF-8。
$utf8Encoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8Encoding
[Console]::InputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
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
if (-not $DisableMediaMtx -and -not $MediaMtxConfig) {
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
if (-not $DisableMediaMtx -and -not $VideoSources) {
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
            throw "网页端口 $Port 已被 $name（PID $ownerPid）占用。请关闭该程序或指定其他 -WebPort。"
        }
        Write-Host "正在停止之前的竞赛后端（PID $ownerPid）..." -ForegroundColor Yellow
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 200
        $remaining = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } while ($remaining -and (Get-Date) -lt $deadline)
    if ($remaining) {
        $pids = ($remaining | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
        throw "停止旧后端后网页端口 $Port 仍被占用（PID $pids）。"
    }
}

Stop-ExistingCompetitionBackend -Port $WebPort

New-Item -ItemType Directory -Path $ImageRoot -Force | Out-Null
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

if (-not $ImageAuthToken) { $ImageAuthToken = $AuthToken }
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
$env:COMPETITION_POINTCLOUD_CAPTURE_LOCAL_IP = $PointCloudCaptureLocalIp
$env:COMPETITION_POINTCLOUD_CAPTURE_REMOTE_IP = $PointCloudCaptureRemoteIp
$env:COMPETITION_POINTCLOUD_CAPTURE_REMOTE_PORT = [string]$PointCloudCaptureRemotePort
$env:COMPETITION_POINTCLOUD_CAPTURE_UAV_ID = [string]$PointCloudCaptureUavId
$env:COMPETITION_TRAFFIC_INTERVAL = [string]$TrafficInterval
$env:COMPETITION_UAV_TRAFFIC_HOSTS = $UavTrafficHosts
$env:COMPETITION_TRAFFIC_REPORT_TOKEN = $TrafficReportToken
if ($ConfirmLiveConfig) { $env:COMPETITION_CONFIRM_LIVE_CONFIG = "true" }
else { Remove-Item Env:COMPETITION_CONFIRM_LIVE_CONFIG -ErrorAction SilentlyContinue }

$OwnedProcesses = @()
try {
    if (-not $DisableMediaMtx) {
        $ExistingMediaMtx = @(Get-Process -Name "mediamtx" -ErrorAction SilentlyContinue)
        if ($ExistingMediaMtx.Count -gt 1) {
            throw "检测到 $($ExistingMediaMtx.Count) 个 MediaMTX 进程；请关闭重复的视频服务后再启动。"
        }
        if ($ExistingMediaMtx.Count -eq 1) {
            $existingMediaMtxPid = $ExistingMediaMtx[0].Id
            $existingMediaMtxCommand = ""
            try {
                $existingMediaMtxCommand = [string](Get-CimInstance Win32_Process -Filter "ProcessId=$existingMediaMtxPid" -ErrorAction Stop).CommandLine
            } catch { $existingMediaMtxCommand = "" }
            $expectedMediaMtxConfig = Split-Path $MediaMtxConfig -Leaf
            if ($existingMediaMtxCommand -and $existingMediaMtxCommand -notmatch [regex]::Escape($expectedMediaMtxConfig)) {
                throw "已有 MediaMTX 进程没有使用本终端的配置 $expectedMediaMtxConfig；请先关闭旧视频服务，避免重复或串台。"
            }
            Write-Host "MediaMTX：已运行（PID $existingMediaMtxPid）"
        }
        else {
            $MediaMtxExe = Join-Path $MediaMtxDirectory "mediamtx.exe"
            if (-not (Test-Path -LiteralPath $MediaMtxExe)) { throw "未找到 MediaMTX 可执行文件：$MediaMtxExe" }
            if (-not (Test-Path -LiteralPath $MediaMtxConfig)) { throw "未找到 MediaMTX 配置文件：$MediaMtxConfig" }
            $MediaMtxProcess = Start-Process -FilePath $MediaMtxExe -ArgumentList @($MediaMtxConfig) -WorkingDirectory $MediaMtxDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot "mediamtx.stdout.log") -RedirectStandardError (Join-Path $LogRoot "mediamtx.stderr.log") -PassThru
            $OwnedProcesses += $MediaMtxProcess
            Write-Host "MediaMTX：已启动（PID $($MediaMtxProcess.Id)）"
        }
    }
    if (-not $DisableImageReceiver) {
        if (-not (Test-Path -LiteralPath $Receiver)) { throw "未找到地面图片接收器：$Receiver" }
        $ExistingImageReceiver = @(Get-NetTCPConnection -LocalPort $ImageReceiverPort -State Listen -ErrorAction SilentlyContinue)
        if ($ExistingImageReceiver) {
            $pids = ($ExistingImageReceiver | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
            throw "图片接收端口 $ImageReceiverPort 已被占用（PID $pids）；请关闭旧图片接收器，避免重复图传链路。"
        }
        $ReceiverUavIds = $(if ($LegacySingleComputer) { $ActiveUavIds } else { [string]$LocalUavId })
        $ReceiverArguments = @($Receiver, "--bind", "0.0.0.0", "--port", [string]$ImageReceiverPort, "--output", $ImageRoot, "--uav-ids", $ReceiverUavIds)
        if ($ImageAuthToken) { $ReceiverArguments += @("--token", $ImageAuthToken) }
        $ReceiverProcess = Start-Process -FilePath $PythonExe -ArgumentList $ReceiverArguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot "image_receiver_uav$LocalUavId.stdout.log") -RedirectStandardError (Join-Path $LogRoot "image_receiver_uav$LocalUavId.stderr.log") -PassThru
        $OwnedProcesses += $ReceiverProcess
        Write-Host "图片接收器：已为 UAV$LocalUavId 启动，端口 $ImageReceiverPort（PID $($ReceiverProcess.Id)）"
    }
    Write-Host "正在启动 $(if ($LegacySingleComputer) { '旧版单机' } else { '分布式' }) Windows 竞赛后端..."
    Write-Host "本机节点：$GroundNodeId（仅直连 UAV$LocalUavId）"
    Write-Host "网页入口：http://127.0.0.1:$WebPort/"
    Write-Host "无人机 TCP：${BindAddress}:$TcpPort"
    Write-Host "地面终端：$(if ($GroundPeers) { $GroundPeers } else { '未配置' })"
    Write-Host "无人机认证：$(if ($AuthToken) { '已启用' } else { '未启用（仅实验室）' })"
    Write-Host "终端认证：$(if ($PeerToken) { '已启用' } else { '未启用（仅实验室）' })"
    Write-Host "本机视频：$VideoSources"
    $PointCloudDescription = if ($PointCloudSource -eq "groundstation_shared") {
        $(if ($PointCloudCaptureLocalIp) { "复制 GroundStation 已接收数据 ${PointCloudCaptureRemoteIp}:$PointCloudCaptureRemotePort -> 网页 UAV$PointCloudCaptureUavId" } else { "等待 GroundStation 已接收点云转入网页端口 $WebPort" })
    } elseif ($PointCloudSource -eq "groundstation_relay") {
        "GroundStation 本机中继 ws://${PointCloudRelayHost}:$PointCloudRelayPort（$PointCloudRelayUavIds）"
    } elseif ($PointCloudRosbridgeHosts) {
        "机载 ROSBridge $PointCloudRosbridgeHosts"
    } else {
        "未启用（请设置 -PointCloudRosbridgeHosts 或 -PointCloudSource groundstation_shared）"
    }
    Write-Host "点云链路：$PointCloudDescription"
    Write-Host "无人机流量：$(if ($UavTrafficHosts) { $UavTrafficHosts } else { '未启用（请设置 -UavTrafficHosts）' })"
    Write-Host "RTSP 视频源：$VideoRtspSource"
    Write-Host "实机配置：$(if ($ConfirmLiveConfig) { '已确认' } else { '已锁定' })"
    Push-Location $BackendRoot
    try { & $PythonExe -m competition_backend.api }
    finally { Pop-Location }
}
finally {
    foreach ($Process in $OwnedProcesses) {
        if ($Process -and -not $Process.HasExited) { Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue }
    }
}
