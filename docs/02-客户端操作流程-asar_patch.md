# 02 操作流程

脚本 `scripts/patch-js_repl.ps1` 已把下面 1-7 步自动化。手工执行或排错时参考本文件。

## 第 0 步：前置检查

- 安装包：`C:\Program Files\WindowsApps\OpenAI.Codex_<版本>_x64__2p2nqsd0c76g0`，取最新版本目录。
- 确认结构：`app\resources\app.asar`（约 287MB）、`app\Codex.exe`、`app\resources\codex.exe`（Rust 核心，约 297MB）。
- 检查 Electron fuses：如果 `EnableEmbeddedAsarIntegrityValidation` 开启，改过的 asar 会被拒绝加载，此时停下汇报，不要继续。可用 `npx @electron/fuses read --app <Codex.exe 路径>` 检查。

## 第 1 步：完整复制到可写目录 + 留档

```powershell
$ver  = '26.818.3698.0'   # 按实际版本
$work = "$env:LOCALAPPDATA\Codex-patched\$ver"
Copy-Item 'C:\Program Files\WindowsApps\OpenAI.Codex_<版本>_x64__2p2nqsd0c76g0\app' "$work\app" -Recurse
# 对 app.asar / Codex.exe / codex.exe 记录原包与副本的 SHA256，存 backup-hashes.json
```

## 第 2 步：解包 asar

```powershell
Copy-Item "$work\app\resources\app.asar" "$work\app\resources\app.asar.orig"   # 原样备份
# 关键：解包目标目录必须先删干净！asar extract 不清理目标目录，旧文件会混入。
Remove-Item "$work\work\asar-src" -Recurse -Force -ErrorAction SilentlyContinue
npx --yes @electron/asar extract "$work\app\resources\app.asar.orig" "$work\work\asar-src"
```

## 第 3 步：定位 patch 点

在 `work\asar-src` 下递归找含 `features.js_repl` 的 js 文件，逐处看上下文分类：

- **要 patch**：keypath 数组——`[` 后第一个元素是 `features.js_repl`，紧跟一个 `mcp_servers.` 开头的元素（反引号模板串或双引号串都可能）；
- **不要动**：`{"features.js_repl":!1}` defaults 表、settings 读取 `t["features.js_repl"]??!1`、env overlay 等。

每个 patch 点必须做**计数断言**（预期出现次数不符就中止），记录 patch 前后 SHA256 与代码片段。

## 第 4 步：重打包（最容易翻车的步骤）

`--unpack '*.node'` 的 glob 不会跨路径匹配，实测无效。必须用 `--unpack-dir` 逐目录声明。来源机 26.818.3698.0 的清单（本机要按实际解包结果核对）：

```
node_modules/@worklouder/device-kit-oai/node_modules/@serialport/bindings-cpp/build/Release
node_modules/@worklouder/device-kit-oai/node_modules/node-hid/build/Release
node_modules/better-sqlite3/build/Release
node_modules/better-sqlite3/lib
node_modules/better-sqlite3/node_modules/.bin
node_modules/node-pty/build/Release
node_modules/node-pty/lib
```

外加 `--unpack winpty-agent.exe`。

```powershell
npx --yes @electron/asar pack "$work\work\asar-src" "$work\app\resources\app.asar" `
  --unpack 'winpty-agent.exe' --unpack-dir '{<上面清单,逗号分隔>}'
```

打包后必须解析 asar 头部 JSON 核对 unpacked 标记：来源机预期 37 个 unpacked 条目、0 错配（核对逻辑见 verify 脚本）。

## 第 5 步：启动 patched 副本

```powershell
& "$work\app\ChatGPT.exe"   # 不是 Codex.exe：那是 windows_update_trampoline 更新器 stub，真正的 GUI 是 ChatGPT.exe（ProductName=Codex, OriginalFilename=chrome.exe，Codex 与 ChatGPT 桌面端已合并）
```

注意：从复制目录运行会脱离 MSIX 包身份，通知、协议注册等依赖包身份的功能可能退化，属预期。若无法启动，先定位真实入口与参数，设计最小可逆的启动方式（如快捷方式/shim），不要回头改系统安装包。

## 第 6 步：动态验证

见 `docs/03-验证与坑.md` 的验证清单。

## 第 7 步：留档与交付

- patch-log.json：每处 patch 的文件、Search/Replace、前后 SHA256、前后代码片段；
- backup-hashes.json：原包/副本关键文件哈希；
- 最终 app.asar 的大小与 SHA256；
- 向用户汇报：版本号、patch 点数量与位置、静态/动态验证结果、未完成事项。
