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
    [switch]$ForceTokenConfig
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
    # winget 安装后当前进程的 PATH 未刷新，直接查找用户范围的解释器。
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

function Ensure-Python {
    $python = Get-PythonCommand
    if ($python) { return $python }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "未找到兼容的 Python 3.9～3.12，且本机没有 winget。请先安装 Python 3.11，再重新执行本命令。"
    }
    Write-Host "未找到兼容的 Python，正在尝试用 winget 安装 Python 3.11（用户范围）..." -ForegroundColor Yellow
    & $winget.Source install --id Python.Python.3.11 --exact --scope user --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { throw "Python 安装失败，请手动安装 Python 3.11 后重试。" }
    $python = Get-PythonCommand
    if (-not $python) { throw "未找到已安装的 Python 3.11 解释器，请检查 Python 安装结果后重试。" }
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

$Destination = [IO.Path]::GetFullPath($Destination)
$parent = Split-Path $Destination -Parent
if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("zhixin_ground_" + [guid]::NewGuid().ToString("N"))
$zipPath = Join-Path $tempRoot "source.zip"
$extractRoot = Join-Path $tempRoot "extract"
New-Item -ItemType Directory -Path $tempRoot,$extractRoot -Force | Out-Null
try {
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
    if (Test-Path -LiteralPath $Destination) {
        Write-Host "正在更新现有项目，保留本机令牌、日志和接收数据..." -ForegroundColor Cyan
    } else {
        New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    }
    Get-ChildItem -LiteralPath $source.FullName -Force | Where-Object {
        $_.Name -notin @("tools\local_tokens.ps1", "tools\local_tokens.env", ".git")
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
    }
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
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
}
