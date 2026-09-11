[CmdletBinding()]
param(
    [string]$UavAddress = "192.168.1.88",
    [string]$RemoteUser = "amov",
    [ValidateSet("vision", "mid360")]
    [string]$Source = "vision",
    [string]$Topic = "",
    [string]$OutputRoot = "",
    [string]$PythonExe = "python",
    [switch]$CheckOnly,
    [switch]$OpenReport
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 may not populate $PSScriptRoot while evaluating
# parameter default expressions, so resolve the default after parameter binding.
if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $projectRoot = Split-Path -Path $PSScriptRoot -Parent
    $OutputRoot = Join-Path -Path $projectRoot -ChildPath "position_tests"
}
if ([string]::IsNullOrWhiteSpace($Topic)) {
    switch ($Source) {
        "vision" { $Topic = "/BSAslam/odometry" }
        "mid360" { $Topic = "/Odometry" }
    }
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw "未找到 ssh。请先在 Windows 可选功能中安装 OpenSSH Client。"
}
if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
    throw "未找到 Python：$PythonExe。请安装 Python 3，或用 -PythonExe 指定 python.exe。"
}

$recorderProgram = Join-Path $PSScriptRoot "record_bsa_position.py"
$reportProgram = Join-Path $PSScriptRoot "bsa_position_report.py"
if (-not (Test-Path -LiteralPath $recorderProgram)) {
    throw "缺少采集程序：$recorderProgram"
}
if (-not (Test-Path -LiteralPath $reportProgram)) {
    throw "缺少报表程序：$reportProgram"
}

if ($CheckOnly) {
    Write-Host "脚本检查通过。"
    Write-Host "项目输出目录：$OutputRoot"
    Write-Host "无人机：$RemoteUser@$UavAddress"
    Write-Host "定位源：$Source"
    Write-Host "话题：$Topic"
    return
}

$recorderArguments = @(
    $recorderProgram,
    "--uav-address", $UavAddress,
    "--remote-user", $RemoteUser,
    "--source", $Source,
    "--topic", $Topic,
    "--output-root", $OutputRoot,
    "--report-program", $reportProgram
)
if ($OpenReport) {
    $recorderArguments += "--open-report"
}

& $PythonExe @recorderArguments
if ($LASTEXITCODE -ne 0) {
    throw "采集或报表程序执行失败，退出码：$LASTEXITCODE"
}
