<#
.SYNOPSIS
  为补丁副本 app.asar 添加 create_thread 运行时自定义 provider 过滤旁路。

.DESCRIPTION
  只处理 Codex-patched 副本中的 app.asar：先提取、唯一锚点断言、Node 精确替换、
  保留 unpack 标记重打包、原子换入。不触碰 WindowsApps 原版，不重启应用。
  脚本幂等：补丁形态已存在时直接跳过。
#>
param(
  [string]$WorkRoot = "$env:LOCALAPPDATA\Codex-patched",
  [string]$Version = '26.818.3698.0'
)

$ErrorActionPreference = 'Stop'

$work = Join-Path $WorkRoot $Version
$asar = Join-Path $work 'app\resources\app.asar'
$backup = Join-Path $work 'work\app.asar.before-create-thread-model-filter'
$logPath = Join-Path $work 'create-thread-model-filter-patch-log.json'
$extractDir = Join-Path $work 'work\asar-src-create-thread-model-filter'
$patchedAsar = Join-Path $work 'work\app.asar.create-thread-model-filter.patching'
$patchExecutor = Join-Path $PSScriptRoot 'patch-create-thread-model-filter.js'

foreach ($item in @($asar, $patchExecutor)) {
  if (-not (Test-Path -LiteralPath $item)) { throw "缺少文件: $item" }
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup) | Out-Null

function Count-InFile([string]$File, [string]$Text) {
  $content = [System.IO.File]::ReadAllText($File)
  $count = 0
  $pos = 0
  while (($pos = $content.IndexOf($Text, $pos, [System.StringComparison]::Ordinal)) -ne -1) {
    $count++
    $pos += $Text.Length
  }
  return $count
}

function Get-AsarHeader([string]$File) {
  $bytes = [System.IO.File]::ReadAllBytes($File)
  if ($bytes.Length -lt 16) { throw "asar 文件过小: $File" }
  $headerSize = [System.BitConverter]::ToUInt32($bytes, 12)
  if ($headerSize -le 0 -or (16 + $headerSize) -gt $bytes.Length) {
    throw "asar header 大小非法: $headerSize"
  }
  $headerText = [System.Text.Encoding]::UTF8.GetString($bytes, 16, $headerSize)
  return $headerText | ConvertFrom-Json
}

function Get-UnpackDirs([string]$Root) {
  $nodeDirs = Get-ChildItem -LiteralPath $Root -Recurse -Filter '*.node' -File |
    ForEach-Object {
      $_.Directory.FullName.Substring($Root.Length + 1).Replace('\', '/')
    } |
    Sort-Object -Unique
  $extraDirs = @(
    'node_modules/better-sqlite3/lib',
    'node_modules/better-sqlite3/node_modules/.bin',
    'node_modules/node-pty/lib'
  ) | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) }
  return @($nodeDirs + $extraDirs | Sort-Object -Unique)
}

function Get-JsFiles([string]$Root) {
  Get-ChildItem -LiteralPath $Root -Recurse -Filter '*.js' -File |
    Where-Object { $_.FullName -notmatch '\\node_modules\\' }
}

$oldCount = Count-InFile $asar 'let i=e.get(kza);return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`?i.availableModels.has(e.model):!e.hidden)}'
$newCount = Count-InFile $asar 'let i=e.get(kza),a=!1;try{a=wRt(S_((await Hg(e,t).sendRequest(`config/read`,{includeLayers:!1,cwd:null})).config))}catch{}return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`&&!a?i.availableModels.has(e.model):!e.hidden)}'

if ($oldCount -eq 0 -and $newCount -eq 1) {
  Get-AsarHeader $asar | Out-Null
  Write-Host "[SKIP] create_thread model filter 补丁已存在: $asar"
  if (Test-Path -LiteralPath $logPath) {
    Get-Content -LiteralPath $logPath -Raw | Write-Output
  }
  exit 0
}

if ($oldCount -ne 1 -or $newCount -ne 0) {
  throw "asar 锚点不唯一或形态异常: old=$oldCount new=$newCount"
}

$beforeHash = (Get-FileHash -LiteralPath $asar -Algorithm SHA256).Hash
if (Test-Path -LiteralPath $backup) {
  $backupHash = (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash
  if ($backupHash -ne $beforeHash) {
    throw "已有备份与当前 asar 不一致；拒绝覆盖。backup=$backupHash current=$beforeHash"
  }
} else {
  Copy-Item -LiteralPath $asar -Destination $backup
  $backupHash = (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash
  if ($backupHash -ne $beforeHash) { throw '备份哈希校验失败' }
}

Remove-Item -LiteralPath $extractDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $patchedAsar -Force -ErrorAction SilentlyContinue
Write-Host "提取当前 app.asar 到 $extractDir ..."
npx --yes @electron/asar extract $asar $extractDir
if ($LASTEXITCODE -ne 0) { throw "asar extract 失败: $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath (Join-Path $extractDir 'package.json'))) { throw 'asar 解包后缺少 package.json' }

$jsFiles = @(Get-JsFiles $extractDir)
$targetHits = @()
foreach ($file in $jsFiles) {
  $count = Count-InFile $file.FullName 'let i=e.get(kza);return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`?i.availableModels.has(e.model):!e.hidden)}'
  if ($count -ne 0) { $targetHits += [pscustomobject]@{ File = $file; Count = $count } }
}
if ($targetHits.Count -ne 1 -or $targetHits[0].Count -ne 1) {
  throw "源码锚点不唯一: files=$($targetHits.Count) counts=$($targetHits -join ',')"
}
$sourceFile = $targetHits[0].File
$sourceRelative = $sourceFile.FullName.Substring($extractDir.Length + 1).Replace('\', '/')
$sourceHashBefore = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash

Write-Host "执行唯一锚点补丁: $sourceRelative"
node $patchExecutor $extractDir
if ($LASTEXITCODE -ne 0) { throw "补丁执行器失败: $LASTEXITCODE" }
$sourceHashAfter = (Get-FileHash -LiteralPath $sourceFile.FullName -Algorithm SHA256).Hash

$unpackDirs = Get-UnpackDirs $extractDir
$packArgs = @('--yes', '@electron/asar', 'pack', $extractDir, $patchedAsar, '--unpack', 'winpty-agent.exe')
if ($unpackDirs.Count -gt 0) {
  $packArgs += @('--unpack-dir', ('{' + ($unpackDirs -join ',') + '}'))
}
Write-Host "重打包 app.asar (unpack-dir=$($unpackDirs.Count)) ..."
& npx @packArgs
if ($LASTEXITCODE -ne 0) { throw "asar pack 失败: $LASTEXITCODE" }

Get-AsarHeader $patchedAsar | Out-Null
$patchedOldCount = Count-InFile $patchedAsar 'let i=e.get(kza);return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`?i.availableModels.has(e.model):!e.hidden)}'
$patchedNewCount = Count-InFile $patchedAsar 'let i=e.get(kza),a=!1;try{a=wRt(S_((await Hg(e,t).sendRequest(`config/read`,{includeLayers:!1,cwd:null})).config))}catch{}return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`&&!a?i.availableModels.has(e.model):!e.hidden)}'
if ($patchedOldCount -ne 0 -or $patchedNewCount -ne 1) {
  throw "打包产物锚点校验失败: old=$patchedOldCount new=$patchedNewCount"
}

Move-Item -LiteralPath $patchedAsar -Destination $asar -Force
$afterHash = (Get-FileHash -LiteralPath $asar -Algorithm SHA256).Hash

[pscustomobject]@{
  timestamp = (Get-Date).ToString('o')
  status = 'patched'
  asarPath = $asar
  backupPath = $backup
  backupSHA256 = $backupHash
  asarSHA256Before = $beforeHash
  asarSHA256After = $afterHash
  sourceRelativePath = $sourceRelative
  sourceSHA256Before = $sourceHashBefore
  sourceSHA256After = $sourceHashAfter
  oldAnchorCount = 0
  newAnchorCount = 1
  unpackDirCount = $unpackDirs.Count
} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $logPath -Encoding utf8

Write-Host ''
Write-Host '== create_thread model filter 补丁完成 =='
Write-Host "backup : $backup"
Write-Host "before : $beforeHash"
Write-Host "after  : $afterHash"
