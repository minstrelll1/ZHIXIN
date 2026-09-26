param(
    [string]$UavAddress = "192.168.1.88",
    [string]$UavUser = "amov"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$Source = Join-Path $ProjectRoot "src\su17_competition_executor"
$RemoteWorkspace = "/home/amov/competition_development"
$Remote = "$UavUser@$UavAddress"

if (-not (Test-Path -LiteralPath $Source)) {
    throw "Package source not found: $Source"
}

Write-Host "Uploading su17_competition_executor to $Remote..."
& ssh $Remote "mkdir -p $RemoteWorkspace/src"
if ($LASTEXITCODE -ne 0) { throw "Unable to prepare remote workspace." }

& scp -r $Source "${Remote}:$RemoteWorkspace/src/"
if ($LASTEXITCODE -ne 0) { throw "Upload failed." }

$BuildCommand = "cd $RemoteWorkspace && source /opt/ros/noetic/setup.bash && source /home/amov/su17_experiment/devel/setup.bash && catkin_make --only-pkg-with-deps su17_competition_executor"
& ssh $Remote $BuildCommand
if ($LASTEXITCODE -ne 0) { throw "Remote catkin build failed." }

Write-Host "Deployment and build completed." -ForegroundColor Green
Write-Host "Start in safe ACK-only mode:"
Write-Host "  roslaunch su17_competition_executor onboard_task_executor.launch uav_id:=1 enable_motion:=false"
