param([int]$WebPort=8000, [int]$BackendPid=0, [switch]$SkipRequest, [switch]$Quiet)
$ErrorActionPreference='Stop'
$ProjectRoot = (Split-Path $PSScriptRoot -Parent).TrimEnd('\','/')
$Logs = Join-Path $ProjectRoot 'ground_logs'
New-Item -ItemType Directory -Force $Logs | Out-Null
$LogPath = Join-Path $Logs 'ground_stop.log'
$utf8 = New-Object Text.UTF8Encoding($false)
function Report([string]$Text) {
    [IO.File]::AppendAllText($LogPath, ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text) + "`n", $utf8)
    if (-not $Quiet) { Write-Host $Text }
}
function Same-Process($Saved) {
    $current = Get-CimInstance Win32_Process -Filter ('ProcessId={0}' -f $Saved.ProcessId) -ErrorAction SilentlyContinue
    return ($null -ne $current -and $current.CreationDate -eq $Saved.CreationDate)
}
try {
    $all = @(Get-CimInstance Win32_Process)
    $byId = @{}
    foreach ($item in $all) { $byId[[int]$item.ProcessId] = $item }
    $targets = @{}
    $projectPattern = [regex]::Escape($ProjectRoot.Replace('/','\'))
    foreach ($item in $all) {
        $command = ([string]$item.CommandLine).Replace('/','\')
        $parent = $byId[[int]$item.ParentProcessId]
        $parentCommand = ([string]$parent.CommandLine).Replace('/','\')
        $backend = $item.Name -match '^pythonw?\.exe$' -and $command -match 'competition_backend\.ground_entry' -and ($command -match ($projectPattern + '\\') -or $parentCommand -match ($projectPattern + '\\'))
        $media = $item.Name -ieq 'mediamtx.exe' -and $command -match ($projectPattern + '\\ground_runtime\\mediamtx_uav[1-6]\.yml')
        if (($backend -and ($BackendPid -eq 0 -or $item.ProcessId -eq $BackendPid -or $item.ParentProcessId -eq $BackendPid)) -or $media) {
            $targets[[int]$item.ProcessId]=$item
        }
    }
    # 后端显式传入的 PID 同样必须通过项目命令行校验，禁止盲杀 PID。
    if ($BackendPid -gt 0 -and $byId.ContainsKey($BackendPid) -and -not $targets.ContainsKey($BackendPid)) {
        throw '后端 PID 的项目身份无法核验，未终止任何进程。'
    }
    do {
        $added=$false
        foreach ($item in $all) {
            if ($item.ProcessId -ne $PID -and $targets.ContainsKey([int]$item.ParentProcessId) -and -not $targets.ContainsKey([int]$item.ProcessId)) {
                if ([string]$item.CommandLine -match 'tools[\\/]stop_ground\.ps1') { continue }
                $targets[[int]$item.ProcessId]=$item; $added=$true
            }
        }
    } while ($added)
    $targets.Remove([int]$PID)
    if ($SkipRequest) { [Console]::Out.WriteLine('GROUND_STOP_READY'); [Console]::Out.Flush() }
    if (-not $SkipRequest) {
        try {
            $result=Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$WebPort/api/v1/programs/ground/stop" -ContentType 'application/json' -Body '{}' -TimeoutSec 12
            Report '已请求地面程序正常退出。'
        } catch { Report '地面退出接口不可用，按项目进程身份清理。' }
    }
    $deadline=(Get-Date).AddSeconds(20)
    do {
        # 纳入宽限期间新创建的子进程，并校验父进程创建时间以防 PID 复用。
        $fresh=@(Get-CimInstance Win32_Process)
        $freshById=@{}
        foreach($item in $fresh) { $freshById[[int]$item.ProcessId]=$item }
        do {
            $added=$false
            foreach($item in $fresh) {
                $parentId=[int]$item.ParentProcessId
                if ($item.ProcessId -ne $PID -and $targets.ContainsKey($parentId) -and $freshById.ContainsKey($parentId) -and $freshById[$parentId].CreationDate -eq $targets[$parentId].CreationDate -and -not $targets.ContainsKey([int]$item.ProcessId)) {
                    # 另一关闭助手自行退出，不把它当成服务结束。
                    if ([string]$item.CommandLine -match 'tools[\\/]stop_ground\.ps1') { continue }
                    $targets[[int]$item.ProcessId]=$item; $added=$true
                }
            }
        } while($added)
        $remaining=@($targets.Values | Where-Object { Same-Process $_ })
        if ($remaining.Count -eq 0) { break }
        Start-Sleep -Milliseconds 300
    } while ((Get-Date) -lt $deadline)
    foreach ($item in $remaining) {
        if (Same-Process $item) {
            $reply=Invoke-CimMethod -InputObject $item -MethodName Terminate
            if ($reply.ReturnValue -ne 0) { Report "进程 $($item.ProcessId) 终止失败，返回码 $($reply.ReturnValue)。" }
        }
    }
    Start-Sleep -Milliseconds 400
    $remaining=@($targets.Values | Where-Object { Same-Process $_ })
    if ($remaining.Count) { throw ('仍有本项目进程未退出：' + (($remaining | ForEach-Object ProcessId) -join '、')) }
    if ($targets.Count) { Report '已核验：本次识别的地面后端、子进程及本项目 MediaMTX 均已退出。' } else { Report '未发现本项目正在运行的地面后端或 MediaMTX。' }
    $listeners=@(Get-NetTCPConnection -LocalPort $WebPort,56010,56100,8554,8889 -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count) { Report ('以下端口仍由其他进程监听，未擅自结束：' + (($listeners | ForEach-Object { "$($_.LocalPort)(PID=$($_.OwningProcess))" }) -join '、')) }
    exit 0
} catch {
    Report ('关闭失败：' + $_.Exception.Message)
    exit 1
}
