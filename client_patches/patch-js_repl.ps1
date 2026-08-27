<#
.SYNOPSIS
  Codex Desktop (MSIX) 综合修复与功能赋能补丁：
  1. Phase 1 (防篡改): 把 features.js_repl 从 config/batchWrite 的 keypath 数组中过滤掉。
  2. Phase 2 (能力赋能): 在全局特征仲裁入口 Er() 中强制开启桌面自动化能力 (inAppBrowserUse 等)。
  全程在 LOCALAPPDATA 副本上操作，不动 WindowsApps 原包。幂等，可重复执行。

.DESCRIPTION
  流程：定位安装目录 -> 复制 app 到 %LOCALAPPDATA%\Codex-patched\<ver> -> SHA256 留档
  -> 备份 app.asar.orig -> 干净解包 -> 调用结构化补丁执行器 patch-core.js (计数断言)
  -> 自动探测 unpack 目录并重打包 -> 校验 unpacked 标记 -> 输出验证提示。

.PARAMETER Version
  可选。指定 OpenAI.Codex 版本号；缺省自动取 WindowsApps 下最新版本目录。
#>
param(
  [string]$PackageRoot = 'C:\Program Files\WindowsApps',
  [string]$WorkRoot    = "$env:LOCALAPPDATA\Codex-patched",
  [string]$Version     = ''
)

$ErrorActionPreference = 'Stop'

# ---------- 1. 定位安装目录 ----------
$appx = Get-AppxPackage -Name 'OpenAI.Codex*' | Select-Object -First 1
$pkgPath = ''
if ($appx) {
  $pkgPath = $appx.InstallLocation
  if ($Version -eq '') { $Version = $appx.Version }
} elseif ($Version) {
  $found = Get-Item "$PackageRoot\OpenAI.Codex_${Version}_x64__*" -ErrorAction SilentlyContinue
  if ($found) { $pkgPath = $found.FullName }
} else {
  $found = Get-ChildItem $PackageRoot -Directory -Filter 'OpenAI.Codex_*_x64__*' -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
  if ($found) {
    $pkgPath = $found.FullName
    $Version = ($found.Name -split '_')[1]
  }
}

if (-not $pkgPath -or -not (Test-Path $pkgPath)) {
  throw "未找到 OpenAI.Codex 安装目录（$PackageRoot），请确认已安装 OpenAI Codex Desktop"
}
Write-Host "安装目录: $pkgPath"
Write-Host "版本: $Version"

$srcApp  = Join-Path $pkgPath 'app'
$srcAsar = Join-Path $srcApp 'resources\app.asar'
if (-not (Test-Path $srcAsar)) { throw "未找到 app.asar: $srcAsar" }

# ---------- 2. 复制 + 留档 ----------
$work    = Join-Path $WorkRoot $Version
$asarDst = "$work\app\resources\app.asar"
$asarOrig = "$work\app\resources\app.asar.orig"

if (-not (Test-Path $asarDst)) {
  Write-Host "复制 app 到 $work ..."
  New-Item -ItemType Directory -Force "$work\app" | Out-Null
  $rcArgs = @($srcApp, "$work\app", '/E', '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS')
  & robocopy @rcArgs
  # 确保关键大文件精确复制
  Copy-Item $srcAsar $asarDst -Force
  if ($LASTEXITCODE -gt 7) {
    throw "robocopy 复制失败，退出码: $LASTEXITCODE"
  }
} else {
  Write-Host "副本已存在: $work（如需基于原包重新同步，请先删除该目录后重跑）"
}

# 确保原始干净副本存在
if (-not (Test-Path $asarOrig)) {
  Copy-Item $srcAsar $asarOrig -Force
  Write-Host '已建立原始基准备份: app.asar.orig'
}

$hashes = @('resources\app.asar', 'Codex.exe', 'ChatGPT.exe', 'resources\codex.exe') | ForEach-Object {
  $rel = $_
  $o = Join-Path $srcApp $rel
  $c = Join-Path "$work\app" $rel
  if ($rel -eq 'resources\app.asar' -and (Test-Path $asarOrig)) {
    $c = $asarOrig
  }
  [pscustomobject]@{
    File       = "app\$rel"
    OrigSHA256 = (Get-FileHash $o -Algorithm SHA256).Hash
    CopySHA256 = (Get-FileHash $c -Algorithm SHA256).Hash
    Match      = ((Get-FileHash $o -Algorithm SHA256).Hash -eq (Get-FileHash $c -Algorithm SHA256).Hash)
  }
}
$hashes | ConvertTo-Json | Out-File "$work\backup-hashes.json" -Encoding utf8
$hashes | ForEach-Object { Write-Host "$($_.File)  Match=$($_.Match)" }
if ($hashes.Match -contains $false) { throw '副本原始基准哈希与原包不一致，中止' }

# ---------- 3. 干净解包 ----------
$extractDir = "$work\work\asar-src"
# 关键：asar extract 不清理目标目录，必须先删（防止旧构建文件混入）
Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue

# 使用 $srcAsar 进行解包（其同级存在完整 app.asar.unpacked 原生模块目录）
Write-Host "从原始原包 $srcAsar 解包到 $extractDir ..."
npx --yes @electron/asar extract $srcAsar $extractDir
if (-not (Test-Path "$extractDir\package.json")) { throw 'asar 解包失败' }
Write-Host "解包完成: $extractDir"

# ---------- 4. 执行结构化补丁 (Phase 1 防篡改 + Phase 2 强制赋能) ----------
$patchCoreScript = Join-Path $PSScriptRoot 'patch-core.js'
$logFile = "$work\patch-log.json"

Write-Host "调用结构化 Patch 执行器: $patchCoreScript ..."
& node $patchCoreScript $extractDir $logFile
if ($LASTEXITCODE -ne 0) {
  throw "patch-core.js 执行失败，退出码: $LASTEXITCODE"
}

# ---------- 5. 自动探测 unpack 目录并重打包 ----------
$nodeDirs = Get-ChildItem $extractDir -Recurse -Filter *.node |
  ForEach-Object { $_.Directory.FullName.Substring($extractDir.Length + 1).Replace('\', '/') } |
  Sort-Object -Unique
$extraDirs = @(
  'node_modules/better-sqlite3/lib',
  'node_modules/better-sqlite3/node_modules/.bin',
  'node_modules/node-pty/lib'
) | Where-Object { Test-Path (Join-Path $extractDir $_) }
$unpackDirs = @($nodeDirs + $extraDirs | Sort-Object -Unique)

$unpackArgs = @('--unpack', 'winpty-agent.exe', '--unpack-dir', ('{' + ($unpackDirs -join ',') + '}'))
Write-Host "重打包 (unpack-dir: $($unpackDirs.Count) 个目录) ..."
npx --yes @electron/asar pack $extractDir $asarDst @unpackArgs

$newHash = (Get-FileHash $asarDst -Algorithm SHA256).Hash
Write-Host "新 app.asar: $((Get-Item $asarDst).Length) bytes  SHA256: $newHash"

# ---------- 6. 校验 unpacked 标记 ----------
$bytes = [System.IO.File]::ReadAllBytes($asarDst)
$size  = [System.BitConverter]::ToUInt32($bytes, 12)
$json  = [System.Text.Encoding]::UTF8.GetString($bytes, 16, $size)
$j     = $json | ConvertFrom-Json
$script:bad = @(); $script:unpackedCount = 0
function Walk($node, $path) {
  foreach ($p in $node.files.PSObject.Properties) {
    $cp = "$path/$($p.Name)"
    if ($p.Value.PSObject.Properties.Name -contains 'files') { Walk $p.Value $cp }
    else {
      $props = @($p.Value.PSObject.Properties.Name)
      $hasU = $props -contains 'unpacked'
      if ($hasU) {
        $script:unpackedCount++
        if (-not $p.Value.unpacked) { $script:bad += "FALSE flag: $cp" }
      } elseif ($cp -match '\.node$|winpty-agent\.exe$|better-sqlite3/lib/|node-pty/lib/|better-sqlite3/node_modules/\.bin/') {
        $script:bad += "MISSING flag: $cp"
      }
    }
  }
}
Walk $j ''
Write-Host "unpacked entries: $script:unpackedCount   mismatches: $($script:bad.Count)"
$script:bad | Select-Object -First 10 | ForEach-Object { Write-Host $_ }
if ($script:bad.Count -gt 0) { throw 'unpacked 标记校验失败，检查 --unpack-dir 清单' }

Write-Host ''
Write-Host '== 补丁执行完成 =='
Write-Host '下一步：pwsh -File scripts\verify-js_repl.ps1 做静态验证，然后按 docs/03 做动态验证。'
Write-Host "启动 patched 副本: & `"$work\app\ChatGPT.exe`"  （注意是 ChatGPT.exe；Codex.exe 是更新器 stub，不会启动界面）"
