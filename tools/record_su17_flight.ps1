param(
    [string]$UavAddress = "192.168.1.88",
    [string]$UavUser = "amov",
    [int]$UavId = 1,
    [ValidateSet("p600", "su17")]
    [string]$Model = "su17",
    [int]$LocalRosUavId = 0,
    [string]$VendorWorkspace = "",
    [string]$OutputRoot = "",
    [string]$PythonExe = "python",
    [switch]$OpenReport,
    [string]$FlightName = "",
    [switch]$NonInteractive,
    [string]$StopSignalPath = "",
    [ValidateSet("core", "full", "complex")]
    [string]$RecordingMode = "core"
)

$ErrorActionPreference = "Stop"

# 采集日志统一使用 UTF-8，避免 Windows 默认代码页导致中文乱码。
$utf8Encoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8Encoding
[Console]::InputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $OutputRoot = Join-Path $projectRoot "flight_records"
}

if ([string]::IsNullOrWhiteSpace($FlightName)) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $FlightName = "uav{0}_{1}" -f $UavId, $timestamp
}
if ($FlightName -notmatch "^uav$UavId`_\d{8}_\d{6}$") {
    throw "飞行记录名称不安全或格式无效：$FlightName"
}
$flightName = $FlightName
$modelLower = $Model.ToLowerInvariant()
$recordingMode = $RecordingMode.ToLowerInvariant()
if ($recordingMode -eq "complex") {
    $recordingMode = "full"
}
if ($LocalRosUavId -eq 0) {
    if ($modelLower -eq "su17") {
        $LocalRosUavId = 1
    }
    else {
        $LocalRosUavId = $UavId
    }
}
if ($LocalRosUavId -lt 1 -or $LocalRosUavId -gt 6) {
    throw "LocalRosUavId 必须在 1～6 之间。"
}
if ([string]::IsNullOrWhiteSpace($VendorWorkspace)) {
    if ($modelLower -eq "su17") {
        $VendorWorkspace = "/home/amov/su17_experiment"
    }
    else {
        $VendorWorkspace = "/home/amov/p600_experiment"
    }
}
$VendorWorkspace = $VendorWorkspace.TrimEnd('/')
$remoteRoot = "/home/amov/flight_records"
$remoteDir = "$remoteRoot/$flightName"
$localDir = Join-Path $OutputRoot $flightName
$remotePidFile = "$remoteDir/rosbag.pid"
$remoteLogFile = "$remoteDir/rosbag_record.log"
$remoteBagPrefix = "$remoteDir/$flightName"
$remoteOdometryCsv = "$remoteDir/position_odometry.csv"
$remoteRawOdometryCsv = "$remoteDir/raw_mid360_odometry.csv"
$remoteCommandCsv = "$remoteDir/uav_command.csv"
$remoteStateCsv = "$remoteDir/uav_state.csv"
$remoteControlStateCsv = "$remoteDir/control_state.csv"
$remoteStatusTextCsv = "$remoteDir/px4_status_text.csv"
$remoteTaskStatusCsv = "$remoteDir/task_status.csv"
$remotePrometheusTextCsv = "$remoteDir/prometheus_text_info.csv"
$remoteBatteryCsv = "$remoteDir/battery.csv"
$remoteTimeReferenceCsv = "$remoteDir/time_reference.csv"
$remoteBagManifest = "$remoteDir/bag_sha256.txt"
$reportProgram = Join-Path $PSScriptRoot "bsa_position_report.py"

if ($modelLower -eq "p600") {
    $positionTopic = "/uav$LocalRosUavId/prometheus/odom"
}
else {
    $positionTopic = "/Odometry"
}
$topics = @(
    $positionTopic,
    "/uav$LocalRosUavId/mavros/vision_pose/pose",
    "/uav$LocalRosUavId/mavros/local_position/pose",
    "/uav$LocalRosUavId/mavros/local_position/velocity_local",
    "/uav$LocalRosUavId/mavros/imu/data",
    "/uav$LocalRosUavId/mavros/battery",
    "/uav$LocalRosUavId/mavros/time_reference",
    "/uav$LocalRosUavId/mavros/rc/in",
    "/uav$LocalRosUavId/mavros/state",
    "/uav$LocalRosUavId/mavros/extended_state",
    "/uav$LocalRosUavId/mavros/estimator_status",
    "/uav$LocalRosUavId/mavros/setpoint_raw/local",
    "/uav$LocalRosUavId/mavros/setpoint_raw/target_local",
    "/uav$LocalRosUavId/prometheus/state",
    "/uav$LocalRosUavId/prometheus/control_state",
    "/uav$LocalRosUavId/prometheus/command",
    "/uav$LocalRosUavId/prometheus/text_info",
    "/uav$LocalRosUavId/competition/task_status",
    # 飞控文字状态包含 failsafe、定位丢失、EKF 和预检失败等原因，
    # 是分析红灯、蜂鸣器和 vision_pose_error 的关键证据。
    "/uav$LocalRosUavId/mavros/statustext/recv",
    "/diagnostics",
    "/diagnostics_agg",
    "/rosout",
    "/rosout_agg"
)

# P600 的 Prometheus 里程计是经过有效性保护后的结果。保留原始 MID360
# 里程计、点云和 IMU，才能区分“传感器本身没有数据”和“Prometheus 拒绝了
# 数据”。SU17 没有这些固定的 Livox 话题，因此不加入 SU17 的采集清单。
$rawP600Topics = @()
if ($modelLower -eq "p600") {
    # 原始 Odometry 体积较小，但它是判断 Prometheus 是否拒绝定位结果的关键对照数据，
    # 因此核心模式也保留；原始点云和 IMU 只在完整模式加入。
    $rawP600Topics = @("/Odometry")
    if ($recordingMode -eq "full") {
        $rawP600Topics += @("/livox/lidar", "/livox/imu")
    }
    $topics += $rawP600Topics
}

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$sshTarget = "${UavUser}@${UavAddress}"
$competitionKey = Join-Path $env:USERPROFILE '.ssh\zhixin_competition_ed25519'
$sshIdentityArgs = @()
if (Test-Path -LiteralPath $competitionKey -PathType Leaf) {
    $sshIdentityArgs = @('-i', $competitionKey, '-o', 'IdentitiesOnly=yes')
}
$topicArgs = $topics -join " "

# --buffsize is MB. Keep it small to protect the companion computer.
# --chunksize is KB. Splitting limits the size of each bag file.
$startInner = "set -e; " +
    "source /opt/ros/noetic/setup.bash; " +
    "source '$VendorWorkspace/devel/setup.bash'; " +
    "mkdir -p '$remoteDir'; " +
    "nohup rosbag record --buffsize=16 --chunksize=768 --split --size=512 " +
    "-O '$remoteBagPrefix' $topicArgs > '$remoteLogFile' 2>&1 & " +
    "pid=`$!; printf '%s\n' `"`$pid`" > '$remotePidFile'; " +
    "disown `"`$pid`" 2>/dev/null || true"

$recordingStarted = $false
$copyAttempted = $false
$lastRemoteExitCode = 0

function Invoke-RemoteBash {
    param([Parameter(Mandatory = $true)][string]$ScriptText)

    # Avoid PowerShell -> OpenSSH -> bash nested-quote corruption by sending
    # the complete UTF-8 shell program as Base64.
    $scriptBytes = [System.Text.Encoding]::UTF8.GetBytes($ScriptText)
    $encodedScript = [Convert]::ToBase64String($scriptBytes)
    $remoteCommand = "printf '%s' '$encodedScript' | base64 -d | bash"
    & ssh @sshIdentityArgs -o BatchMode=yes -o ConnectTimeout=5 $sshTarget $remoteCommand
    $script:lastRemoteExitCode = $LASTEXITCODE
}

function Stop-RemoteRecording {
    param([switch]$Quiet)

    $stopInner = "if [ -f '$remotePidFile' ]; then " +
        "pid=`$(cat '$remotePidFile'); " +
        "if kill -0 `$pid 2>/dev/null; then kill -INT `$pid; fi; " +
        "for i in `$(seq 1 60); do kill -0 `$pid 2>/dev/null || break; sleep 1; done; " +
        "kill -0 `$pid 2>/dev/null && kill -TERM `$pid || true; " +
        "fi"
    if (-not $Quiet) {
        Write-Host "正在停止机载 rosbag 并整理文件..." -ForegroundColor Yellow
    }
    Invoke-RemoteBash -ScriptText $stopInner
    if ($lastRemoteExitCode -ne 0) {
        throw "未能正常停止机载 rosbag，机载端可能仍在记录。"
    }
}

function Copy-RemoteRecording {
    New-Item -ItemType Directory -Force -Path $localDir | Out-Null
    Write-Host "正在将飞行记录复制到地面电脑..." -ForegroundColor Yellow
    & scp @sshIdentityArgs -o BatchMode=yes -o ConnectTimeout=5 -r "${sshTarget}:${remoteDir}/." "$localDir"
    if ($LASTEXITCODE -ne 0) {
        throw "SCP 复制失败，完整记录仍保留在机载端：$remoteDir"
    }
    $script:copyAttempted = $true
}

function Add-ElapsedTimeColumns {
    $program = Join-Path $PSScriptRoot "add_elapsed_time_column.py"
    if (-not (Test-Path -LiteralPath $program)) {
        Write-Warning "缺少相对时间处理器：$program"
        return $false
    }
    if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
        Write-Warning "未找到 Python，未能为 CSV 添加分秒相对时间列。"
        return $false
    }
    & $PythonExe $program --directory $localDir
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "CSV 相对时间列生成失败，原始 CSV 仍已保留。"
        return $false
    }
    return $true
}

function Export-RemoteFlightCsv {
    Write-Host "正在从已完成的 bag 文件提取位置里程计、无人机指令和飞行状态；P600 原始点云 bag 较大，此步骤可能持续数分钟..." -ForegroundColor Yellow
    $commandTopic = "/uav$LocalRosUavId/prometheus/command"
    $rawOdometryExport = ""
    if ($modelLower -eq "p600") {
        # 只导出原始 Odometry；原始点云和 IMU 保留在 bag 中，避免生成巨大的文本文件。
        $rawOdometryExport = "export_topic '/Odometry' '$remoteRawOdometryCsv' || true; "
    }
    $exportInner = "set -e; source /opt/ros/noetic/setup.bash; " +
        "source '$VendorWorkspace/devel/setup.bash'; " +
        "export_topic() { topic=`"`$1`"; output=`"`$2`"; printf '正在提取 CSV：%s\\n' `"`$topic`"; rm -f `"`$output`"; " +
        "first=1; found=0; for bag in '$remoteDir'/*.bag; do " +
        "[ -e `"`$bag`" ] || continue; tmp=`"`$bag.csvpart`"; " +
        "rostopic echo -p -b `"`$bag`" `"`$topic`" > `"`$tmp`" 2>/dev/null || true; " +
        "if [ -s `"`$tmp`" ] && [ `$(wc -l < `"`$tmp`") -gt 1 ]; then found=1; " +
        "if [ `$first -eq 1 ]; then cat `"`$tmp`" > `"`$output`"; first=0; " +
        "else tail -n +2 `"`$tmp`" >> `"`$output`"; fi; fi; " +
        "rm -f `"`$tmp`"; done; [ `$found -eq 1 ] && [ -s `"`$output`" ]; result=`$?; " +
        "if [ `$result -eq 0 ]; then printf 'CSV 提取完成：%s\\n' `"`$output`"; else printf 'CSV 无数据或提取失败：%s\\n' `"`$topic`"; fi; return `$result; }; " +
        "export_topic '$positionTopic' '$remoteOdometryCsv'; " +
        $rawOdometryExport +
        "export_topic '$commandTopic' '$remoteCommandCsv' || true; " +
        "export_topic '/uav$LocalRosUavId/prometheus/state' '$remoteStateCsv' || true; " +
        "export_topic '/uav$LocalRosUavId/prometheus/control_state' '$remoteControlStateCsv' || true; " +
        "export_topic '/uav$LocalRosUavId/mavros/statustext/recv' '$remoteStatusTextCsv' || true; " +
        "export_topic '/uav$LocalRosUavId/competition/task_status' '$remoteTaskStatusCsv' || true; " +
        "export_topic '/uav$LocalRosUavId/prometheus/text_info' '$remotePrometheusTextCsv' || true; " +
        "export_topic '/uav$LocalRosUavId/mavros/battery' '$remoteBatteryCsv' || true; " +
        "export_topic '/uav$LocalRosUavId/mavros/time_reference' '$remoteTimeReferenceCsv' || true; true"
    Invoke-RemoteBash -ScriptText $exportInner
    if ($lastRemoteExitCode -ne 0) {
        Write-Warning "无法提取位置里程计 CSV，bag 文件仍会复制并保留。"
        return $false
    }
    return $true
}

function Test-RemoteBagsAndBuildManifest {
    Write-Host "正在检查已完成的 bag 文件并生成 SHA-256 清单..." -ForegroundColor Yellow
    $checkInner = "set -e; source /opt/ros/noetic/setup.bash; " +
        "source '$VendorWorkspace/devel/setup.bash'; " +
        "cd '$remoteDir'; rm -f 'bag_sha256.txt'; found=0; " +
        "for bag in *.bag; do [ -e `"`$bag`" ] || continue; found=1; " +
        "rosbag info `"`$bag`" >/dev/null 2>&1 || exit 31; " +
        "sha256sum `"`$bag`" >> 'bag_sha256.txt' || exit 32; done; " +
        "[ `$found -eq 1 ] && [ -s 'bag_sha256.txt' ]"
    Invoke-RemoteBash -ScriptText $checkInner
    if ($lastRemoteExitCode -ne 0) {
        Write-Warning "机载 bag 校验失败。文件仍会复制，但已禁止自动删除。"
        return $false
    }
    return $true
}

function Test-GroundBagCopy {
    $manifestPath = Join-Path $localDir "bag_sha256.txt"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        Write-Warning "地面副本没有 SHA-256 清单，已禁止自动删除机载备份。"
        return $false
    }

    $checked = 0
    foreach ($line in Get-Content -LiteralPath $manifestPath) {
        if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') {
            Write-Warning "清单行无效：$line"
            return $false
        }
        $expectedHash = $Matches[1].ToUpperInvariant()
        $bagName = $Matches[2].Trim()
        if ([System.IO.Path]::GetFileName($bagName) -ne $bagName) {
            Write-Warning "清单中的 bag 文件名不安全：$bagName"
            return $false
        }
        $localBag = Join-Path $localDir $bagName
        if (-not (Test-Path -LiteralPath $localBag -PathType Leaf)) {
            Write-Warning "缺少已下载的 bag 文件：$localBag"
            return $false
        }
        $actualHash = (Get-FileHash -LiteralPath $localBag -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actualHash -ne $expectedHash) {
            Write-Warning "SHA-256 不匹配：$bagName"
            return $false
        }
        $checked++
    }

    if ($checked -eq 0) {
        Write-Warning "清单中没有列出 bag 文件。"
        return $false
    }
    Write-Host "bag 校验通过：$checked 个文件，SHA-256 匹配。" -ForegroundColor Green
    return $true
}

function Build-GroundPositionReport {
    $csvPath = Join-Path $localDir "position_odometry.csv"
    $commandCsvPath = Join-Path $localDir "uav_command.csv"
    $stateCsvPath = Join-Path $localDir "uav_state.csv"
    $reportPath = Join-Path $localDir "mid360_position_report.html"

    if (-not (Test-Path -LiteralPath $csvPath)) {
        Write-Warning "位置里程计 CSV 未下载，未生成 HTML 报告。"
        return $false
    }
    if (-not (Test-Path -LiteralPath $reportProgram)) {
        Write-Warning "缺少报告生成器：$reportProgram"
        return $false
    }
    if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
        Write-Warning "未找到 Python：$PythonExe。bag 和 CSV 文件仍然可用。"
        return $false
    }

    Write-Host "正在生成交互式 XYZ 位置报告..." -ForegroundColor Yellow
    $reportArgs = @($reportProgram, "--input", $csvPath, "--output-dir", $localDir, "--source", "mid360")
    if (Test-Path -LiteralPath $commandCsvPath) {
        $reportArgs += @("--command-input", $commandCsvPath)
    }
    else {
        Write-Warning "未采集到无人机控制指令 CSV；图中的零指令线仅为占位，不代表真实指令。"
    }
    if (Test-Path -LiteralPath $stateCsvPath) {
        $reportArgs += @("--state-input", $stateCsvPath)
    }
    else {
        Write-Warning "缺少飞行状态 CSV，实际位置和速度曲线将回退到位置里程计。"
    }
    & $PythonExe @reportArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $reportPath)) {
        Write-Warning "位置报告生成失败。bag 和 CSV 文件仍然可用。"
        return $false
    }

    Write-Host "HTML 报告：$reportPath" -ForegroundColor Green
    if ($OpenReport) {
        Start-Process -FilePath $reportPath
    }
    return $true
}

function Request-OnboardBackupDeletion {
    $expectedPrefix = "/home/amov/flight_records/uav${UavId}_"
    if (-not $remoteDir.StartsWith($expectedPrefix, [System.StringComparison]::Ordinal)) {
        Write-Warning "拒绝删除：远程路径不在预期飞行记录目录内：$remoteDir"
        return
    }

    Write-Host ""
    Write-Host "地面 bag 校验和 HTML 报告生成均已成功。" -ForegroundColor Green
    Write-Host "机载备份：$remoteDir" -ForegroundColor Yellow
    $answer = Read-Host "输入 DELETE 删除机载备份；直接回车保留"
    if ($answer -cne "DELETE") {
        Write-Host "已保留机载备份：$remoteDir"
        return
    }

    $deleteInner = "target='$remoteDir'; resolved=`$(realpath -e -- `"`$target`" 2>/dev/null) || exit 41; " +
        "[ `"`$resolved`" = '$remoteDir' ] || exit 42; " +
        "case `"`$resolved`" in '$expectedPrefix'*) rm -rf -- `"`$resolved`" ;; *) exit 43 ;; esac; " +
        "[ ! -e '$remoteDir' ]"
    Invoke-RemoteBash -ScriptText $deleteInner
    if ($lastRemoteExitCode -ne 0) {
        Write-Warning "机载备份删除失败或未通过路径安全检查，备份仍保留在：$remoteDir"
        return
    }
    Write-Host "已删除机载备份：$remoteDir" -ForegroundColor Green
}

try {
    Write-Host "正在启动低内存机载飞行记录..." -ForegroundColor Cyan
    Write-Host "机型：     $modelLower"
    Write-Host "ROS 编号：  $LocalRosUavId"
    Write-Host "位置话题：  $positionTopic"
    Write-Host "工作空间：  $VendorWorkspace"
    Write-Host "无人机：    $sshTarget"
    Write-Host "机载目录：  $remoteDir"
    Write-Host "地面目录：  $localDir"
    Write-Host "采集模式： $recordingMode"
    if ($modelLower -eq "p600" -and $recordingMode -eq "full") {
        Write-Host "话题数量：  $($topics.Count) 个（完整数据，含飞控诊断、原始 MID360 里程计/点云/IMU，不含图像）"
        Write-Host "注意：完整模式的 P600 原始点云会明显增大 bag 文件，测试结束后请等待脚本完成校验和复制。" -ForegroundColor Yellow
    }
    elseif ($modelLower -eq "p600") {
        Write-Host "话题数量：  $($topics.Count) 个（核心数据，含原始 MID360 里程计、定位、控制状态、飞控文字状态、任务状态和诊断，不含原始点云/IMU/图像）"
    }
    else {
        Write-Host "话题数量：  $($topics.Count) 个（$recordingMode 数据，含飞控诊断和状态文字，不含图像和点云）"
    }

    Invoke-RemoteBash -ScriptText $startInner
    if ($lastRemoteExitCode -ne 0) {
        throw "无法在无人机上启动 rosbag。"
    }
    $recordingStarted = $true

    Start-Sleep -Seconds 2
    $checkInner = "pid=`$(cat '$remotePidFile' 2>/dev/null) && kill -0 `$pid 2>/dev/null && echo RECORDING"
    $checkResult = @(Invoke-RemoteBash -ScriptText $checkInner)
    if ($lastRemoteExitCode -ne 0 -or $checkResult -notcontains "RECORDING") {
        throw "rosbag 未能保持运行，请检查无人机上的日志：$remoteLogFile"
    }

    Write-Host "正在记录，可以开始飞行。" -ForegroundColor Green
    if ($NonInteractive) {
        if ([string]::IsNullOrWhiteSpace($StopSignalPath)) {
            throw "非交互模式必须提供 StopSignalPath。"
        }
        Write-Host "记录由地面网页控制。" -ForegroundColor Green
        while (-not (Test-Path -LiteralPath $StopSignalPath)) {
            Start-Sleep -Seconds 1
        }
    }
    else {
        Write-Host "降落并解除锁定后返回此窗口，按回车结束记录。" -ForegroundColor Green
        [void](Read-Host)
    }

    Stop-RemoteRecording
    $recordingStarted = $false
    $remoteBagsValid = Test-RemoteBagsAndBuildManifest
    $odometryExported = Export-RemoteFlightCsv
    Copy-RemoteRecording
    Add-ElapsedTimeColumns | Out-Null

    $bagFiles = Get-ChildItem -Path $localDir -Filter "*.bag" -File -ErrorAction SilentlyContinue
    if (-not $bagFiles) {
        throw "没有下载到已完成的 .bag 文件，请检查日志：$remoteLogFile"
    }

    $totalBytes = ($bagFiles | Measure-Object -Property Length -Sum).Sum
    Write-Host "飞行记录完成。" -ForegroundColor Green
    Write-Host "bag 文件数：$($bagFiles.Count)"
    Write-Host ("总大小：{0:N1} MB" -f ($totalBytes / 1MB))
    Write-Host "保存位置：$localDir"
    Write-Host "机载备份仍保留在：$remoteDir"
    $groundBagsValid = $false
    if ($remoteBagsValid) {
        $groundBagsValid = Test-GroundBagCopy
    }
    $reportBuilt = $false
    if ($odometryExported) {
        $reportBuilt = Build-GroundPositionReport
    }
    if ($NonInteractive -and $remoteBagsValid -and $groundBagsValid -and $reportBuilt) {
        Write-Host "网页录制已完成；机载备份仍保留，可在网页中明确删除。"
    }
    elseif ($remoteBagsValid -and $groundBagsValid -and $reportBuilt) {
        Request-OnboardBackupDeletion
    }
    else {
        Write-Warning "由于 bag 校验、地面哈希校验或 HTML 生成未成功，机载备份将保留。"
    }
}
finally {
    if ($recordingStarted) {
        try {
            Stop-RemoteRecording -Quiet
            $recordingStarted = $false
        }
        catch {
            Write-Warning $_.Exception.Message
        }
    }

    if (-not $copyAttempted -and (Test-Path $OutputRoot)) {
        Write-Host "如果下载未完成，请手动复制：" -ForegroundColor Yellow
        Write-Host "scp -r ${sshTarget}:${remoteDir} `"$OutputRoot`""
    }
}
