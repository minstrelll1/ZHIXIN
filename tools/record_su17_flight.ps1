param(
    [string]$UavAddress = "192.168.1.88",
    [string]$UavUser = "amov",
    [int]$UavId = 1,
    [string]$OutputRoot = "",
    [string]$PythonExe = "python",
    [switch]$OpenReport,
    [string]$FlightName = "",
    [switch]$NonInteractive,
    [string]$StopSignalPath = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $OutputRoot = Join-Path $projectRoot "flight_records"
}

if ([string]::IsNullOrWhiteSpace($FlightName)) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $FlightName = "uav{0}_{1}" -f $UavId, $timestamp
}
if ($FlightName -notmatch "^uav$UavId`_\d{8}_\d{6}$") {
    throw "Unsafe or invalid flight name: $FlightName"
}
$flightName = $FlightName
$remoteRoot = "/home/amov/flight_records"
$remoteDir = "$remoteRoot/$flightName"
$localDir = Join-Path $OutputRoot $flightName
$remotePidFile = "$remoteDir/rosbag.pid"
$remoteLogFile = "$remoteDir/rosbag_record.log"
$remoteBagPrefix = "$remoteDir/$flightName"
$remoteOdometryCsv = "$remoteDir/mid360_odometry.csv"
$remoteCommandCsv = "$remoteDir/uav_command.csv"
$remoteStateCsv = "$remoteDir/uav_state.csv"
$remoteBagManifest = "$remoteDir/bag_sha256.txt"
$reportProgram = Join-Path $PSScriptRoot "bsa_position_report.py"

$topics = @(
    "/Odometry",
    "/uav$UavId/mavros/vision_pose/pose",
    "/uav$UavId/mavros/local_position/pose",
    "/uav$UavId/mavros/local_position/velocity_local",
    "/uav$UavId/mavros/imu/data",
    "/uav$UavId/mavros/rc/in",
    "/uav$UavId/mavros/state",
    "/uav$UavId/mavros/extended_state",
    "/uav$UavId/mavros/estimator_status",
    "/uav$UavId/mavros/setpoint_raw/local",
    "/uav$UavId/mavros/setpoint_raw/target_local",
    "/uav$UavId/prometheus/state",
    "/uav$UavId/prometheus/control_state",
    "/uav$UavId/prometheus/command",
    "/rosout",
    "/rosout_agg"
)

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$sshTarget = "${UavUser}@${UavAddress}"
$topicArgs = $topics -join " "

# --buffsize is MB. Keep it small to protect the companion computer.
# --chunksize is KB. Splitting limits the size of each bag file.
$startInner = "set -e; " +
    "source /opt/ros/noetic/setup.bash; " +
    "source /home/amov/su17_experiment/devel/setup.bash; " +
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
    & ssh -o BatchMode=yes -o ConnectTimeout=5 $sshTarget $remoteCommand
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
        Write-Host "Stopping onboard rosbag and finalizing files..." -ForegroundColor Yellow
    }
    Invoke-RemoteBash -ScriptText $stopInner
    if ($lastRemoteExitCode -ne 0) {
        throw "Failed to stop onboard rosbag cleanly. The onboard copy may still be recording."
    }
}

function Copy-RemoteRecording {
    New-Item -ItemType Directory -Force -Path $localDir | Out-Null
    Write-Host "Copying flight log to ground computer..." -ForegroundColor Yellow
    & scp -o BatchMode=yes -o ConnectTimeout=5 -r "${sshTarget}:${remoteDir}/." "$localDir"
    if ($LASTEXITCODE -ne 0) {
        throw "SCP failed. The complete recording remains onboard at $remoteDir"
    }
    $script:copyAttempted = $true
}

function Export-RemoteFlightCsv {
    Write-Host "Extracting MID360 odometry, UAV commands, and UAV state from finalized bag files..." -ForegroundColor Yellow
    $commandTopic = "/uav$UavId/prometheus/command"
    $exportInner = "set -e; source /opt/ros/noetic/setup.bash; " +
        "source /home/amov/su17_experiment/devel/setup.bash; " +
        "export_topic() { topic=`"`$1`"; output=`"`$2`"; rm -f `"`$output`"; " +
        "first=1; found=0; for bag in '$remoteDir'/*.bag; do " +
        "[ -e `"`$bag`" ] || continue; tmp=`"`$bag.csvpart`"; " +
        "rostopic echo -p -b `"`$bag`" `"`$topic`" > `"`$tmp`" 2>/dev/null || true; " +
        "if [ -s `"`$tmp`" ] && [ `$(wc -l < `"`$tmp`") -gt 1 ]; then found=1; " +
        "if [ `$first -eq 1 ]; then cat `"`$tmp`" > `"`$output`"; first=0; " +
        "else tail -n +2 `"`$tmp`" >> `"`$output`"; fi; fi; " +
        "rm -f `"`$tmp`"; done; [ `$found -eq 1 ] && [ -s `"`$output`" ]; }; " +
        "export_topic '/Odometry' '$remoteOdometryCsv'; " +
        "export_topic '$commandTopic' '$remoteCommandCsv' || true; " +
        "export_topic '/uav$UavId/prometheus/state' '$remoteStateCsv' || true; true"
    Invoke-RemoteBash -ScriptText $exportInner
    if ($lastRemoteExitCode -ne 0) {
        Write-Warning "Could not extract the MID360 odometry CSV. Bag files will still be copied and retained."
        return $false
    }
    return $true
}

function Test-RemoteBagsAndBuildManifest {
    Write-Host "Checking finalized bag files and building SHA-256 manifest..." -ForegroundColor Yellow
    $checkInner = "set -e; source /opt/ros/noetic/setup.bash; " +
        "source /home/amov/su17_experiment/devel/setup.bash; " +
        "cd '$remoteDir'; rm -f 'bag_sha256.txt'; found=0; " +
        "for bag in *.bag; do [ -e `"`$bag`" ] || continue; found=1; " +
        "rosbag info `"`$bag`" >/dev/null 2>&1 || exit 31; " +
        "sha256sum `"`$bag`" >> 'bag_sha256.txt' || exit 32; done; " +
        "[ `$found -eq 1 ] && [ -s 'bag_sha256.txt' ]"
    Invoke-RemoteBash -ScriptText $checkInner
    if ($lastRemoteExitCode -ne 0) {
        Write-Warning "Onboard bag validation failed. Files will be copied, but automatic deletion is disabled."
        return $false
    }
    return $true
}

function Test-GroundBagCopy {
    $manifestPath = Join-Path $localDir "bag_sha256.txt"
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        Write-Warning "Ground copy has no SHA-256 manifest. Automatic deletion is disabled."
        return $false
    }

    $checked = 0
    foreach ($line in Get-Content -LiteralPath $manifestPath) {
        if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') {
            Write-Warning "Invalid manifest line: $line"
            return $false
        }
        $expectedHash = $Matches[1].ToUpperInvariant()
        $bagName = $Matches[2].Trim()
        if ([System.IO.Path]::GetFileName($bagName) -ne $bagName) {
            Write-Warning "Unsafe bag name in manifest: $bagName"
            return $false
        }
        $localBag = Join-Path $localDir $bagName
        if (-not (Test-Path -LiteralPath $localBag -PathType Leaf)) {
            Write-Warning "Downloaded bag is missing: $localBag"
            return $false
        }
        $actualHash = (Get-FileHash -LiteralPath $localBag -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actualHash -ne $expectedHash) {
            Write-Warning "SHA-256 mismatch: $bagName"
            return $false
        }
        $checked++
    }

    if ($checked -eq 0) {
        Write-Warning "No bag file was listed in the manifest."
        return $false
    }
    Write-Host "Bag validation passed: $checked file(s), SHA-256 matched." -ForegroundColor Green
    return $true
}

function Build-GroundPositionReport {
    $csvPath = Join-Path $localDir "mid360_odometry.csv"
    $commandCsvPath = Join-Path $localDir "uav_command.csv"
    $stateCsvPath = Join-Path $localDir "uav_state.csv"
    $reportPath = Join-Path $localDir "mid360_position_report.html"

    if (-not (Test-Path -LiteralPath $csvPath)) {
        Write-Warning "MID360 CSV was not downloaded; HTML report was not generated."
        return $false
    }
    if (-not (Test-Path -LiteralPath $reportProgram)) {
        Write-Warning "Report generator is missing: $reportProgram"
        return $false
    }
    if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
        Write-Warning "Python was not found: $PythonExe. Bag and CSV files are still available."
        return $false
    }

    Write-Host "Generating interactive MID360 XYZ HTML report..." -ForegroundColor Yellow
    $reportArgs = @($reportProgram, "--input", $csvPath, "--output-dir", $localDir, "--source", "mid360")
    if (Test-Path -LiteralPath $commandCsvPath) {
        $reportArgs += @("--command-input", $commandCsvPath)
    }
    else {
        Write-Warning "UAV command CSV is missing; command curves will be shown as zero."
    }
    if (Test-Path -LiteralPath $stateCsvPath) {
        $reportArgs += @("--state-input", $stateCsvPath)
    }
    else {
        Write-Warning "UAV state CSV is missing; actual comparison curves will fall back to /Odometry."
    }
    & $PythonExe @reportArgs
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $reportPath)) {
        Write-Warning "Position report generation failed. Bag and CSV files are still available."
        return $false
    }

    Write-Host "HTML report: $reportPath" -ForegroundColor Green
    if ($OpenReport) {
        Start-Process -FilePath $reportPath
    }
    return $true
}

function Request-OnboardBackupDeletion {
    $expectedPrefix = "/home/amov/flight_records/uav${UavId}_"
    if (-not $remoteDir.StartsWith($expectedPrefix, [System.StringComparison]::Ordinal)) {
        Write-Warning "Refusing deletion because the remote path is outside the expected flight directory: $remoteDir"
        return
    }

    Write-Host ""
    Write-Host "Ground bag verification and HTML report generation both succeeded." -ForegroundColor Green
    Write-Host "Onboard backup: $remoteDir" -ForegroundColor Yellow
    $answer = Read-Host "Type DELETE to remove this onboard backup; press Enter to keep it"
    if ($answer -cne "DELETE") {
        Write-Host "Onboard backup retained: $remoteDir"
        return
    }

    $deleteInner = "target='$remoteDir'; resolved=`$(realpath -e -- `"`$target`" 2>/dev/null) || exit 41; " +
        "[ `"`$resolved`" = '$remoteDir' ] || exit 42; " +
        "case `"`$resolved`" in '$expectedPrefix'*) rm -rf -- `"`$resolved`" ;; *) exit 43 ;; esac; " +
        "[ ! -e '$remoteDir' ]"
    Invoke-RemoteBash -ScriptText $deleteInner
    if ($lastRemoteExitCode -ne 0) {
        Write-Warning "Onboard deletion failed or was refused by the path safety check. Backup remains at $remoteDir"
        return
    }
    Write-Host "Onboard backup deleted: $remoteDir" -ForegroundColor Green
}

try {
    Write-Host "Starting low-memory onboard flight recording..." -ForegroundColor Cyan
    Write-Host "UAV:       $sshTarget"
    Write-Host "Onboard:   $remoteDir"
    Write-Host "Ground:    $localDir"
    Write-Host "Topics:    $($topics.Count) diagnostic topics (no image or point cloud)"

    Invoke-RemoteBash -ScriptText $startInner
    if ($lastRemoteExitCode -ne 0) {
        throw "Unable to start rosbag on the UAV."
    }
    $recordingStarted = $true

    Start-Sleep -Seconds 2
    $checkInner = "pid=`$(cat '$remotePidFile' 2>/dev/null) && kill -0 `$pid 2>/dev/null && echo RECORDING"
    $checkResult = @(Invoke-RemoteBash -ScriptText $checkInner)
    if ($lastRemoteExitCode -ne 0 -or $checkResult -notcontains "RECORDING") {
        throw "Rosbag did not remain running. Check $remoteLogFile on the UAV."
    }

    Write-Host "RECORDING. You may now perform the flight." -ForegroundColor Green
    if ($NonInteractive) {
        if ([string]::IsNullOrWhiteSpace($StopSignalPath)) {
            throw "StopSignalPath is required in non-interactive mode."
        }
        Write-Host "Recording is controlled by the ground web interface." -ForegroundColor Green
        while (-not (Test-Path -LiteralPath $StopSignalPath)) {
            Start-Sleep -Seconds 1
        }
    }
    else {
        Write-Host "After landing and disarming, return here and press Enter." -ForegroundColor Green
        [void](Read-Host)
    }

    Stop-RemoteRecording
    $recordingStarted = $false
    $remoteBagsValid = Test-RemoteBagsAndBuildManifest
    $odometryExported = Export-RemoteFlightCsv
    Copy-RemoteRecording

    $bagFiles = Get-ChildItem -Path $localDir -Filter "*.bag" -File -ErrorAction SilentlyContinue
    if (-not $bagFiles) {
        throw "No finalized .bag file was downloaded. Check $remoteLogFile"
    }

    $totalBytes = ($bagFiles | Measure-Object -Property Length -Sum).Sum
    Write-Host "Flight recording complete." -ForegroundColor Green
    Write-Host "Bag files:  $($bagFiles.Count)"
    Write-Host ("Total size: {0:N1} MB" -f ($totalBytes / 1MB))
    Write-Host "Saved at:   $localDir"
    Write-Host "Onboard backup retained at: $remoteDir"
    $groundBagsValid = $false
    if ($remoteBagsValid) {
        $groundBagsValid = Test-GroundBagCopy
    }
    $reportBuilt = $false
    if ($odometryExported) {
        $reportBuilt = Build-GroundPositionReport
    }
    if ($NonInteractive -and $remoteBagsValid -and $groundBagsValid -and $reportBuilt) {
        Write-Host "Web recording completed; onboard backup retained for explicit deletion in the web interface."
    }
    elseif ($remoteBagsValid -and $groundBagsValid -and $reportBuilt) {
        Request-OnboardBackupDeletion
    }
    else {
        Write-Warning "Onboard backup will be retained because bag validation, ground hash verification, or HTML generation did not succeed."
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
        Write-Host "If download did not complete, manually copy:" -ForegroundColor Yellow
        Write-Host "scp -r ${sshTarget}:${remoteDir} `"$OutputRoot`""
    }
}
