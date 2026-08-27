# install_autostart.ps1 — 注册登录自启计划任务（登录时以 pythonw 后台启动桥接）
# 用法: powershell -ExecutionPolicy Bypass -File install_autostart.ps1
$pyw = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = (Get-Command pythonw.exe).Source }
$script = Join-Path $env:USERPROFILE '.codex\tools\kimi_bridge.py'
if (-not (Test-Path $script)) { Write-Error "桥接未部署: $script 不存在，先按 README 部署"; exit 1 }

$action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName 'CodexKimiBridge' -Action $action -Trigger $trigger -Settings $settings `
  -Description 'Codex<->Kimi normalization bridge on 127.0.0.1:15731 (logs to ~/.codex/tools/kimi_bridge.log)' | Select-Object TaskName, State
Write-Host "已注册。卸载: Unregister-ScheduledTask -TaskName 'CodexKimiBridge' -Confirm:`$false"
