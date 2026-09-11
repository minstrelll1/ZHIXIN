param(
    [Parameter(Mandatory = $true)][string]$UavAddress,
    [string]$UavUser = "amov",
    [string]$RemoteWorkspace = "/home/amov/competition_development"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$ExecutorPackage = Join-Path $ProjectRoot "src\su17_competition_executor"
$ImagePackage = Join-Path $ProjectRoot "src\su17_image_transfer"
$StartScript = Join-Path $PSScriptRoot "start_onboard_stack.sh"
$Remote = "${UavUser}@${UavAddress}"

foreach ($Path in @($ExecutorPackage, $ImagePackage, $StartScript)) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Deployment source not found: $Path" }
}

& ssh $Remote "mkdir -p '$RemoteWorkspace/src' '$RemoteWorkspace/tools'"
if ($LASTEXITCODE -ne 0) { throw "Unable to prepare onboard workspace." }

& scp -r $ExecutorPackage "${Remote}:$RemoteWorkspace/src/"
if ($LASTEXITCODE -ne 0) { throw "Executor upload failed." }
& scp -r $ImagePackage "${Remote}:$RemoteWorkspace/src/"
if ($LASTEXITCODE -ne 0) { throw "Image-transfer upload failed." }
& scp $StartScript "${Remote}:$RemoteWorkspace/tools/start_onboard_stack.sh"
if ($LASTEXITCODE -ne 0) { throw "Onboard start-script upload failed." }

$BuildCommand = "cd '$RemoteWorkspace' && source /opt/ros/noetic/setup.bash && source /home/amov/su17_experiment/devel/setup.bash && catkin_make --only-pkg-with-deps su17_competition_executor su17_image_transfer"
& ssh $Remote $BuildCommand
if ($LASTEXITCODE -ne 0) { throw "Onboard catkin build failed." }

Write-Host "Onboard task, image, and one-command launcher deployed." -ForegroundColor Green
