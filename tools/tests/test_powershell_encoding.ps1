$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$ToolRoot = Join-Path $ProjectRoot "tools"
if ($PSVersionTable.PSVersion.Major -ne 5) {
    throw "请使用 powershell.exe 运行此检查，确保验证 Windows PowerShell 5 的文件解码。"
}
$files = @(Get-ChildItem -LiteralPath $ToolRoot -Filter "*.ps1" -File | Where-Object { $_.Name -ne "local_tokens.ps1" })
$files += @(Get-ChildItem -LiteralPath (Join-Path $ToolRoot "tests") -Filter "*.ps1" -File)
foreach ($file in $files) {
    $bytes = [IO.File]::ReadAllBytes($file.FullName)
    $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 239 -and $bytes[1] -eq 187 -and $bytes[2] -eq 191
    $content = [Text.Encoding]::UTF8.GetString($bytes)
    if ($file.Name -eq "bootstrap_ground.ps1") {
        if ($bom -or $content -match '[^\x00-\x7F]') {
            throw "下载入口必须为无 BOM 的 ASCII：$($file.Name)"
        }
    }
    elseif ($content -match '[^\x00-\x7F]' -and -not $bom) {
        throw "含中文的本地 PowerShell 脚本必须带 UTF-8 BOM：$($file.Name)"
    }
    $tokens = $null
    $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count) {
        throw "Windows PowerShell 5 解析失败：$($file.Name)；$($parseErrors.Message -join '；')"
    }
}
# 不安装或启动服务；验证安装脚本真正从本地文件执行并进入中文错误处理。
$installer = Join-Path $ToolRoot "install_ground_station.ps1"
$expected = "未找到 Python 3，请安装 Python 3 并勾选添加到 PATH。"
$caught = $false
try { & $installer -PythonCommand "zhixin-test-nonexistent-python-command" }
catch {
    if ($_.Exception.Message -ne $expected) { throw }
    $caught = $true
}
if (-not $caught) { throw "未验证安装脚本的本地执行入口。" }
Write-Host "Windows PowerShell 5 编码与解析检查通过：$($files.Count) 个脚本；安装脚本本地入口通过。"
