$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$source = Join-Path $ProjectRoot "tools\bootstrap_ground_impl.ps1"
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw "部署脚本语法错误。" }
foreach ($name in @("Resolve-CompatiblePython", "Get-PythonCommand", "Install-OfficialPython", "Ensure-Python")) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $definition) { throw "缺少 Python 选择函数：$name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$fixtureRoot = Join-Path ([IO.Path]::GetTempPath()) ("zhixin-python-test-" + [Guid]::NewGuid().ToString("N"))
$previousLocalAppData = $env:LOCALAPPDATA
$previousExit = $global:LASTEXITCODE
try {
    New-Item -ItemType Directory -Path $fixtureRoot -Force | Out-Null
    $script:interpreter = Join-Path $fixtureRoot "解释器 311.exe"
    [IO.File]::WriteAllBytes($script:interpreter, [byte[]]@())
    $script:pythonAvailable = $true
    $script:launcherAvailable = $true
    $script:pythonExit = 1
    $script:wingetAvailable = $true
    $script:wingetFails = $false
    $script:probeCalls = @()
    function Invoke-FixturePython {
        param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
        $script:probeCalls += ,@("python", $Arguments)
        if ($Arguments[-1] -notmatch '\(3,9\).*\(3,13\)') { throw "解释器探测缺少兼容版本范围。" }
        $global:LASTEXITCODE = $script:pythonExit
        return (ConvertTo-Json -InputObject $script:interpreter -Compress)
    }
    function Invoke-FixtureLauncher {
        param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
        $script:probeCalls += ,@("py", $Arguments)
        if ($Arguments[0] -ne '-3.11') { throw "Python 启动器没有优先选择 3.11。" }
        $global:LASTEXITCODE = 0
        return (ConvertTo-Json -InputObject $script:interpreter -Compress)
    }
    function Get-Command {
        param([string]$Name, [string]$ErrorAction)
        if ($Name -eq 'python' -and $script:pythonAvailable) { return [pscustomobject]@{ Source='Invoke-FixturePython' } }
        if ($Name -eq 'py' -and $script:launcherAvailable) { return [pscustomobject]@{ Source='Invoke-FixtureLauncher' } }
        if ($Name -eq 'winget' -and $script:wingetAvailable) { return [pscustomobject]@{ Source='Invoke-FixtureWinget' } }
        return $null
    }
    $selected = Get-PythonCommand
    if ($selected -ne $script:interpreter -or $script:probeCalls.Count -ne 2) {
        throw "未跳过不兼容 Python 并解析启动器的实际解释器路径。"
    }
    $script:pythonExit = 0
    $script:probeCalls = @()
    if ((Get-PythonCommand) -ne $script:interpreter -or $script:probeCalls.Count -ne 1) {
        throw "已有兼容 Python 不应重新安装或调用启动器。"
    }
    # 模拟 winget 安装完成而当前 PATH 未刷新；不执行实际安装。
    $script:pythonAvailable = $false
    $script:launcherAvailable = $false
    $env:LOCALAPPDATA = Join-Path $fixtureRoot "用户目录"
    $script:installedPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"
    function Resolve-CompatiblePython([string]$Command, [string[]]$Arguments = @()) {
        if ($Command -eq $script:installedPython -and (Test-Path -LiteralPath $Command -PathType Leaf)) { return $Command }
        return $null
    }
    function Invoke-FixtureWinget {
        param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
        if ($Arguments -notcontains 'Python.Python.3.11' -or $Arguments -notcontains 'user') { throw "新安装未固定 Python 3.11 用户范围。" }
        if ($script:wingetFails) { $global:LASTEXITCODE = 1; return }
        New-Item -ItemType Directory -Path (Split-Path $script:installedPython -Parent) -Force | Out-Null
        [IO.File]::WriteAllBytes($script:installedPython, [byte[]]@())
        $global:LASTEXITCODE = 0
    }
    if ((Ensure-Python) -ne $script:installedPython) { throw "安装后仍依赖刷新 PATH。" }
    Remove-Item -LiteralPath $script:installedPython -Force
    $script:wingetAvailable = $false
    $script:officialCalls = 0
    function Install-OfficialPython {
        $script:officialCalls++
        [IO.File]::WriteAllBytes($script:installedPython, [byte[]]@())
    }
    if ((Ensure-Python) -ne $script:installedPython -or $script:officialCalls -ne 1) {
        throw "没有 winget 时未使用 Python 官方安装包。"
    }
    Remove-Item -LiteralPath $script:installedPython -Force
    $script:wingetAvailable = $true
    $script:wingetFails = $true
    if ((Ensure-Python) -ne $script:installedPython -or $script:officialCalls -ne 2) {
        throw "winget 安装失败时未回退到 Python 官方安装包。"
    }
    # 单独核验官方下载路径、指纹、签名与静默安装参数；不下载或执行安装程序。
    $officialDefinition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-OfficialPython' }, $true)
    . ([scriptblock]::Create($officialDefinition.Extent.Text))
    $script:signatureStatus = 'NotSigned'
    $script:installCalls = 0
    function Invoke-WebRequest {
        param([string]$Uri, [switch]$UseBasicParsing, [int]$TimeoutSec, [string]$OutFile)
        if ($Uri -ne 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe') {
            throw "安装包来源不是已核对的 Python 官方发布地址。"
        }
        [IO.File]::WriteAllBytes($OutFile, [byte[]]@(1,2,3))
    }
    function Get-FileHash {
        param([string]$LiteralPath, [string]$Algorithm)
        if ($Algorithm -ne 'MD5' -or -not (Test-Path -LiteralPath $LiteralPath)) { throw '未校验下载文件指纹。' }
        return [pscustomobject]@{ Hash='e8dcd502e34932eebcaf1be056d5cbcd' }
    }
    function Get-AuthenticodeSignature {
        param([string]$LiteralPath)
        return [pscustomobject]@{ Status=$script:signatureStatus; SignerCertificate=[pscustomobject]@{ Subject='CN=Python Software Foundation' } }
    }
    function Start-Process {
        param([string]$FilePath, [string[]]$ArgumentList, [switch]$Wait, [switch]$PassThru, [string]$WindowStyle)
        if (-not $Wait -or -not $PassThru -or $WindowStyle -ne 'Hidden' -or
            $ArgumentList -notcontains '/quiet' -or $ArgumentList -notcontains 'InstallAllUsers=0' -or
            $ArgumentList -notcontains 'Include_pip=1') { throw '官方安装参数不正确。' }
        $script:installCalls++
        return [pscustomobject]@{ ExitCode=0 }
    }
    $rejected = $false
    try { Install-OfficialPython } catch { $rejected = $_.Exception.Message -match '代码签名无效' }
    if (-not $rejected -or $script:installCalls -ne 0) { throw '未拦截签名无效的安装包。' }
    $script:signatureStatus = 'Valid'
    Install-OfficialPython
    if ($script:installCalls -ne 1) { throw '校验通过后未执行官方安装包。' }
    Write-Host "Python 版本筛选、复用已有解释器、winget 与无 winget 回退检查通过。"
}
finally {
    $env:LOCALAPPDATA = $previousLocalAppData
    $global:LASTEXITCODE = $previousExit
    foreach ($name in @('Get-Command', 'Invoke-FixturePython', 'Invoke-FixtureLauncher', 'Invoke-FixtureWinget',
            'Invoke-WebRequest', 'Get-FileHash', 'Get-AuthenticodeSignature', 'Start-Process')) {
        Remove-Item -LiteralPath "Function:\$name" -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $fixtureRoot) { Remove-Item -LiteralPath $fixtureRoot -Recurse -Force }
}
