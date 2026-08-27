# 01 问题与根因

## 现象

Codex Desktop（Windows，MSIX 包）里 `mcp__node_repl__js` 等 node_repl 工具时有时无；在 `%USERPROFILE%\.codex\config.toml` 手工写：

```toml
[features]
js_repl = true
```

重启 Desktop 后会被静默改回 `js_repl = false`。

## 根因（来源机 26.818.3698.0 实锤）

Desktop 启动/同步 Browser Use 配置时，Electron 主进程代码（在 `app\resources\app.asar` 里）会：

1. 读取内置默认配置与覆盖表，其中有硬编码 `{"features.js_repl":!1}`；
2. 由同步函数 `_s({appServerConnection, appVersion, desktopFeatureAvailability, isPackaged, ...})` 构造一组 config edits；
3. edits 的 keypath 来自一个数组，真实构建中形如 `hs=[\`features.js_repl\`,\`mcp_servers.${Ue}\`,ri]`；
4. 通过 `sendAppServerRequest("config/batchWrite")` 以 `mergeStrategy:"replace"` 写回 config.toml。

可用于搜索定位的锚点字符串（都在 asar 内实测命中）：

- `features.js_repl`
- `config/batchWrite`
- `BrowserUseThreadConfig`
- `desktopFeatureAvailability`
- `mergeStrategy`

## 修法选择：过滤 keypath，而不是删逻辑

同一份代码里还有一个大 defaults 块，把 `features.plugins`、`features.shell_tool` 等十几项都写成 `!1`。因此：

- 不能全局替换 `!1` -> `!0`（误伤一大片）；
- 不能删 `_s` 或整个 batchWrite 调用（node_repl、computer-use、MCP servers 的同步都走这条路）；
- 正确做法：把 `features.js_repl` 从 keypath 数组里删掉（数组首元素 + 逗号，约 19 字节），其它元素原样保留。patch 后面向的 edit 集合自然不再包含这个 key。

## 重要认知：patch 不等于工具恢复

来源机 2026-08-22 实测：config.toml 里 `js_repl = false` 的会话中，`mcp__node_repl__js` 实际可用（执行 JS、连内置浏览器、开 tab、导航、截图全通）。说明：

- 工具暴露与否由 Desktop 与核心进程间的 capability 协商决定，不直接由 config.toml 的值决定；
- 被强制回写 false 是"配置被篡改"的正确性问题，patch 解决的是它；
- 若某台机器 patch 后工具仍缺失，进入第二阶段：定位 capability 判定（大概率在 Rust 核心 `app\resources\codex.exe`，而非 asar），不要继续在 js_repl 上打转。

## 关联已知问题

GitHub issue #29234：Desktop 重启会静默丢 config.toml 里的其它段落。因此验证时必须对**整个 config.toml** 做前后 diff，不能只盯 js_repl 一行。
