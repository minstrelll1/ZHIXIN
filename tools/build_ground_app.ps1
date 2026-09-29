param()
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path $PSScriptRoot -Parent
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
if (-not (Test-Path -LiteralPath $compiler)) { throw '缺少 Windows .NET Framework 编译器。' }
$output = Join-Path $ProjectRoot '智信竞赛.exe'
& $compiler /nologo /target:winexe /reference:System.Windows.Forms.dll "/out:$output" (Join-Path $PSScriptRoot 'CompetitionLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw '生成双击启动应用失败。' }
Write-Host "已生成：$output"
