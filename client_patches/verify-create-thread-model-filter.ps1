<#
.SYNOPSIS
  静态验证 create_thread model filter 补丁的最终 app.asar。
#>
param(
  [string]$WorkRoot = "$env:LOCALAPPDATA\Codex-patched",
  [string]$Version = '26.818.3698.0'
)

$ErrorActionPreference = 'Stop'
$work = Join-Path $WorkRoot $Version
$asar = Join-Path $work 'app\resources\app.asar'
$backup = Join-Path $work 'work\app.asar.before-create-thread-model-filter'

foreach ($file in @($asar, $backup)) {
  if (-not (Test-Path -LiteralPath $file)) { throw "缺少文件: $file" }
}

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

$fail = 0
function Check([string]$Label, [bool]$Ok, $Detail) {
  if ($Ok) { Write-Host "[PASS] $Label  $Detail" }
  else { Write-Host "[FAIL] $Label  $Detail"; $script:fail++ }
}

$oldFilter = 'let i=e.get(kza);return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`?i.availableModels.has(e.model):!e.hidden)}'
$newFilter = 'let i=e.get(kza),a=!1;try{a=wRt(S_((await Hg(e,t).sendRequest(`config/read`,{includeLayers:!1,cwd:null})).config))}catch{}return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`&&!a?i.availableModels.has(e.model):!e.hidden)}'
$newFunction = 'async function Q3o(e,t){let n;try{n=await Kni(n=>Hg(e,t).sendRequest(`model/list`,{...n},{priority:`critical`}))}catch{return null}if(t!==`local`)return n.filter(e=>!e.hidden);let r;try{r=await kri(e,t)}catch{return n.filter(e=>!e.hidden)}let i=e.get(kza),a=!1;try{a=wRt(S_((await Hg(e,t).sendRequest(`config/read`,{includeLayers:!1,cwd:null})).config))}catch{}return n.filter(e=>i.useHiddenModels&&r!==`amazonBedrock`&&!a?i.availableModels.has(e.model):!e.hidden)}'
$validator = 'function $Yo(e,t,n){let r=n?.find(t=>t.model===e);if(r==null)return t==null&&n==null?null:t==null?`${y_} could not validate model "${e}". Use a model listed in the tool description, or omit model.`:`${y_} could not validate reasoning effort "${t}" for model "${e}". Use a model and reasoning combination listed in the tool description, or omit thinking.`;'
$schemaCustomBypass = 'a&&!r&&t!==`amazonBedrock`?n.has(i.model):!i.hidden'

$oldInPatched = Count-InFile $asar $oldFilter
$oldInBackup = Count-InFile $backup $oldFilter
$newInPatched = Count-InFile $asar $newFilter
$newInBackup = Count-InFile $backup $newFilter
$newFnCount = Count-InFile $asar $newFunction
$validatorCount = Count-InFile $asar $validator
$schemaBypassCount = Count-InFile $asar $schemaCustomBypass

Check '旧过滤形态已从 asar 消失' ($oldInPatched -eq 0) "patched=$oldInPatched"
Check '旧过滤形态在备份中恰好一次' ($oldInBackup -eq 1) "backup=$oldInBackup"
Check '新过滤形态在 asar 中恰好一次' ($newInPatched -eq 1) "patched=$newInPatched"
Check '新过滤形态未污染备份' ($newInBackup -eq 0) "backup=$newInBackup"
Check '补丁后的完整 Q3o 恰好一次' ($newFnCount -eq 1) "count=$newFnCount"
Check '$Yo 校验器未被修改' ($validatorCount -eq 1) "count=$validatorCount"
Check 'schema 层自定义 provider 旁路仍存在' ($schemaBypassCount -eq 1) "count=$schemaBypassCount"

$bytes = [System.IO.File]::ReadAllBytes($asar)
$headerOk = $true
$headerDetail = ''
$unpackedCount = 0
$bad = @()
try {
  $headerSize = [System.BitConverter]::ToUInt32($bytes, 12)
  if ($headerSize -le 0 -or (16 + $headerSize) -gt $bytes.Length) { throw 'header size invalid' }
  $header = [System.Text.Encoding]::UTF8.GetString($bytes, 16, $headerSize) | ConvertFrom-Json
  function Walk($node, [string]$path) {
    foreach ($property in $node.files.PSObject.Properties) {
      $childPath = if ($path) { "$path/$($property.Name)" } else { $property.Name }
      if ($property.Value.PSObject.Properties.Name -contains 'files') {
        Walk $property.Value $childPath
      } else {
        $properties = @($property.Value.PSObject.Properties.Name)
        if ($properties -contains 'unpacked') {
          $script:unpackedCount++
          if (-not $property.Value.unpacked) { $script:bad += "FALSE flag: $childPath" }
        } elseif ($childPath -match '\.node$|winpty-agent\.exe$|better-sqlite3/lib/|node-pty/lib/|better-sqlite3/node_modules/\.bin/') {
          $script:bad += "MISSING flag: $childPath"
        }
      }
    }
  }
  Walk $header ''
  $headerDetail = "unpacked=$unpackedCount mismatches=$($bad.Count)"
} catch {
  $headerOk = $false
  $headerDetail = $_.Exception.Message
}
Check 'asar header 可解析' ($headerOk) $headerDetail
Check 'unpacked 标记无错配' ($headerOk -and $bad.Count -eq 0) $headerDetail

& npx --yes @electron/asar list $asar | Out-Null
Check '@electron/asar list 可解析产物' ($LASTEXITCODE -eq 0) "exit=$LASTEXITCODE"

$backupHash = (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash
$asarHash = (Get-FileHash -LiteralPath $asar -Algorithm SHA256).Hash
Check '备份/补丁产物哈希已生成' ($backupHash.Length -eq 64 -and $asarHash.Length -eq 64) "backup=$backupHash asar=$asarHash"

Write-Host ''
if ($fail -eq 0) {
  Write-Host '静态验证全部通过。'
} else {
  Write-Host "$fail 项静态验证未通过。"
  exit 1
}
