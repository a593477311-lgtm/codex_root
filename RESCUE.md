# RESCUE.md — Codex 被改崩后的救回手册（给 Codex 以外的 agent / 人类）

> 使用场景：某个 agent（通常是另一台机器上的 Codex 自己）按本目录的方案 patch 时出了岔子，Codex Desktop 起不来、行为异常、或配置损坏。救回工作不要指望 Codex 本身能跑——本手册全部只用 PowerShell + 文件操作，任何 agent（Claude Code、Cursor、终端里的人类）都能执行。
>
> 核心原则：这套方案的设计本来就是"原包只读、改动全在副本"，所以绝大多数"改崩了"只需要删掉副本目录就能满血复原。先判断崩的是哪一层，再按下面对号入座。

## 第 0 步：先判断崩在哪一层

| 症状 | 崩的层 | 去第几节 |
|---|---|---|
| patched 副本（%LOCALAPPDATA%\Codex-patched\... 里的）起不来或报错 | 副本层 | 第 1 节 |
| 官方安装的 Codex（开始菜单/原快捷方式）起不来 | 原包层 | 第 2 节 |
| Codex 能开但配置丢了/行为怪（登录还在、设置不对） | 配置层 | 第 3 节 |
| 都不确定 | 从第 1 节开始顺一遍 | — |

先确认现场，任何修复前把现状拍个快照（有错就贴给用户）：

```powershell
# 装了哪些版本
Get-AppxPackage -Name 'OpenAI.Codex*' | Select-Object Name, Version, InstallLocation, Status
# 有没有残留进程在占用文件
Get-Process | Where-Object { $_.Name -match 'codex' } | Select-Object Name, Id, Path
# 副本目录现状
Get-ChildItem "$env:LOCALAPPDATA\Codex-patched" -ErrorAction SilentlyContinue
# 配置目录现状
Get-ChildItem "$env:USERPROFILE\.codex" -ErrorAction SilentlyContinue | Select-Object Name, Length, LastWriteTime
```

操作前先杀掉所有 Codex 进程，避免文件占用：

```powershell
Get-Process | Where-Object { $_.Path -like '*OpenAI.Codex*' -or $_.Path -like '*Codex-patched*' } | Stop-Process -Force
```

## 第 1 节：副本层崩了（最常见，30 秒解决）

症状：从 `%LOCALAPPDATA%\Codex-patched\<版本>\app\ChatGPT.exe`（真正的 GUI 入口；Codex.exe 是更新器 stub，打不开是正常的，不算故障）启动失败、白屏、报 asar 错误。这不影响官方安装。直接：

```powershell
# 1. 删掉崩掉的副本（整个版本目录）
Remove-Item "$env:LOCALAPPDATA\Codex-patched\<版本>" -Recurse -Force
# 2. 用官方安装正常启动 Codex（开始菜单即可）
# 3. 如果还想再试 patch：重跑 scripts\patch-js_repl.ps1（副本已删，脚本会从头复制）
```

如果用户想保留现场给后续排查，先改名而不是删除：

```powershell
Rename-Item "$env:LOCALAPPDATA\Codex-patched\<版本>" "<版本>.broken-$(Get-Date -Format yyyyMMdd-HHmmss)"
```

## 第 2 节：原包层崩了（不该发生，但按这个救）

症状：官方 Codex 本体起不来。本方案的红线就是不动 `C:\Program Files\WindowsApps\OpenAI.Codex_*`，如果真被动了，不要尝试手工修文件（MSIX 有签名校验，手工修也修不好），直接走包管理器：

```powershell
# 方案 A：重注册（包还在但状态坏了时先试这个，最快）
Get-AppxPackage -Name 'OpenAI.Codex*' | ForEach-Object {
  Add-AppxPackage -Register "$($_.InstallLocation)\AppxManifest.xml" -DisableDevelopmentMode
}

# 方案 B：重置（Windows 11 支持，会清应用数据但保留包）
Get-AppxPackage -Name 'OpenAI.Codex*' | Reset-AppxPackage

# 方案 C：彻底重装（最稳）
winget uninstall --id OpenAI.Codex   # 或在 设置 > 应用 里卸载
winget install --id OpenAI.Codex     # 或从 Microsoft Store 重装
```

注意：卸载/重置一般不动 `%USERPROFILE%\.codex`（配置、会话、登录态都在用户目录），但动手前仍建议按第 3 节先备份 `.codex` 目录，保险。

重装完成后验证：

```powershell
Get-AppxPackage -Name 'OpenAI.Codex*' | Select-Object Version, Status
# 然后从开始菜单正常启动一次 Codex，确认能开新任务
```

## 第 3 节：配置层崩了（config.toml 损坏/丢失）

症状：Codex 能启动但行为异常，或 `%USERPROFILE%\.codex\config.toml` 内容不对。

先备份现状再动手（哪怕它已经是坏的，也可能有线索）：

```powershell
$stamp = Get-Date -Format yyyyMMdd-HHmmss
Copy-Item "$env:USERPROFILE\.codex\config.toml" "$env:USERPROFILE\.codex\config.toml.broken-$stamp" -ErrorAction SilentlyContinue
```

找可用的备份恢复（按优先级）：

```powershell
# 1. patch 流程要求的"动态验证前整文件备份"（如果当时按 docs/03 做了）
Get-ChildItem "$env:USERPROFILE\.codex" -Filter 'config.toml*' | Sort-Object LastWriteTime -Descending
# 2. 用户自己可能有的其它备份位置（OneDrive/版本库等），问用户
```

没有备份时的最小重建（注意：已知 issue #29234——Desktop 重启可能静默丢 config.toml 段落，所以重建后重启一次再 diff 检查）：

```toml
# %USERPROFILE%\.codex\config.toml 最小可用骨架，按用户实际模型/需求补
model = "gpt-5.3-codex"

[features]
js_repl = true
```

重建后：启动 Codex -> 开一个 projectless 任务 -> 确认 `mcp__node_repl__js` 在工具列表里 -> 重启 Codex -> diff config.toml 确认段落没丢。

## 第 4 节：验证"救回来了"

按顺序确认：

```powershell
# 1. 包状态健康
Get-AppxPackage -Name 'OpenAI.Codex*' | Select-Object Version, Status   # Status 应为 Ok
# 2. 残骸已清
Test-Path "$env:LOCALAPPDATA\Codex-patched"   # False 或只剩确认无用的 .broken 目录
```

第 3、4 条需要在 Codex UI 里人工确认：3. 从官方入口（开始菜单）启动 Codex 能正常开新任务；4. 新任务里工具正常（node_repl / 内置浏览器）。agent 做完 1、2 条后提示用户检查。四条全绿 = 救回完成。

## 第 5 节：向用户汇报什么

- 崩的是哪一层、怎么判定的（快照输出）；
- 做了什么处置（删了/改名了什么目录，重装了什么版本）；
- 有没有数据损失：重点说明 `.codex` 目录（会话记录、登录态、配置）是否完整；
- 如果要继续原来的 patch 任务：建议先复盘上次崩的原因（对照 docs/03-验证与坑.md 的六个坑），再重跑脚本，不要直接再试一次碰运气。

## 附：这个方案为什么"理论上不容易崩"

- 所有修改发生在 `%LOCALAPPDATA%\Codex-patched\<版本>` 的副本上，WindowsApps 原包全程只读；
- patch 只删 keypath 数组首元素（约 19 字节），有计数断言，断言不过会中止；
- 重打包产物先静态验证（verify 脚本），不过就不进入启动阶段；
- 最坏情况就是"副本是个坏副本"，官方安装毫发无损。

如果以上原则被违反了（比如有人直接改了 WindowsApps），那不是方案的问题，按第 2 节重装即可。
