[CmdletBinding()]
param(
    [string]$Repository = 'minstrelll1/ZHIXIN',
    [string]$Branch = 'codex/portable-ground-deployment',
    [string]$Destination = (Join-Path (Get-Location) 'competition_development')
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

function Resolve-UpdatePath([string]$Root, [string]$Relative) {
    if ([string]::IsNullOrWhiteSpace($Relative) -or $Relative.Contains('\') -or $Relative -match '[:*?"<>|]' -or $Relative.StartsWith('/')) { throw "更新路径无效：$Relative" }
    $segments = $Relative.Split('/')
    foreach ($segment in $segments) {
        if (-not $segment -or $segment -in @('.', '..') -or $segment -match '[. ]$' -or $segment -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') { throw "更新路径无效：$Relative" }
    }
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $target = [IO.Path]::GetFullPath((Join-Path $base ($Relative.Replace('/', '\'))))
    if (-not $target.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "更新路径超出项目目录：$Relative" }
    $probe = $base
    foreach ($part in @('') + $segments) {
        if ($part) { $probe = Join-Path $probe $part }
        if ((Test-Path -LiteralPath $probe) -and ((Get-Item -LiteralPath $probe -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "更新路径包含目录链接，请使用实际项目目录：$probe" }
    }
    return $target
}

function Write-UpdateLog([string]$Message) {
    Write-Host $Message
    [IO.File]::AppendAllText($script:updateLog, ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message) + [Environment]::NewLine, $utf8)
}

function Get-BlobHash([string]$Path, [switch]$NormalizeText) {
    $sha = [Security.Cryptography.SHA1]::Create()
    $stream = $null
    try {
        if ($NormalizeText) {
            $bytes = [IO.File]::ReadAllBytes($Path)
            $strict = [Text.UTF8Encoding]::new($false, $true)
            try { $content = $strict.GetString($bytes).Replace("`r`n", "`n") } catch { return '' }
            $stream = [IO.MemoryStream]::new($utf8.GetBytes($content))
        } else { $stream = [IO.File]::OpenRead($Path) }
        $header = [Text.Encoding]::ASCII.GetBytes(('blob ' + $stream.Length + [char]0))
        [void]$sha.TransformBlock($header, 0, $header.Length, $header, 0)
        $buffer = New-Object byte[] 65536
        while (($count = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) { [void]$sha.TransformBlock($buffer, 0, $count, $buffer, 0) }
        [void]$sha.TransformFinalBlock([byte[]]@(), 0, 0)
        return ([BitConverter]::ToString($sha.Hash)).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

function Test-BlobMatch([string]$Path, [string]$Hash) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ((Get-BlobHash $Path) -eq $Hash) { return $true }
    $extension = [IO.Path]::GetExtension($Path).ToLowerInvariant()
    $textFile = $extension -in @('.ps1','.py','.sh','.env','.toml','.yml','.yaml','.md','.txt','.json','.geojson','.code-workspace','.js','.html','.css','.xml','.msg','.srv','.launch','.cmake','.cfg','.ini','.h','.hpp','.cpp','.c','.kml','.svg','.csv') -or [IO.Path]::GetFileName($Path) -in @('.editorconfig','.gitattributes','.gitignore','LICENSE')
    if ($textFile -and (Get-Item -LiteralPath $Path).Length -lt 16MB) {
        return (Get-BlobHash $Path -NormalizeText) -eq $Hash
    }
    return $false
}

function Test-ProtectedPath([string]$Path) {
    return $Path -match '^(\.git|\.runtime|\.venv|ground_runtime|ground_logs|flight_records|received_images|image_cache|pointcloud_records|onboard_source_backup|position_tests|build[^/]*|devel[^/]*|logs|dist)(/|$)' -or
        $Path -match '^competition_backend/(\.venv|data)(/|$)' -or
        $Path -match '^tools/local_tokens\.(ps1|env)$' -or $Path -match '^docs/flight_reviews(/|$)' -or $Path -match '(^|/)(__pycache__|auto\.key|auto\.crt)(/|$)'
}

function Test-PreservedFile([string]$Path, [string]$Local) {
    return (Test-ProtectedPath $Path) -or ($Path -in @('config/fleet.json', 'config/onboard_programs.json') -and (Test-Path -LiteralPath $Local)) -or
        ($Path -match '^third_party/.*\.(exe|dll|zip|msi)$' -and (Test-Path -LiteralPath $Local))
}

function Request-UpdateFile([string]$Url, [string]$OutFile = '') {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $request = @{ Uri=$Url; UseBasicParsing=$true; TimeoutSec=60; Headers=@{'User-Agent'='ZhiXin-Ground-Updater'; 'Accept'='application/vnd.github+json'} }
            if ($OutFile) { Invoke-WebRequest @request -OutFile $OutFile | Out-Null; return }
            $response = Invoke-WebRequest @request
            $buffer = [IO.MemoryStream]::new()
            try {
                $response.RawContentStream.Position = 0
                $response.RawContentStream.CopyTo($buffer)
                return ($utf8.GetString($buffer.ToArray()).TrimStart([char]0xFEFF) | ConvertFrom-Json)
            } finally { $buffer.Dispose(); $response.RawContentStream.Dispose() }
        } catch {
            $status = 0
            if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
            # 403/429 需要等待服务端配额恢复；立即重试只会继续消耗请求。
            if ($status -in @(401,403,404,429)) {
                if ($status -in @(403,429)) {
                    Write-UpdateLog "更新服务限制请求（HTTP $status），停止立即重试。"
                }
                throw
            }
            if ($attempt -eq 3) { throw }
            Write-UpdateLog "网络请求失败，正在重试（$attempt/3）。"
            Start-Sleep -Seconds $attempt
        }
    }
}

function Get-UpdateSnapshot([string]$Repo, [string]$Ref) {
    # 由源码分支每次 push 后的工作流发布，指向不可变的源码提交。
    # 普通客户端只读 raw 清单和变化文件，不调用 GitHub REST API。
    $escapedRef = (($Ref.Split('/') | ForEach-Object { [Uri]::EscapeDataString($_) }) -join '/')
    $url = "https://raw.githubusercontent.com/$Repo/codex/ground-update-manifests/manifests/$escapedRef.json"
    $manifest = $null
    try { $manifest = Request-UpdateFile $url }
    catch {
        Write-UpdateLog '发布清单暂不可用，尝试兼容旧仓库的版本查询。'
    }
    if ($null -ne $manifest) {
        if ($manifest.schema_version -ne 1 -or $manifest.repository -ne $Repo -or
            $manifest.branch -cne $Ref -or $manifest.commit -notmatch '^[a-f0-9]{40}$' -or
            $manifest.tree_sha -notmatch '^[a-f0-9]{40}$' -or -not $manifest.files -or
            $manifest.file_count -ne @($manifest.files).Count) {
            throw '发布文件清单无效，已停止更新；尚未替换本机代码。'
        }
        Write-UpdateLog ('已读取增量发布清单：{0}（生成时间 {1}）；无需 GitHub API。' -f $manifest.commit.Substring(0,7), $manifest.generated_at)
        return [pscustomobject]@{ Version=[string]$manifest.commit; Files=@($manifest.files); Source='published_manifest' }
    }
    try {
        $commit = Request-UpdateFile "https://api.github.com/repos/$Repo/commits/$([Uri]::EscapeDataString($Ref))"
        if ($commit.sha -notmatch '^[a-f0-9]{40}$' -or $commit.commit.tree.sha -notmatch '^[a-f0-9]{40}$') { throw 'GitHub 返回的版本信息无效。' }
        $tree = Request-UpdateFile "https://api.github.com/repos/$Repo/git/trees/$($commit.commit.tree.sha)?recursive=1"
        if ($tree.truncated -or -not $tree.tree) { throw 'GitHub 文件清单不完整，已停止更新。' }
        return [pscustomobject]@{ Version=[string]$commit.sha; Files=@($tree.tree | Where-Object { $_.type -eq 'blob' }); Source='legacy_api' }
    } catch {
        $status = 0
        if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
        if ($status -in @(403,429)) {
            throw 'GitHub API 已限流，且增量发布清单尚不可用。请等待仓库 Actions 中“发布地面增量更新清单”成功后重试原命令；无需重装或修改 AuthToken/PeerToken。'
        }
        throw
    }
}

$Destination = [IO.Path]::GetFullPath($Destination)
$logDirectory = Resolve-UpdatePath $Destination 'ground_logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$script:updateLog = Join-Path $logDirectory ('update_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + [guid]::NewGuid().ToString('N').Substring(0,8) + '.log')
$lock = $null
$applied = [Collections.Generic.List[object]]::new()
$committed = $false
try {
    Write-UpdateLog "增量更新日志：$script:updateLog"
    if (-not (Test-Path -LiteralPath (Resolve-UpdatePath $Destination 'tools/install_ground_station.ps1'))) { throw '未找到已部署的项目。首次部署请去掉 -SkipInstall。' }
    $Repository = $Repository -replace '^https?://github.com/', '' -replace '/$', ''
    if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or [string]::IsNullOrWhiteSpace($Branch) -or
        $Branch -match '\.\.' -or $Branch.Contains('\') -or $Branch.StartsWith('/') -or $Branch.EndsWith('/')) { throw '仓库或分支名称无效。' }
    $runtime = Resolve-UpdatePath $Destination '.runtime'
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $lockPath = Resolve-UpdatePath $Destination '.runtime/ground_update.lock'
    try { $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw '另一个更新程序正在运行，请等待它结束后重试。' }
    $runRoot = Resolve-UpdatePath $Destination ('.runtime/ground_update_runs/' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
    $statePath = Resolve-UpdatePath $Destination '.runtime/ground_update_state.json'
    $previous = $null
    if (Test-Path -LiteralPath $statePath) {
        try { $previous = [IO.File]::ReadAllText($statePath, $utf8) | ConvertFrom-Json } catch { Write-UpdateLog '旧更新记录无法读取，将按本地文件重新核对。' }
    }
    Write-UpdateLog '正在查询版本和文件清单；保留现有机队配置、令牌、数据及第三方软件。'
    $snapshot = Get-UpdateSnapshot $Repository $Branch
    $version = $snapshot.Version
    $files = $snapshot.Files
    $current = @{}
    $downloads = [Collections.Generic.List[object]]::new()
    $removals = [Collections.Generic.List[object]]::new()
    [long]$totalBytes = 0
    foreach ($file in $files) {
        $relative = [string]$file.path
        $local = Resolve-UpdatePath $Destination $relative
        if (Test-PreservedFile $relative $local) { continue }
        if ($file.type -ne 'blob' -or $file.mode -notin @('100644','100755') -or $file.sha -notmatch '^[a-f0-9]{40}$' -or
            $null -eq $file.size -or [long]$file.size -lt 0) { throw "不支持的仓库文件：$relative" }
        if ($current.ContainsKey($relative)) { throw "Windows 文件路径重名：$relative" }
        $current[$relative] = [string]$file.sha
        if (Test-BlobMatch $local $file.sha) { continue }
        $stage = Resolve-UpdatePath $runRoot ('staged/' + $relative)
        $downloads.Add([pscustomobject]@{ Path=$relative; Local=$local; Stage=$stage; Sha=[string]$file.sha; Size=[long]$file.size })
        $totalBytes += [long]$file.size
    }
    if ($previous -and $previous.repository -eq $Repository -and $previous.branch -eq $Branch) {
        foreach ($property in $previous.files.PSObject.Properties) {
            $relative = $property.Name
            $local = Resolve-UpdatePath $Destination $relative
            if ($current.ContainsKey($relative) -or (Test-PreservedFile $relative $local) -or -not (Test-Path -LiteralPath $local)) { continue }
            if (Test-BlobMatch $local ([string]$property.Value)) { $removals.Add([pscustomobject]@{ Path=$relative; Local=$local; Stage=$null }) }
            else { Write-UpdateLog "保留本机改动的旧文件：$relative" }
        }
    }
    Write-UpdateLog ('版本 {0}：需下载 {1} 个文件，共 {2:N1} KB；清理 {3} 个已停用文件。' -f $version.Substring(0,7), $downloads.Count, ($totalBytes/1KB), $removals.Count)
    foreach ($file in $downloads) {
        Write-UpdateLog "下载：$($file.Path)"
        New-Item -ItemType Directory -Path (Split-Path $file.Stage -Parent) -Force | Out-Null
        $escapedPath = (($file.Path.Split('/') | ForEach-Object { [Uri]::EscapeDataString($_) }) -join '/')
        Request-UpdateFile "https://raw.githubusercontent.com/$Repository/$version/$escapedPath" $file.Stage
        if ((Get-Item -LiteralPath $file.Stage).Length -ne $file.Size -or (Get-BlobHash $file.Stage) -ne $file.Sha) { throw "文件校验失败：$($file.Path)，尚未替换本机代码。" }
        if ($file.Path.EndsWith('.ps1')) {
            $tokens = $null; $parseErrors = $null
            [void][Management.Automation.Language.Parser]::ParseFile($file.Stage, [ref]$tokens, [ref]$parseErrors)
            if ($parseErrors.Count) { throw "PowerShell 脚本语法校验失败：$($file.Path)：$($parseErrors[0].Message)" }
        }
    }
    # 所有下载和校验成功后才替换；替换失败则恢复本次已经改动的文件。
    foreach ($file in @($downloads.ToArray()) + @($removals.ToArray())) {
        [void](Resolve-UpdatePath $Destination $file.Path)
        $existed = Test-Path -LiteralPath $file.Local -PathType Leaf
        $backup = Resolve-UpdatePath $runRoot ('backup/' + $file.Path)
        if ($existed) {
            New-Item -ItemType Directory -Path (Split-Path $backup -Parent) -Force | Out-Null
            Copy-Item -LiteralPath $file.Local -Destination $backup -Force
        }
        $applied.Add([pscustomobject]@{ Local=$file.Local; Backup=$backup; Existed=$existed; Path=$file.Path })
        if ($file.Stage) {
            New-Item -ItemType Directory -Path (Split-Path $file.Local -Parent) -Force | Out-Null
            Copy-Item -LiteralPath $file.Stage -Destination $file.Local -Force
        } else { Remove-Item -LiteralPath $file.Local -Force }
    }
    $state = @{ schema_version=1; repository=$Repository; branch=$Branch; commit=$version; files=$current; manifest_source=$snapshot.Source; updated_at=(Get-Date).ToUniversalTime().ToString('o') }
    $pendingState = Join-Path $runRoot 'state.json'
    [IO.File]::WriteAllText($pendingState, ($state | ConvertTo-Json -Depth 6), $utf8)
    if (Test-Path -LiteralPath $statePath) { [IO.File]::Replace($pendingState, $statePath, (Join-Path $runRoot 'previous-state.json')) }
    else { [IO.File]::Move($pendingState, $statePath) }
    $committed = $true
    Write-UpdateLog '增量更新完成。请重启地面后端并刷新网页。未重新安装 Python 或 MediaMTX。'
    if (@($downloads | Where-Object { $_.Path -in @('competition_backend/requirements.txt','competition_backend/pyproject.toml') }).Count) {
        Write-UpdateLog '依赖清单有变化。如新版需要新增依赖，请在项目目录运行 powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\tools\install_ground_station.ps1。'
    }
} catch {
    $failure = $_
    if (-not $committed) {
        for ($index = $applied.Count - 1; $index -ge 0; $index--) {
            $entry = $applied[$index]
            try {
                [void](Resolve-UpdatePath $Destination $entry.Path)
                if ($entry.Existed) { Copy-Item -LiteralPath $entry.Backup -Destination $entry.Local -Force }
                elseif (Test-Path -LiteralPath $entry.Local -PathType Leaf) { Remove-Item -LiteralPath $entry.Local -Force }
            } catch { Write-UpdateLog "自动恢复失败：$($entry.Path)。备份位置：$($entry.Backup)" }
        }
    }
    Write-UpdateLog ("更新失败：" + $failure.Exception.Message)
    [IO.File]::AppendAllText($script:updateLog, (($failure | Out-String) + $failure.ScriptStackTrace), $utf8)
    Write-UpdateLog "错误详情已保存：$script:updateLog"
    throw $failure
} finally { if ($lock) { $lock.Dispose() } }
