<#
.SYNOPSIS
    从 GitHub 获取竞赛地面端，并在当前电脑完成相对路径部署。

.DESCRIPTION
    该脚本可以直接从 raw.githubusercontent.com 执行，也可以在项目中执行。
    它不会把认证令牌写入 GitHub。首次部署且目标目录没有 tools/local_tokens.ps1
    时，会在本机交互式询问 AuthToken 和 PeerToken，并把它们保存到被 .gitignore
    忽略的本机文件中。
#>
[CmdletBinding()]
param(
    [string]$Repository = "minstrelll1/ZHIXIN",
    [string]$Branch = "codex/portable-ground-deployment",
    [string]$Destination = (Join-Path (Get-Location) "competition_development"),
    [string]$AuthToken = "",
    [string]$PeerToken = "",
    [switch]$SkipInstall,
    [switch]$ForceTokenConfig,
    [switch]$FromUsb
)

$ErrorActionPreference = "Stop"

# 中文控制台与 Python 子进程统一使用 UTF-8。
$utf8Encoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8Encoding
[Console]::InputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

function Get-PlainSecureValue([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Test-Token([string]$Value) {
    return (-not [string]::IsNullOrWhiteSpace($Value))
}

function Resolve-CompatiblePython([string]$Command, [string[]]$Arguments = @()) {
    # 返回实际解释器路径，避免 py.exe 后续丢失 -3 版本选择参数。
    try {
        $output = @(& $Command @Arguments -c "import json,sys; print(json.dumps(sys.executable)); raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 1)" 2>$null)
        if ($LASTEXITCODE -ne 0) { return $null }
        $interpreter = ($output -join "`n") | ConvertFrom-Json
        if ($interpreter -and (Test-Path -LiteralPath $interpreter -PathType Leaf)) { return [string]$interpreter }
    } catch {
        # 忽略不可用的商店别名、旧版本和未安装的启动器选项，继续寻找。
    }
    return $null
}

function Get-PythonCommand {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $interpreter = Resolve-CompatiblePython $python.Source
        if ($interpreter) { return $interpreter }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @("-3.11", "-3.12", "-3.10", "-3.9", "-3")) {
            $interpreter = Resolve-CompatiblePython $py.Source @($version)
            if ($interpreter) { return $interpreter }
        }
    }
    # 安装后当前进程的 PATH 未刷新，直接查找用户范围的解释器。
    if ($env:LOCALAPPDATA) {
        foreach ($version in @("311", "312", "310", "39")) {
            $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$version\python.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $interpreter = Resolve-CompatiblePython $candidate
                if ($interpreter) { return $interpreter }
            }
        }
    }
    return $null
}

function Install-OfficialPython {
    if (-not [Environment]::Is64BitOperatingSystem -or
        $env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or
        $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
        throw "自动安装目前只支持 x64 Windows。请安装兼容的 Python 3.9～3.12 后重试。"
    }
    # 官方 3.11.9 Windows x64 安装包；MD5 来自 python.org 发布页，再校验代码签名。
    $url = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    $expectedMd5 = "e8dcd502e34932eebcaf1be056d5cbcd"
    $installer = Join-Path ([IO.Path]::GetTempPath()) ("zhixin-python-3.11.9-" + [Guid]::NewGuid().ToString("N") + ".exe")
    try {
        Write-Host "正在从 Python 官网下载 Python 3.11.9（约 25 MB）..." -ForegroundColor Yellow
        try {
            Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 180 -OutFile $installer
        } catch {
            throw "无法从 Python 官网下载安装包：$($_.Exception.Message)。请检查网络后重试。"
        }
        if ((Get-FileHash -LiteralPath $installer -Algorithm MD5).Hash -ine $expectedMd5) {
            throw "Python 安装包校验不通过，已取消安装。"
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne "Valid" -or
            -not $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
            throw "Python 安装包的代码签名无效，已取消安装。"
        }
        Write-Host "安装包校验通过，正在为当前用户安装 Python 3.11..." -ForegroundColor Yellow
        $process = Start-Process -FilePath $installer -ArgumentList @(
            "/quiet", "InstallAllUsers=0", "Include_launcher=0", "Include_test=0",
            "Include_pip=1", "PrependPath=0"
        ) -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -ne 0) {
            throw "Python 安装程序退出码为 $($process.ExitCode)，请检查系统安装日志。"
        }
    } finally {
        if (Test-Path -LiteralPath $installer -PathType Leaf) {
            Remove-Item -LiteralPath $installer -Force
        }
    }
}

function Ensure-Python {
    $python = Get-PythonCommand
    if ($python) { return $python }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host "未找到兼容的 Python，正在尝试用 winget 安装 Python 3.11（用户范围）..." -ForegroundColor Yellow
        try {
            & $winget.Source install --id Python.Python.3.11 --exact --scope user --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -eq 0) { $python = Get-PythonCommand }
        } catch {
            Write-Warning "winget 安装未完成：$($_.Exception.Message)"
        }
    }
    if (-not $python) {
        Write-Host "winget 不可用或未安装成功，改用 Python 官方安装包。" -ForegroundColor Yellow
        Install-OfficialPython
        $python = Get-PythonCommand
    }
    if (-not $python) { throw "未找到已安装的 Python 3.11 解释器，请检查安装结果后重试。" }
    return $python
}

function Write-OnboardTokenConfig([string]$Root, [string]$LocalAuthToken, [string]$LocalPeerToken) {
    if (-not (Test-Token $LocalAuthToken) -or -not (Test-Token $LocalPeerToken)) {
        throw "本机令牌配置无效，请重新配置 AuthToken 和 PeerToken。"
    }
    $authEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($LocalAuthToken))
    $peerEncoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($LocalPeerToken))
    $lines = @(
        "# 本机机载认证配置；不要提交到版本库。",
        ('export AUTH_TOKEN="$(printf ''%s'' ''{0}'' | base64 --decode)"' -f $authEncoded),
        ('export PEER_TOKEN="$(printf ''%s'' ''{0}'' | base64 --decode)"' -f $peerEncoded)
    )
    # Bash 环境文件使用无 BOM 的 UTF-8 和 LF，避免 Windows 换行进入令牌。
    [IO.File]::WriteAllText((Join-Path $Root "tools\local_tokens.env"),
        (($lines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
}

function Ensure-TokenConfig([string]$Root) {
    $tokenFile = Join-Path $Root "tools\local_tokens.ps1"
    if ((Test-Path -LiteralPath $tokenFile) -and (-not $ForceTokenConfig)) {
        . $tokenFile
        Write-OnboardTokenConfig $Root $env:AUTH_TOKEN $env:PEER_TOKEN
        Write-Host "已保留本机令牌，并同步生成机载令牌配置。" -ForegroundColor Green
        return
    }
    $localAuthToken = $AuthToken
    $localPeerToken = $PeerToken
    if (-not (Test-Token $localAuthToken)) { $localAuthToken = Get-PlainSecureValue "请输入 AuthToken（不会显示）" }
    if (-not (Test-Token $localPeerToken)) { $localPeerToken = Get-PlainSecureValue "请输入 PeerToken（不会显示）" }
    if (-not (Test-Token $localAuthToken) -or -not (Test-Token $localPeerToken)) {
        throw "AuthToken 和 PeerToken 不能为空。"
    }
    $lines = @(
        "# 本机认证配置；不要提交到版本库。",
        ('$env:AUTH_TOKEN = ' + "'" + $localAuthToken.Replace("'", "''") + "'"),
        ('$env:PEER_TOKEN = ' + "'" + $localPeerToken.Replace("'", "''") + "'")
    )
    Set-Content -LiteralPath $tokenFile -Value $lines -Encoding UTF8
    Write-OnboardTokenConfig $Root $localAuthToken $localPeerToken
    Write-Host "已写入本机令牌配置（令牌未输出，且不会上传 GitHub）。" -ForegroundColor Green
}

function Copy-GroundProjectFiles([string]$SourceRoot, [string]$TargetRoot) {
    # 逐文件复制，可准确报告拒绝访问发生在源文件还是目标文件；也避免
    # Copy-Item -Recurse 合并已有 tools 目录时中途失败后难以定位。
    $sourcePrefix = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\') + '\'
    $targetPrefix = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\') + '\'
    foreach ($file in Get-ChildItem -LiteralPath $SourceRoot -Recurse -File -Force) {
        if (-not $file.FullName.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "下载包中的文件路径异常：$($file.FullName)"
        }
        $relative = $file.FullName.Substring($sourcePrefix.Length)
        $relativeUnix = $relative.Replace('\', '/')
        if ($relativeUnix -match '(^|/)(\.git|\.venv|__pycache__|ground_runtime|ground_logs|flight_records|received_images|pointcloud_records|position_tests|onboard_source_backup|\.codex_backup[^/]*|data)(/|$)' -or
            $relativeUnix -match '(^|/)(local_tokens\.ps1|local_tokens\.env|auto\.key|auto\.crt)$' -or
            $relativeUnix -match '\.(pyc|bag|log)$') { continue }
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "下载包中包含不支持的链接文件：$relative"
        }
        $target = [IO.Path]::GetFullPath((Join-Path $TargetRoot $relative))
        if (-not $target.StartsWith($targetPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "下载包中的文件路径越界：$relative"
        }
        if ($relative -in @('config\fleet.json', 'config\onboard_programs.json') -and
            (Test-Path -LiteralPath $target -PathType Leaf)) { continue }
        $targetDirectory = Split-Path $target -Parent
        if (-not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
            New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
        }
        for ($attempt = 1; $attempt -le 4; $attempt++) {
            try {
                Copy-Item -LiteralPath $file.FullName -Destination $target -Force -ErrorAction Stop
                break
            } catch {
                if ($attempt -lt 4) {
                    Start-Sleep -Milliseconds (500 * $attempt)
                    continue
                }
                $sourceState = try {
                    $stream = [IO.File]::Open($file.FullName, [IO.FileMode]::Open,
                        [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
                    $stream.Dispose()
                    '此时源文件可读取；请检查目标目录的写入权限或文件占用'
                } catch {
                    '此时源文件不可读取；请检查 Windows 安全中心的保护历史和下载包'
                }
                throw "部署文件复制失败：$relative`n来源：$($file.FullName)`n目标：$target`n诊断：$sourceState`n原始错误：$($_.Exception.Message)"
            }
        }
    }
}

# 已有部署的代码更新只拉取变化文件，不能回退到整仓库 ZIP。
if ($SkipInstall -and -not $FromUsb) {
    $updateRoot = [IO.Path]::GetFullPath($Destination)
    # 始终获取小体积的最新更新器，避免旧电脑优先执行本地旧版而无法修复自身。
    $updateRepo = $Repository -replace "^https?://github.com/", "" -replace "/$", ""
    if ($updateRepo -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or $Branch -match '\.\.' -or $Branch.Contains('\')) { throw "仓库或分支名称无效。" }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -Uri "https://raw.githubusercontent.com/$updateRepo/$Branch/tools/update_ground.ps1"
    $buffer = [IO.MemoryStream]::new()
    try {
        $response.RawContentStream.Position = 0
        $response.RawContentStream.CopyTo($buffer)
        $updateText = [Text.Encoding]::UTF8.GetString($buffer.ToArray())
    } finally { $buffer.Dispose(); $response.RawContentStream.Dispose() }
    & ([scriptblock]::Create($updateText.TrimStart([char]0xFEFF))) -Repository $Repository -Branch $Branch -Destination $updateRoot
    if ($ForceTokenConfig) { Ensure-TokenConfig $updateRoot }
    return
}

$Destination = [IO.Path]::GetFullPath($Destination)
$parent = Split-Path $Destination -Parent
if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

$tempRoot = $null
try {
    if ($FromUsb) {
        $sourceRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
        if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot "tools\install_ground_station.ps1") -PathType Leaf)) {
            throw "U 盘项目不完整：缺少 tools\install_ground_station.ps1。"
        }
        $sourcePrefix = $sourceRoot.TrimEnd('\') + '\'
        $destinationPrefix = $Destination.TrimEnd('\') + '\'
        if ($Destination -eq $sourceRoot -or
            $Destination.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or
            $sourceRoot.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "目标目录不能与 U 盘源码目录重叠。"
        }
        Write-Host "正在从 U 盘部署地面端：$sourceRoot" -ForegroundColor Cyan
    } else {
        $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("zhixin_ground_" + [guid]::NewGuid().ToString("N"))
        $zipPath = Join-Path $tempRoot "source.zip"
        $extractRoot = Join-Path $tempRoot "extract"
        New-Item -ItemType Directory -Path $tempRoot,$extractRoot -Force | Out-Null
        $encodedRepo = $Repository -replace "^https?://github.com/", "" -replace "/$", ""
        if ($Branch -match '\.\.' -or $Branch.Contains('\') -or $Branch.Contains('"')) { throw "分支名包含不安全字符。" }
        $zipUrl = "https://github.com/$encodedRepo/archive/refs/heads/$Branch.zip"
        Write-Host "正在下载竞赛地面端：$encodedRepo / $Branch" -ForegroundColor Cyan
        Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zipPath
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force
        $source = Get-ChildItem -LiteralPath $extractRoot -Directory | Select-Object -First 1
        if (-not $source -or -not (Test-Path (Join-Path $source.FullName "tools\install_ground_station.ps1"))) {
            throw "下载包中没有找到有效的竞赛地面端项目。请检查仓库和分支。"
        }
        $sourceRoot = $source.FullName
    }
    if (Test-Path -LiteralPath $Destination) {
        Write-Host "正在更新现有项目，保留本机令牌、日志和接收数据..." -ForegroundColor Cyan
    } else {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }
    Copy-GroundProjectFiles -SourceRoot $sourceRoot -TargetRoot $Destination
    Ensure-TokenConfig $Destination

    $media = Join-Path $Destination "third_party\mediamtx\mediamtx.exe"
    if (-not (Test-Path -LiteralPath $media)) {
        throw "缺少 third_party\mediamtx\mediamtx.exe。请确认发布包包含 MediaMTX。"
    }
    if (-not $SkipInstall) {
        $python = Ensure-Python
        $installer = Join-Path $Destination "tools\install_ground_station.ps1"
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer -PythonCommand $python
        if ($LASTEXITCODE -ne 0) { throw "地面端 Python 依赖安装失败。" }
    }
    Write-Host "地面端部署完成：$Destination" -ForegroundColor Green
    Write-Host "启动：在该目录执行 powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\start_ground.ps1" -ForegroundColor Green
}
finally {
    if ($tempRoot) {
        $resolvedTemp = [IO.Path]::GetFullPath($tempRoot)
        $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $resolvedTemp.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "部署临时目录超出系统临时路径，已停止清理：$resolvedTemp"
        }
        if (Test-Path -LiteralPath $tempRoot) {
            try { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
            catch { Write-Warning "部署临时文件未能清理：$tempRoot；$($_.Exception.Message)" }
        }
    }
}
