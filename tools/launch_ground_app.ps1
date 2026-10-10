param([int]$WebPort = 8000)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$LogDirectory = Join-Path $ProjectRoot 'ground_logs'
$Url = "http://127.0.0.1:$WebPort/"
Add-Type -AssemblyName System.Windows.Forms
$mutex = New-Object System.Threading.Mutex($false, 'Local\ZhiXinGroundLauncher')
$held = $false

function Test-GroundReady {
    try {
        $data = Invoke-RestMethod -Uri ($Url + 'api/v1/status') -TimeoutSec 2
        return ($data.ok -eq $true -and ($null -ne $data.operator -or $data.adapter -eq 'awaiting_selection'))
    } catch { return $false }
}

try {
    try { $held = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { return }
    if (-not (Test-GroundReady)) {
        New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
        $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
        $outFile = Join-Path $LogDirectory "app_start_${stamp}.out.log"
        $errFile = Join-Path $LogDirectory "app_start_${stamp}.err.log"
        $script = Join-Path $PSScriptRoot 'start_ground.ps1'
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -WebPort {1} -ConfirmLiveConfig' -f $script, $WebPort
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru
        $deadline = (Get-Date).AddSeconds(90)
        $ready = $false
        do {
            if (Test-GroundReady) { $ready = $true; break }
            if ($process.HasExited) { break }
            Start-Sleep -Milliseconds 350
        } while ((Get-Date) -lt $deadline)
        if (-not $ready) {
            $details = ''
            if (Test-Path -LiteralPath $errFile) { $details = Get-Content -LiteralPath $errFile -Raw }
            throw "地面程序未能启动。请检查是否已完成首次部署，以及端口 $WebPort 是否被占用。`n日志：$errFile`n$details"
        }
    } else {
        # 后台已运行时不会再次经过 start_ground，仍允许补配缺失的本机规则。
        try {
            & (Join-Path $PSScriptRoot 'configure_ground_firewall.ps1') -WebPort $WebPort
        } catch {
            Write-Warning ("地面互联防火墙检查失败，继续打开网页：{0}" -f $_.Exception.Message)
        }
    }
    Start-Process $Url
} catch {
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '智信竞赛程序', 'OK', 'Error') | Out-Null
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
