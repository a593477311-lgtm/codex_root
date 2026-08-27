# 来源机实测证据（Windows，OpenAI.Codex 26.818.3698.0，2026-08-21/22）

供对照用。注意：以下变量名、文件名、哈希只对这一个构建有效，其它机器/版本必须重新定位。

## 安装与备份哈希

| 文件 | SHA256 | 说明 |
|---|---|---|
| app\resources\app.asar（原包=副本） | 1EB70E2AA26F2408A3E65817F0974E137B1A7FF6E52E43A184154BD4DB2074D1 | 286,838,157 字节；与 WindowsApps 原包逐字节一致 |
| app\Codex.exe | BCA3F26C75819910468D2AC7DB2C91AA560BAB557FBFE1D06175A316F344847F | Electron 壳 |
| app\resources\codex.exe | F0626BFD231D04D7D40FD5CABF4E614506474A3267142CF7623CCD135530899D | Rust 核心（~297MB） |
| patch 后 app.asar | AC1EE54E432C6B6AC53BE434F51665129B19FE35B0D56C2FA4A426E51D952D08 | 286,291,252 字节 |

## 本构建的真实 patch 点（1 处）

文件：`.vite\build\main-B6Z1yw33.js`（本构建主 bundle，2.6MB）

```
patch 前: hs=[`features.js_repl`,`mcp_servers.${Ue}`,ri]
patch 后: hs=[`mcp_servers.${Ue}`,ri]
```

patch 前 SHA256 `CC4F32A1F312F5C67DAD78E520825950A7FB0031587B2D8EA91873BE8408FF50`，
patch 后 SHA256 `6766634A0808EAC55AC401FB127F3B7360163179109913774EC3ED48E13E976E`，
长度变化 -19。

上下文（patch 后）：
`...function ms(e){return e==="mainFrame"||e==="subFrame"}var hs=[` + "`mcp_servers.${Ue}`,ri],gs=[an,`NODE_REPL_TRUSTED_CODE_PATHS`];async function _s({appServerConnection...`"

## 最终 asar 静态验证输出（2026-08-22 复核）

```
app.asar(patched) : features.js_repl 命中行 3   old-hs 无   patched-hs 1
app.asar.orig     : features.js_repl 命中行 4   old-hs 1   patched-hs 无
orig batchWrite: 4   new batchWrite: 4
orig mergeStrategy replace: 6   new mergeStrategy replace: 6
sync fn _s present: 1   BrowserUseThreadConfig present: 1
unpacked entries: 37 (expect 37)   mismatches: 0
```

## 重打包 unpack 清单（本构建）

```
--unpack winpty-agent.exe
--unpack-dir {
  node_modules/@worklouder/device-kit-oai/node_modules/@serialport/bindings-cpp/build/Release,
  node_modules/@worklouder/device-kit-oai/node_modules/node-hid/build/Release,
  node_modules/better-sqlite3/build/Release,
  node_modules/better-sqlite3/lib,
  node_modules/better-sqlite3/node_modules/.bin,
  node_modules/node-pty/build/Release,
  node_modules/node-pty/lib
}
```

## 污染事件记录（为什么文档反复强调"删干净再解包"）

`work\asar-src` 曾混入另一个构建的 `main-B5hSiS5A.js`（16.4MB，Ro/dh/ua 命名）与
`app-initial-DlR7BIbo.js`，导致中途一度把 Ro/dh/ua 当成真实 patch 目标。复核证明：
原包 asar 里只有 4 处 js_repl、keypath 数组仅 hs 一种；16.4MB 文件从未存在于原包中。
教训：任何 patch 前后都要拿 app.asar.orig 做基准对照，验证必须打在最终打包产物上。

## 2026-08-22 补充实测：js_repl=false 时工具依然可用

在 config.toml 为 `js_repl = false` 的活跃会话中：

- `mcp__node_repl__js` 执行成功（读到 cwd/tmpDir 等环境信息）；
- 内置浏览器插件全链路通过：连接 iab -> `tabs.new()` -> 导航 example.com -> 读标题 "Example Domain" -> 截图成功（14,967 字节）。

结论：本构建中工具暴露不受 config.toml 该值门控。patch 的价值是阻止配置被篡改（并配合 #29234 防止丢段落），不是恢复工具的必要条件。

## 未完成事项

- 未从副本目录启动过 patched Codex（动态验证未做）；
- fuses（EnableEmbeddedAsarIntegrityValidation）未检查，启动验证前先查；
- MSIX 自动更新后需重跑 patch 脚本。
