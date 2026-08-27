# 04 真因：tool_search 载荷与本地代理（5229 调查终章）

> 时间线：2026-08-23 怀疑 Codex 5229 门控 → 打 Electron patch（本包 01-03 文档）→ 2026-08-24 证据链闭合，**根因不在 Codex 任何版本，而在本机 Antigravity Tools 代理（127.0.0.1:8045）不透传 Responses API 的自定义工具类型**。本文档是最终结论与修复方案。

## 症状（精确的，不是"工具没装"）

- codex.exe 正常拉起 node_repl.exe，MCP initialize 成功（协议 2025-06-18），tools/list 成功，目录里有 3 个工具（js / js_reset / js_add_node_module_dir）；
- 但新对话里 `mcp__node_repl__*` 从不出现，`tool_search` 搜任何东西返回空或 aborted；
- 核心日志（`~/.codex/logs_2.sqlite`）里每次 tool_search 调用都报：
  `Fatal error: tool_search handler received unsupported payload`（codex_core::tools::router）；
- rollout jsonl 里 tool_search 的 function_call 全部没有对应 output；`state_5.sqlite` 的 `thread_dynamic_tools` 表 0 行。

## 机制（codex 开源，直接读源码，别猜）

1. MCP 工具数量超过阈值后，核心把工具设为 **deferred-loading**：不直接暴露给模型，只暴露一个自定义类型工具 `type:"tool_search"`；模型先搜、核心执行搜索、把命中的工具挂载进线程，下一轮才能调用。
2. 核心 router（`codex-rs/core/src/tools/router.rs` + `protocol/src/models.rs`）只接受这种响应项：

   ```json
   {"type":"tool_search_call","call_id":"...","execution":"client","arguments":{"query":"...","limit":1}}
   ```

   —— arguments 必须是**对象**。若上游返回普通 `function_call`（arguments 为 JSON 字符串），router 包成 `ToolPayload::Function`，handler 拒收 → `unsupported payload` → turn 中止。见 openai/codex issue #20574。
3. 非原生代理（Antigravity/Ollama/LiteLLM 等）不认识 `tool_search` 自定义类型，模型只能以普通 function 形式调用 → 核心全灭。**deferred 工具因此永远无法挂载**，表现就是"进程拉起但工具不进线程"。

## 修复：antigravity_bridge.py（本地规范化桥）

```
Codex (base_url = http://127.0.0.1:18045/v1)
  → antigravity_bridge.py (127.0.0.1:18045)
  → Antigravity Tools (127.0.0.1:8045)
  → Gemini 上游
```

参照 `kimi_bridge.py`（参照机同款，仓库 codex_xiufu）移植，`scripts/antigravity_bridge.py`，双向规范化：

**请求方向（Codex → 上游）**
- `type:"tool_search"` / `type:"web_search"` 工具定义 → 等价 function 工具（name/description/parameters）；
- namespace 块**拍平**为成员 function 工具（裸名），并学习 name→namespace 映射（存 `namespace_map.json`）；
- 剥掉 `defer_loading`；schema 里 `$ref`+sibling 内联展开（`$defs` 丢弃）；
- 历史里 `tool_search_call`/`tool_search_output`/任意 `*_call`/`*_output` → 普通 `function_call`/`function_call_output` 对；非字符串 payload 一律 JSON 字符串化；
- **deferred 工具再注入**：从历史 `tool_search_output` 里把已发现的工具补回 tools 数组（OpenAI 原生路径由服务端做，代理路径必须桥来做——不做的话 tool_search 修好了模型也调不到 mcp__node_repl__js）；
- **孤儿调用修复**：web_search 这类服务端语义调用在历史上没有结果项，合成一条 function_call_output 说明真相，避免上游拒收"tool_calls 后必须跟 tool 消息"。

**响应方向（SSE / JSON）**
- 名为 tool_search/web_search 的 function_call：缓冲 added+delta 事件，在 `output_item.done` 时改发原生 `tool_search_call`/`web_search_call` 的 added+done 事件对（arguments 转对象、`execution:"client"`、`status:"completed"`）；
- 已学习的 namespaced 动态工具调用：回补 `namespace` 字段；
- `sequence_number` 全流重编号；`response.completed` 内 output 数组同规则改写。

启动：`python scripts\antigravity_bridge.py`（依赖 fastapi/uvicorn/httpx）。验证套件：`python scripts\verify_bridge_antigravity.py http://127.0.0.1:18045`（7 项全 PASS 才算部署成功）。

## 本机踩过的坑（每条都真实发生）

1. **排除法顺序**：MCP 层正常 ≠ 工具对模型可见。deferred 暴露链是 模型→tool_search→handler→挂载，断任何一环都表现为"没工具"。先读 logs_2.sqlite 找直接错误，再怀疑门控。
2. **Electron 侧证据链有尽头**：工具级暴露决策在 Rust 核心（Electron bundle 里 tool_search/allowedTools/disabledTools 均 0 命中）。codex 开源——读 router.rs 十分钟胜过逆向一天。
3. **日志取证三件套**：`logs_2.sqlite`（核心结构化日志，复制三件套再查）、`sessions/**/rollout-*.jsonl`（会话项时间线）、`state_5.sqlite.thread_dynamic_tools`（动态工具挂载表）。
4. **SSE 改写必须保留事件边界空行**：早期 Node 版 shim 的正则把 `\r\n\r\n` 边界替换成单个 `\r\n`，所有事件粘成一个、客户端永远等不到空行 → `stream closed before response.completed`。教训：**重放测试要断言线上字节（含空行），不能只断言解析后的内容**。
5. **httpx 默认 trust_env=True 会读 Windows 系统代理**：本机配了系统代理后，发往 127.0.0.1 的请求也被劫给代理 → 502 空响应，上游监控里**根本看不到请求**（"没发过去"）。本地桥必须 `trust_env=False`。http.client/urllib/requests 有 localhost 绕过逻辑所以不受影响，curl 对 localhost 默认直连——这会让排查时"有的客户端通有的不通"，极具迷惑性。
6. **429/503 是上游偶发网络问题**：重试即可，别误判成账号池耗尽或配置错误而改乱别的东西。
7. **模型名要和上游账号池匹配**：测试套件的 `k3` 是 Kimi 的模型名，打 Antigravity 会 `No accounts available with quota for model: k3`；本机正确名是 `gemini-3.7-flash-high`（无 `-online` 后缀，带错名字会 429）。
8. **版本差异理论的葬礼**：3698 通 / 5229 不通曾让人怀疑版本门控，实际是参照机走 kimi_bridge（已做 tool_search 归一化）、本机直连 Antigravity——变量从来不是 Codex 版本，是**代理链路上有没有桥**。01-03 文档的 Electron patch（keypath 剔除 + Er 强制赋能）经复核机制上成立且有效，但它们防的是"配置被篡改"，与本次工具缺失无关；Patch C（nte 线程级注入 js_repl:false）因根因在外部而搁置未打。

## 验收记录（2026-08-24，本机）

- bridge 7/7 验收套件通过（真实 Antigravity 流量）：最小请求 / tool_search 定义转换 / 历史条目转换 / SSE 原生改写 / deferred 注入+namespace 恢复 / $ref 内联 / web_search 孤儿修复；
- Codex（D:\code_ture\app，未 patch 副本）新对话：`tool_search` 搜 node_repl 正常返回并挂载 `mcp__node_repl__js`；内置浏览器打开并受控；
- config.toml 全量 diff：无段落丢失，仅 base_url→18045 与应用自身同步项（runtime 路径、projects 信任项）。
