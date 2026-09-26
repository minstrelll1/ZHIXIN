param(
    [Parameter(Mandatory = $true)]
    [string]$UavAddress,

    [string]$UavUser = "amov",
    [string]$RemoteWorkspace = "/home/amov/competition_development"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LocalPackage = Join-Path $ProjectRoot "src\su17_image_transfer"
$Remote = "${UavUser}@${UavAddress}"

if (-not (Test-Path -LiteralPath $LocalPackage)) {
    throw "Package directory not found: $LocalPackage"
}

Write-Host "Creating remote workspace on $Remote"
ssh $Remote "mkdir -p '$RemoteWorkspace/src'"
if ($LASTEXITCODE -ne 0) {
    throw "Could not create the remote workspace."
}

Write-Host "Copying su17_image_transfer to $RemoteWorkspace/src"
scp -r $LocalPackage "${Remote}:$RemoteWorkspace/src/"
if ($LASTEXITCODE -ne 0) {
    throw "SCP upload failed."
}

Write-Host "Upload complete. Build the package in the onboard SSH terminal."

