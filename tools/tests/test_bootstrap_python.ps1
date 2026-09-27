$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$source = Join-Path $ProjectRoot "tools\bootstrap_ground_impl.ps1"
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw "部署脚本语法错误。" }
foreach ($name in @("Resolve-CompatiblePython", "Get-PythonCommand", "Ensure-Python")) {
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
        if ($Name -eq 'winget') { return [pscustomobject]@{ Source='Invoke-FixtureWinget' } }
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
        New-Item -ItemType Directory -Path (Split-Path $script:installedPython -Parent) -Force | Out-Null
        [IO.File]::WriteAllBytes($script:installedPython, [byte[]]@())
        $global:LASTEXITCODE = 0
    }
    if ((Ensure-Python) -ne $script:installedPython) { throw "安装后仍依赖刷新 PATH。" }
    Write-Host "Python 兼容版本筛选、启动器真实路径、复用已有解释器和安装后直接发现检查通过。"
}
finally {
    $env:LOCALAPPDATA = $previousLocalAppData
    $global:LASTEXITCODE = $previousExit
    foreach ($name in @('Get-Command', 'Invoke-FixturePython', 'Invoke-FixtureLauncher', 'Invoke-FixtureWinget')) {
        Remove-Item -LiteralPath "Function:\$name" -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $fixtureRoot) { Remove-Item -LiteralPath $fixtureRoot -Recurse -Force }
}
