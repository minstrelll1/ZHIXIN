$ErrorActionPreference = 'Stop'
$project = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
. (Join-Path $project 'tools\deploy_transport.ps1')
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('zhixin_deploy_test_' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
function Assert-Deploy([bool]$Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
$log = Join-Path $temporary 'probe.log'
try {
    # 本地子进程验证参数、双管道、失败及总超时；不连接无人机。
    $native = Join-Path $temporary '模拟 probe.exe'
    Add-Type -OutputAssembly $native -OutputType ConsoleApplication -TypeDefinition @'
using System;
using System.Text;
using System.Threading;
using System.Diagnostics;
using System.IO;
public class DeployProbeFixture {
    public static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        if (args[0] == "hang") {
            File.WriteAllText(args[1], Process.GetCurrentProcess().Id.ToString());
            Console.Error.WriteLine("probe blocked");
            Thread.Sleep(60000); return 0;
        }
        if (args[0] == "fail") {
            Console.Error.WriteLine("Permission denied (publickey)."); return 255;
        }
        if (args[0] == "flood") {
            for (int i=0; i<2000; i++) {
                Console.Out.WriteLine(new string('x', 100));
                Console.Error.WriteLine(new string('e', 100));
            }
            Console.WriteLine("双管道完成"); return 0;
        }
        for (int i=1; i<args.Length; i++)
            Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(args[i])));
        return 0;
    }
}
'@
    $arguments = @('space and 中文', '', 'quote"inside', 'C:\path with space\', 'a\\\"b', 'a&b;$(noop)', "mkdir -p '/home/amov/competition_development/config'")
    $r = Invoke-OnboardDeployProbe $native (@('args') + $arguments) $log
    $actual = @($r.Output.Replace("`r", '').Split("`n") | Select-Object -SkipLast 1)
    Assert-Deploy ($actual.Count -eq $arguments.Count) '参数数量不匹配'
    for ($i=0; $i -lt $arguments.Count; $i++) {
        Assert-Deploy ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($actual[$i])) -ceq $arguments[$i]) "参数未原样传递：$i"
    }
    $r = Invoke-OnboardDeployProbe $native @('flood') $log
    Assert-Deploy ($r.Output.Contains('双管道完成') -and $r.ErrorOutput.Length -gt 200000) '双管道输出未完整收集'
    try { $null = Invoke-OnboardDeployProbe $native @('fail') $log; throw '失败退出应中止' }
    catch { Assert-Deploy ($_.Exception.Message.Contains('255') -and $_.Exception.Message.Contains('Permission denied')) '未报告真实失败' }
    $pidFile = Join-Path $temporary 'probe.pid'
    $timer = [Diagnostics.Stopwatch]::StartNew()
    try { $null = Invoke-OnboardDeployProbe $native @('hang', $pidFile) $log -TimeoutSeconds 1; throw '未执行超时中止' }
    catch { Assert-Deploy ($_.Exception.Message.Contains('超过 1 秒')) '未报告整体超时' }
    Assert-Deploy ($timer.Elapsed.TotalSeconds -lt 6) '超时后仍等待过久'
    $childPid = [int]([IO.File]::ReadAllText($pidFile))
    Assert-Deploy ($null -eq (Get-Process -Id $childPid -ErrorAction SilentlyContinue)) '超时检查子进程仍存活'
    $logged = [IO.File]::ReadAllText($log)
    Assert-Deploy ($logged.Contains('probe blocked') -and $logged.Contains('[TIMEOUT]') -and $logged.Contains('Permission denied')) '诊断日志缺少错误信息'

    # 隔离部署入口：SSH/SCP/授权均使用桩，不访问用户密钥或网络。
    $fixture = Join-Path $temporary 'project'
    New-Item -ItemType Directory -Force -Path (Join-Path $fixture 'tools'), (Join-Path $fixture 'config') | Out-Null
    Copy-Item -LiteralPath (Join-Path $project 'tools\deploy_onboard_stack.ps1') -Destination (Join-Path $fixture 'tools')
    [IO.File]::WriteAllText((Join-Path $fixture 'config\fleet.json'), '{}')
    [IO.File]::WriteAllText((Join-Path $fixture 'tools\setup_onboard_ssh.ps1'), 'param($UavAddress, $UavUser)')
    $fakeTransport = @'
function Write-OnboardDeployLog { param($LogPath,$Message) }
function Invoke-OnboardDeployProbe {
    param($FilePath,$ArgumentList,$LogPath)
    $global:DeployFixtureProbes += ,$ArgumentList
    if ($global:DeployFixtureProbeFails) { throw 'simulated directory probe failed' }
    $command = $ArgumentList[-1]
    if ($global:DeployFixtureNoMarker) { return [pscustomobject]@{Output='login banner only'} }
    $marker = if ($command.Contains('test -f')) {
        if ($global:DeployFixtureExisting) { 'ZHIXIN_FLEET_EXISTS' } else { 'ZHIXIN_FLEET_MISSING' }
    } else { 'ZHIXIN_DEPLOY_READY' }
    return [pscustomobject]@{Output=("login banner`n" + $marker + "`n")}
}
'@
    [IO.File]::WriteAllText((Join-Path $fixture 'tools\deploy_transport.ps1'), $fakeTransport)
    function scp {
        $global:DeployFixtureUploads += ,$args
        $global:LASTEXITCODE = 0
    }
    function ssh {
        Assert-Deploy ($args -contains 'BatchMode=yes') '授权后仍可弹出密码等待'
        Assert-Deploy ([string]$args[-1] -match 'build_onboard.sh') '不应额外开 SSH 连接检查配置'
        $global:DeployFixtureBuilds++
        $global:LASTEXITCODE = 0
    }
    $deploy = Join-Path $fixture 'tools\deploy_onboard_stack.ps1'
    foreach ($case in @('sync', 'preserve', 'missing', 'failure', 'no_marker')) {
        $global:DeployFixtureProbes = @(); $global:DeployFixtureUploads = @(); $global:DeployFixtureBuilds = 0
        $global:DeployFixtureProbeFails = $case -eq 'failure'
        $global:DeployFixtureNoMarker = $case -eq 'no_marker'
        $global:DeployFixtureExisting = $case -ne 'missing'
        if ($case -in @('failure','no_marker')) {
            $caught = $false
            try { & $deploy -UavAddress '192.0.2.1' }
            catch { $caught = $true }
            Assert-Deploy $caught '检查失败应停止'
            Assert-Deploy ($global:DeployFixtureUploads.Count -eq 0 -and $global:DeployFixtureBuilds -eq 0) '检查失败后仍发生上传或编译'
            continue
        }
        & $deploy -UavAddress '192.0.2.1' -SyncConfig:($case -eq 'sync')
        Assert-Deploy ($global:DeployFixtureProbes.Count -eq 1) '应只合并执行一次目录/配置检查'
        $probeArgs = $global:DeployFixtureProbes[0]
        Assert-Deploy ($probeArgs -contains 'BatchMode=yes') '检查应禁用交互'
        Assert-Deploy ($probeArgs[-1].Contains('test -f') -eq ($case -ne 'sync')) 'SyncConfig没有跳过远端配置查询'
        $configs = @($global:DeployFixtureUploads | Where-Object { [string]$_[-1] -like '*config/fleet.json' })
        Assert-Deploy ($configs.Count -eq [int]($case -ne 'preserve')) '配置覆盖/保留行为错误'
        Assert-Deploy ($global:DeployFixtureBuilds -eq 1) '未进入正常编译'
    }
    Write-Host '通过：真实子进程参数与输出、失败/超时终止/日志，以及5种部署流程（无网络）。'
} finally {
    Remove-Variable -Scope Global -Name DeployFixtureProbes,DeployFixtureUploads,DeployFixtureBuilds,DeployFixtureProbeFails,DeployFixtureNoMarker,DeployFixtureExisting -ErrorAction SilentlyContinue
    $resolved = [IO.Path]::GetFullPath($temporary)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw '测试临时目录越界' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
