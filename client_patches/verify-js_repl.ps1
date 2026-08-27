<#
.SYNOPSIS
  对 patch 后的 app.asar 做全量静态验证（直接验最终打包产物，不看解包源目录）。

.DESCRIPTION
  对比 app.asar 与 app.asar.orig：
  1. Phase 1 (防篡改):
     - features.js_repl 出现次数应当减少（减少量 = 被过滤的 keypath 数组数）；
     - keypath 数组旧形态应消失、新形态（首元素即 mcp_servers.）应存在；
  2. Phase 2 (能力赋能):
     - 新 Er 强制赋能特征串恰好出现 1 次；
     - 旧 Er 函数体特征串 (Dev 环境限制判定) 0 命中；
  3. 核心链路与原生模块:
     - config/batchWrite 调用数、mergeStrategy:"replace" 数、同步函数存在性必须不变；
     - asar 头部 unpacked 标记复核（零错配）。
#>
param(
  [string]$WorkRoot = "$env:LOCALAPPDATA\Codex-patched",
  [string]$Version  = ''
)

$ErrorActionPreference = 'Stop'

if ($Version -eq '') {
  $Version = (Get-ChildItem $WorkRoot -Directory | Sort-Object Name -Descending | Select-Object -First 1).Name
}
$work     = Join-Path $WorkRoot $Version
$asar     = "$work\app\resources\app.asar"
$asarOrig = "$work\app\resources\app.asar.orig"
foreach ($f in @($asar, $asarOrig)) { if (-not (Test-Path $f)) { throw "缺少文件: $f（先跑 patch-js_repl.ps1）" } }

function Count-InFile($file, [string]$pattern, [switch]$Fixed) {
  $text = [System.IO.File]::ReadAllText($file)
  if ($Fixed) {
    $c = 0; $pos = 0
    while (($pos = $text.IndexOf($pattern, $pos, [System.StringComparison]::Ordinal)) -ne -1) {
      $c++; $pos += $pattern.Length
    }
    return $c
  }
  return [regex]::Matches($text, $pattern).Count
}

$fail = 0
function Check($label, [bool]$ok, $detail) {
  if ($ok) { Write-Host "[PASS] $label  $detail" } else { Write-Host "[FAIL] $label  $detail"; $script:fail++ }
}

Write-Host "== 静态验证 $work =="; Write-Host ''

# 1. js_repl 出现次数对比 (Phase 1)
$occNew  = Count-InFile $asar      'features\.js_repl'
$occOrig = Count-InFile $asarOrig  'features\.js_repl'
Check 'js_repl 出现次数减少' ($occNew -lt $occOrig) "orig=$occOrig new=$occNew"

# 2. 旧 keypath 数组应消失。结构模式：数组首元素 features.js_repl + 紧跟 mcp_servers.
$oldArray = '\[[`"' + "'" + ']features\.js_repl[`"' + "'" + ']\s*,\s*[`"' + "'" + ']mcp_servers\.'
$oldNew   = Count-InFile $asar     $oldArray
$oldOrig  = Count-InFile $asarOrig $oldArray
Check '旧 keypath 数组已消失' ($oldNew -eq 0 -and $oldOrig -ge 1) "orig=$oldOrig new=$oldNew"

# 3. patch 后数组存在（首元素直接是 mcp_servers.）
$patchedArray = '\[[`"' + "'" + ']mcp_servers\.'
$patchedNew   = Count-InFile $asar $patchedArray
Check 'patch 后数组存在' ($patchedNew -ge 1) "new=$patchedNew"

# 4. Phase 2 Er 强制赋能验证
$erFeatureString = 'inAppBrowserUse:!0,inAppBrowserUseAllowed:!0,browserPane:!0,externalBrowserUse:!0,externalBrowserUseAllowed:!0,computerUse:!0,computerUseNodeRepl:!0'
$erCountNew  = Count-InFile $asar $erFeatureString -Fixed
$erCountOrig = Count-InFile $asarOrig $erFeatureString -Fixed
Check '新 Er 赋能特征串恰好出现 1 次' ($erCountNew -eq 1 -and $erCountOrig -eq 0) "orig=$erCountOrig new=$erCountNew"

# 旧 Er 特征判定 (Dev 限制门控) 应消失
$oldErDevGate = 'CODEX_ELECTRON_ENABLE_WINDOWS_COMPUTER_USE===`1`'
$oldErDevPattern = 'CODEX_ELECTRON_ENABLE_WINDOWS_COMPUTER_USE===[`''"]1[`''"]\?[^;]+:i,\w+=t===a\.a\.Dev'
$oldErDevMatches = Count-InFile $asar $oldErDevPattern
Check '旧 Er 门控特征已完全消除' ($oldErDevMatches -eq 0) "newMatches=$oldErDevMatches"

# 5. 关键业务逻辑不变性
$batchWrite = 'config/batchWrite'
$bwOrig = Count-InFile $asarOrig $batchWrite -Fixed
$bwNew  = Count-InFile $asar $batchWrite -Fixed
Check 'config/batchWrite 出现数不变' ($bwNew -eq $bwOrig) "orig=$bwOrig new=$bwNew"

$mergeReplace = 'mergeStrategy:`replace`'
$mrOrig = Count-InFile $asarOrig $mergeReplace -Fixed
$mrNew  = Count-InFile $asar $mergeReplace -Fixed
if ($mrOrig -eq 0) {
  $mergeReplace = 'mergeStrategy:"replace"'
  $mrOrig = Count-InFile $asarOrig $mergeReplace -Fixed
  $mrNew  = Count-InFile $asar $mergeReplace -Fixed
}
Check 'mergeStrategy replace 数不变' ($mrNew -eq $mrOrig -and $mrOrig -ge 1) "orig=$mrOrig new=$mrNew"

$syncFn = 'async function vs({appServerConnection'
$sfOrig = Count-InFile $asarOrig $syncFn -Fixed
$sfNew  = Count-InFile $asar $syncFn -Fixed
if ($sfOrig -eq 0) {
  $syncFn = 'async function _s({appServerConnection'
  $sfOrig = Count-InFile $asarOrig $syncFn -Fixed
  $sfNew  = Count-InFile $asar $syncFn -Fixed
}
Check '同步函数 (vs/_s) 仍在' ($sfNew -ge 1 -and $sfNew -eq $sfOrig) "orig=$sfOrig new=$sfNew"

$btOrig = Count-InFile $asarOrig 'BrowserUseThreadConfig' -Fixed
$btNew  = Count-InFile $asar 'BrowserUseThreadConfig' -Fixed
Check 'BrowserUseThreadConfig 仍在' ($btNew -ge 1 -and $btNew -eq $btOrig) "orig=$btOrig new=$btNew"

# 6. unpacked 标记复核
$bytes = [System.IO.File]::ReadAllBytes($asar)
$size  = [System.BitConverter]::ToUInt32($bytes, 12)
$j     = [System.Text.Encoding]::UTF8.GetString($bytes, 16, $size) | ConvertFrom-Json
$script:bad = @(); $script:unpackedCount = 0
function Walk2($node, $path) {
  foreach ($p in $node.files.PSObject.Properties) {
    $cp = "$path/$($p.Name)"
    if ($p.Value.PSObject.Properties.Name -contains 'files') { Walk2 $p.Value $cp }
    else {
      $props = @($p.Value.PSObject.Properties.Name)
      if ($props -contains 'unpacked') {
        $script:unpackedCount++
        if (-not $p.Value.unpacked) { $script:bad += "FALSE flag: $cp" }
      } elseif ($cp -match '\.node$|winpty-agent\.exe$|better-sqlite3/lib/|node-pty/lib/|better-sqlite3/node_modules/\.bin/') {
        $script:bad += "MISSING flag: $cp"
      }
    }
  }
}
Walk2 $j ''
Check 'unpacked 标记无错配' ($script:bad.Count -eq 0) "unpacked=$script:unpackedCount mismatches=$($script:bad.Count)"
$script:bad | Select-Object -First 10 | ForEach-Object { Write-Host "  $_" }

Write-Host ''
if ($fail -eq 0) {
  Write-Host '静态验证全部通过！可安全启动 patched 副本进行动态验证。'
} else {
  Write-Host "$fail 项未通过。请先排查，不要启动 patched 副本。"
  exit 1
}
