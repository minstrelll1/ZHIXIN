# 短时部署检查：给远程命令设置整体期限，而不只限制 SSH 建连时间。
function Write-OnboardDeployLog {
    param([string]$LogPath, [string]$Message)
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    [IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Write-Host $line
}

function ConvertTo-OnboardNativeArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows CRT 参数转义；不经过 cmd.exe，不解释 shell 元字符。
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Invoke-OnboardDeployProbe {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$LogPath,
        [ValidateRange(1, 120)][int]$TimeoutSeconds = 20
    )
    $startInfo = New-Object Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = ($ArgumentList | ForEach-Object { ConvertTo-OnboardNativeArgument $_ }) -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $startInfo.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $startInfo
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $nextProgress = 5
    $stdout = $null
    $stderr = $null
    $started = $false
    $timedOut = $false
    try {
        $started = $process.Start()
        $process.StandardInput.Close()
        # 同时排空两条输出管道，避免输出缓冲区满导致自身阻塞。
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        while (-not $process.WaitForExit(100)) {
            if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                $timedOut = $true
                break
            }
            if ($timer.Elapsed.TotalSeconds -ge $nextProgress) {
                Write-OnboardDeployLog $LogPath ("等待机载 SSH 目录/配置检查返回，已用时 {0:N0} 秒，上限 {1} 秒。" -f $timer.Elapsed.TotalSeconds, $TimeoutSeconds)
                $nextProgress += 5
            }
        }
        if ($timedOut) {
            throw "机载 SSH 目录/配置检查超过 $TimeoutSeconds 秒；已中止本次检查，尚未上传代码。请检查链路、机载负载或 SSH 登录初始化脚本。日志：$LogPath"
        }
        if (-not $stdout.Wait(1000) -or -not $stderr.Wait(1000)) {
            throw "机载检查进程已退出，但输出未关闭；本次部署已停止。日志：$LogPath"
        }
        $result = [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $stdout.Result; ErrorOutput = $stderr.Result }
        if ($result.ExitCode -ne 0) {
            throw "机载 SSH 目录/配置检查失败（退出码 $($result.ExitCode)）：$($result.ErrorOutput.Trim())。日志：$LogPath"
        }
        Write-OnboardDeployLog $LogPath ("机载目录/配置检查完成，用时 {0:N1} 秒。" -f $timer.Elapsed.TotalSeconds)
        return $result
    } finally {
        # 只结束本次检查的本地 SSH 子进程；不停止机载业务程序。
        if ($started -and -not $process.HasExited) {
            $process.Kill()
            [void]$process.WaitForExit(1500)
        }
        foreach ($entry in @(@('SSH stdout', $stdout), @('SSH stderr', $stderr))) {
            $task = $entry[1]
            if ($null -ne $task -and $task.Wait(500)) {
                [IO.File]::AppendAllText($LogPath, "[$($entry[0])]`r`n" + $task.Result + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
            }
        }
        if ($timedOut) {
            [IO.File]::AppendAllText($LogPath, "[TIMEOUT] $TimeoutSeconds seconds" + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
        }
        $process.Dispose()
    }
}
