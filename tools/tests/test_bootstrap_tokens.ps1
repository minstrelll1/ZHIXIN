$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$source = Join-Path $ProjectRoot "tools\bootstrap_ground_impl.ps1"
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw "部署脚本语法检查失败。" }
foreach ($name in @("Test-Token", "Write-OnboardTokenConfig", "Ensure-TokenConfig")) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $definition) { throw "未找到待测函数。" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
$recordRoot = Join-Path ([IO.Path]::GetTempPath()) ("zhixin-token-test-" + [Guid]::NewGuid().ToString("N"))
$previousAuth = $env:AUTH_TOKEN
$previousPeer = $env:PEER_TOKEN
try {
    New-Item -ItemType Directory -Path (Join-Path $recordRoot "tools") -Force | Out-Null
    $script:ForceTokenConfig = $false
    $script:AuthToken = "test-auth-'" + [char]34 + '$x`y'
    $script:PeerToken = "test-peer-123"
    Ensure-TokenConfig $recordRoot
    . (Join-Path $recordRoot "tools\local_tokens.ps1")
    if ($env:AUTH_TOKEN -ne $script:AuthToken -or $env:PEER_TOKEN -ne $script:PeerToken) { throw "地面令牌未保持原值。" }
    $envFile = Join-Path $recordRoot "tools\local_tokens.env"
    $bytes = [IO.File]::ReadAllBytes($envFile)
    if ($bytes[0] -eq 239 -or $bytes -contains 13) { throw "Bash 配置包含 BOM 或 CR。" }
    $content = [Text.Encoding]::UTF8.GetString($bytes)
    foreach ($item in @(@("AUTH_TOKEN", $script:AuthToken), @("PEER_TOKEN", $script:PeerToken))) {
        $pattern = "export " + $item[0] + '=.*printf ''%s'' ''([^'']+)'''
        if ($content -notmatch $pattern) { throw "缺少机载令牌配置。" }
        $decoded = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Matches[1]))
        if ($decoded -ne $item[1]) { throw "机载令牌未保持原值。" }
    }
    Remove-Item -LiteralPath $envFile
    $script:AuthToken = "ignored-new-value"
    $script:PeerToken = "ignored-new-value"
    Ensure-TokenConfig $recordRoot
    if (-not (Test-Path -LiteralPath $envFile)) { throw "更新时未补齐机载配置。" }
    if ($env:AUTH_TOKEN -eq $script:AuthToken) { throw "更新覆盖了原地面令牌。" }
    $failed = $false
    try { Write-OnboardTokenConfig $recordRoot "" "" } catch { $failed = $true }
    if (-not $failed) { throw "空令牌未被拒绝。" }
    Write-Host "首次部署、更新补齐、特殊字符、Bash 编码和空令牌检查通过。"
}
finally {
    $env:AUTH_TOKEN = $previousAuth
    $env:PEER_TOKEN = $previousPeer
    # 仅清理本测试创建且已核实位于临时目录内的文件夹。
    $target = [IO.Path]::GetFullPath($recordRoot)
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $target.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "测试清理路径不安全。" }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
