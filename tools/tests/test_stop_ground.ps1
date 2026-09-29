$ErrorActionPreference='Stop'
$root=Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
foreach($name in @('tools\stop_ground.ps1','tools\start_ground.ps1')) {
    $tokens=$null; $parseErrors=$null
    $null=[Management.Automation.Language.Parser]::ParseFile((Join-Path $root $name),[ref]$tokens,[ref]$parseErrors)
    if($parseErrors.Count) { throw ($parseErrors | Out-String) }
}
$source=[IO.File]::ReadAllText((Join-Path $root 'tools\stop_ground.ps1'))
# 所有进程和 HTTP 操作替换为内存模拟；不关闭真实程序，不访问无人机。
$source=$source.Replace('$ProjectRoot = (Split-Path $PSScriptRoot -Parent).TrimEnd(''\'',''/'')', '$ProjectRoot = ''C:\isolated\competition_development''')
$begin=$source.IndexOf('function Report(');$end=$source.IndexOf('function Same-Process(')
$source=$source.Substring(0,$begin)+'function Report([string]$Text) { $global:Messages.Add($Text) }'+"`n"+$source.Substring($end)
$source=$source.Replace('    exit 0','    return 0').Replace('    exit 1','    return 1')
function New-Item { param($ItemType,[switch]$Force,$Path) }
function Get-CimInstance {
    param($ClassName,$Filter,$ErrorAction)
    if($Filter) { return $global:Processes[[int]($Filter -replace 'ProcessId=','')] }
    return @($global:Processes.Values)
}
function Invoke-CimMethod {
    param($InputObject,$MethodName)
    $global:Killed.Add([int]$InputObject.ProcessId)
    if(-not $global:Refuse) { $global:Processes.Remove([int]$InputObject.ProcessId) }
    return @{ReturnValue=0}
}
function Invoke-RestMethod { param($Method,$Uri,$ContentType,$Body,$TimeoutSec) throw 'isolated test fallback' }
function Get-NetTCPConnection { param($LocalPort,$State,$ErrorAction) return @() }
function Get-Date { param($Format) return $global:Now }
function Start-Sleep { param($Milliseconds) $global:Now=$global:Now.AddSeconds(30) }
function Setup {
    $global:Now=[datetime]'2026-01-01'
    $global:Killed=New-Object 'Collections.Generic.List[int]'
    $global:Messages=New-Object 'Collections.Generic.List[string]'
    $global:Refuse=$false
    $global:Processes=@{}
    foreach($row in @(
      @(901,0,'python.exe','C:\isolated\competition_development\competition_backend\.venv\Scripts\python.exe -m competition_backend.ground_entry'),
      @(902,901,'ssh.exe','ssh -T host python3 -'),
      @(903,0,'mediamtx.exe','mediamtx.exe C:\isolated\competition_development\ground_runtime\mediamtx_uav1.yml'),
      @(904,0,'python.exe','C:\isolated\competition_development_old\python.exe -m competition_backend.ground_entry'),
      @(905,0,'mediamtx.exe','mediamtx.exe C:\other\mediamtx.yml')
    )) { $global:Processes[[int]$row[0]]=[pscustomobject]@{ProcessId=$row[0];ParentProcessId=$row[1];Name=$row[2];CommandLine=$row[3];CreationDate=[datetime]'2026-01-01'} }
}
Setup
$result=& ([scriptblock]::Create($source)) -Quiet
if($result -ne 0 -or ($global:Killed | Sort-Object) -join ',' -ne '901,902,903') { throw '未按项目身份清理后端、子进程和视频服务' }
if($global:Processes.Count -ne 2) { throw '误杀了其他项目进程' }
Setup
$result=& ([scriptblock]::Create($source)) -BackendPid 904 -Quiet
if($result -ne 1 -or $global:Killed.Count -ne 0) { throw '错误 PID 未拒绝' }
Setup
$global:Refuse=$true
$result=& ([scriptblock]::Create($source)) -Quiet
if($result -ne 1) { throw '残留进程被误报为已退出' }
Write-Host 'Windows 关闭脚本：语法、项目识别、子进程清理、拒绝误杀及残留检测通过。'
