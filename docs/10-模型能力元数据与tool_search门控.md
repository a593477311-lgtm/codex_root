# 10 - 模型能力元数据与 tool_search 门控（2026-08-28 教训沉淀）

## 现象

GLM-5.3-Flash / GLM-5.3 / MiniMax-M3 会话中，模型声称"没有创建线程的能力"，
`create_thread` / `send_message_to_thread` 等线程工具完全不可用；
而 k3 会话可以正常创建线程并发消息。

## 根因（已实锤）

Codex Desktop 的高级工具采用 **tool_search 延迟发现机制**：

1. 客户端在请求中注入一个 `tool_search` 元工具；
2. 模型调用 `tool_search("create thread")` 按需发现延迟工具；
3. 网关把发现的延迟工具（`create_thread`、`fork_thread`、`send_message_to_thread`、
   `automation_update`、`send_input` 等）注入后续请求。

**是否注入 `tool_search`，由 `~/.codex/models.json` 中该模型条目的"完整度"门控。**

- k3 / gemini-3.7-flash-high：完整条目（30 个字段），客户端注入 tool_search → 一切正常；
- GLM / MiniMax：早期被网关自动注册逻辑生成的**精简条目**（17 个字段），
  缺 `context_window`、`max_context_window`、`model_messages`、`supports_search_tool`、
  `apply_patch_tool_type` 等 13 个字段 → 客户端不注入 tool_search → 线程工具永远拿不到。

### 证据链

- 网关日志：`convert tool type 'tool_search' -> function 'tool_search'` +
  `injected N deferred tools from tool_search history: ['create_thread', ...]`
  只出现在 k3 请求中，GLM 请求里完全没有；
- 逐字段 diff k3 vs GLM-5.3-Flash：精简条目缺 13 个能力字段（无独有字段）。

## 修复（commit ffbd013）

1. **数据层**：把 GLM-5.3-Flash / GLM-5.3 / MiniMax-M3 三条目按 k3 模板补齐 12 个字段
   （`model_messages` 故意不抄——里面是 "based on GPT-5" 的人设模板，抄了会串身份）；
2. **代码层**（`bridge/dashboard.py` 的 `ensure_models_in_catalog`）：
   - 新模型注册改为**深拷贝完整模板**（k3 → gemini → models[0] 回退链），不再用精简模板；
   - `base_instructions` 和 `model_messages.instructions_template` 双处做名字替换
     （旧 slug 和 "GPT-5" 都替换为新模型名）；
   - 固定写 4 档推理（low/medium/high/xhigh），写盘走 tmp + `os.replace` 原子替换。

## 验证方法（排障时可复用）

```powershell
# 网关中实时观察某模型请求是否带 tool_search 注入：
Select-String ~/.codex/tools/kimi_bridge.log -Pattern "tool_search|deferred tools"
```

- 修复后需**新开会话**（客户端在会话创建时按 models.json 决定注入哪些工具，存量会话不变）；
- 日志里出现该模型的 `convert tool type 'tool_search'` 即生效。
- 2026-08-28 实测：修复后 MiniMax-M3、gemini、GLM 会话全部能创建线程 ✅

## 教训与遗留坑

1. **models.json 不是花架子**——它直接门控客户端能力注入。新接模型时条目完整度必须对齐 k3。
2. `ensure_models_in_catalog` 目前**只注册新 slug，不会升级已存在的精简条目**（P3 遗留）。
   若 models.json 被重置或某模型曾被旧逻辑稀疏注册，需手工补齐或删除条目后让它重注册。
3. 模板回退链最后一档 `models[0]` 本身可能是精简条目，会把稀疏性传染给新模型——
   保持 k3 条目存在即可规避。
4. 照搬 `context_window=1048576` 的前提是上游真支持 1M 上下文；接新上游时按真实窗口填，
   否则客户端压缩阈值（95%）会超过上游上限，长会话直接 400 而不是优雅压缩。
5. 密钥轮换闭环检查清单：新 key 入配置 → 直连上游测活 200 → 走网关全链路 200 →
   **旧 key 直连上游确认 401**（防止只生成新 key 忘删旧 key）。
6. asar 补丁副本（%LOCALAPPDATA%\Codex-patched\<ver>）静态验证全过后，
   仍需手动启动其中的 `ChatGPT.exe` 才生效（`Codex.exe` 是更新器 stub，不会起界面）；
   正在运行的 WindowsApps 原版不受补丁影响。

## 附：freeform custom 工具的桥接缺陷（同日第二案）

**现象**：`apply_patch` 调用全部空转 aborted——模型发出的调用没有内容。

**根因（双面 bug）**：

1. 请求侧：`custom`（freeform）类型工具不在 `SPECIAL_CALL_TYPES` 里，走兜底分支被转成
   **空 schema 的 JSON 函数**（`properties: {}`），模型没有字段可填 patch 文本，
   只能发出 `arguments: {}`；
2. 响应侧：`apply_patch` 未注册进 `special_names`，模型返回的 `function_call` 原样透传，
   但客户端按 custom 工具注册，无法派发 `function_call` → 调用 aborted。
   客户端期望的原生形状是 `custom_tool_call` + 字符串 `input`。

**修复**（kimi_bridge.py）：

1. `normalize_tool` 增加 `ty == "custom"` 专分支：注册
   `special_names[name] = "custom_tool_call"`，schema 提供单一 `input` 字符串字段
   （required），模型有地方写原始 payload；
2. `rewrite_output_item` 增加 `custom_tool_call` 还原分支：从模型 JSON 参数里抽 `input`
   文本（兜底用原始 arguments 字符串），还原为
   `{type: custom_tool_call, name, call_id, input}`——注意必须带 `name`，
   原通用 special 分支恰恰丢了 `name`；
3. SSE 流式路径无需改动：`SseRewriter` 的 special 缓冲重发走 `rewrite_output_item`，自动生效。

历史侧（input 里的 `custom_tool_call`/`custom_tool_output` → `function_call`/
`function_call_output`）原本就是对的，不用动。

**验证**：本条目第一段标题以上的这段文字，就是修复后首个真实 `apply_patch` 调用写入的 ✅
