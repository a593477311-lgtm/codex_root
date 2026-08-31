# 11 - web_search 孤儿调用落地为真实搜索（2026-08-29 教训沉淀）

## 现象

k3 等走网关的会话里，模型调用 `web_search` 后永远拿不到结果：
客户端把 `web_search_call` 记入历史但不附带结果项，下一轮请求经
`repair_orphan_calls` 合成一条"[bridge note] ... 从未执行"的占位输出。
模型反复重试搜索 → 反复拿到占位 note → 会话原地打转甚至中断。

## 根因

`web_search` 在 Codex app 里是**服务端语义**工具（官方路径由 OpenAI 服务器
代为执行）。走网关后没有任何一方真正执行它：

1. 客户端记录调用、等服务端回填结果；
2. 网关只是把 `web_search_call` 转成普通 `function_call` 透传；
3. 上游模型（Kimi/GLM/...）更没有替它执行的义务。

于是孤儿调用每轮都被 note 堵住，搜索能力名存实亡。

## 修复（bridge/kimi_bridge.py，+110 行）

新增 `fulfill_web_searches`：在 `normalize_body` 之后、转发上游之前，
扫描请求体里的 web_search 孤儿 note，**现场替模型把搜索真做掉**：

1. 从对应 `function_call` 的 arguments 提取查询词
   （`query` 或 `input` 字段都认——不同模型落键习惯不同）；
2. **空查询兜底**：客户端原生 web_search 无参数 schema，模型经常发出
   空 arguments（2026-08-28 实测连续 4 次空调用），此时回退取
   **最近一条有实质内容的用户消息**作为查询词——跳过"继续/好"这类
   推进指令，剥掉 app 的 environment_context 包装层
   （取 `## My request:` 之后的正文）；
3. 经由具备原生联网能力的供应商链执行
   （`SEARCH_CAPABLE = ("zhipu", "kimi", "minimax")`，按各上游**原生形状**适配，
   详见下节实测表）；
4. 按 call_id 缓存最终结果（**失败也缓存为原 note**），避免同一个孤儿
   在每一轮请求里重复触发真实搜索拖慢会话；
5. 替换 note 为真实结果后重新序列化请求体。无目标时字节级原样透传。

## 验证

离线直测（导入模块直接调函数，不动生产进程）：

- `_native_search("zhipu", ...)` 真实联网返回结果 + 来源链接 ✅
- 显式 query 的孤儿 note 被替换为真实结果 ✅
- 空 query 孤儿经用户消息兜底拿到真实结果 ✅
- 无目标请求体字节级透传 ✅

## 四通道实测（2026-08-29）与执行门控

对四个供应商逐一实测其**原生形状**：

| 供应商 | 原生搜索形状 | 实测结论 |
| --- | --- | --- |
| zhipu (GLM-5.3-Flash) | Responses `{"type": "web_search"}`，模型自愿真搜 | ✅ 稳定，带来源链接 |
| kimi (k3 @ api.kimi.com/coding) | **Anthropic `/v1/messages` + `web_search_20250305` server tool**，单请求内完成搜索并作答 | ✅ 稳定，带来源链接 |
| minimax (MiniMax-M3) | 同 zhipu 形状，但 M3 大概率无视工具（强制 `tool_choice` 也拦不住） | ⚠️ 8 次实测仅 2 次真搜，列为链尾兜底 |
| gemini (3.7-flash-high @ 127.0.0.1:8044) | 本地代理 503 | ❌ |

**关键教训**：
- kimi 的 OpenAI 形状 `$web_search` builtin 也在（需两轮握手），但 Anthropic
  server-tool 形状单请求即可完成，适配器选后者；
- **MiniMax 失败时不报错而是现场编日期**——同问题三连测答出 5月18日/
  5月5日/5月14日三个互异的错日期。门控因此看**执行证据**而非文本：
  响应里必须真出现 `web_search_call`（kimi 侧为带来源的
  `web_search_tool_result`）才算成功，否则顺延下一个供应商。
  没有执行证据的"搜索结果"与幻觉无法区分。

## 运维注意

- **热重启会断在途 SSE 流**。本网关承载着正在进行的会话流量，
  重启瞬间恰好有推理请求的话，那一轮会直接中断（2026-08-29 早
  K3 线程反复"抽风中断"即此：agent 改完网关代码热重启，把自己的
  在途请求杀了）。改网关代码尽量在会话空闲窗口重启，或接受重启后
  需要用户手动"继续"。
- `_SEARCH_CACHE` 是进程内存，重启即清空；孤儿 note 会在重启后的
  下一轮请求重新触发一次真实搜索，属预期行为。

## 续轮引擎（2026-08-29 下午，根上消除"断了"体感）

### 现象

模型发出 web_search 后，整个轮次戛然而止：客户端认为该工具是服务端语义、
不本地执行、也不自动续话，用户只能看到"我去搜一下"然后静默——
体感就是"又断了"。此前 fulfill 只把结果回填进**下一次**请求，救不了当下这轮。

### 方案

对齐官方 Codex 的服务端工具语义——**响应流内续轮**：

1. 上游响应以 web_search 调用收尾时（`response.completed` 被扣下不转发），
   桥自己按供应商链执行搜索；
2. 用"原始 input + 各轮 output + function_call_output(搜索结果)"构造
   follow-up 请求（保持时间序），继续流式转发，作为**同一条客户端响应**的延续；
3. 续轮事件处理：丢弃 `response.created/in_progress`、`output_index` 按已发
   output 数前移、sequence_number 续号；最终只发一个合并版
   `response.completed`（原 response id、合并 output、合并 usage）；
4. 上限 `SEARCH_CONT_MAX = 3` 轮防打转；follow-up 失败则优雅回退为
   "轮次止于搜索调用"（旧行为），历史孤儿仍由请求路径的 fulfill 兜底；
5. 续轮里模型若改调**客户端工具**（exec_command 等），续轮判定
   （output 里全部 function_call 都是 web_search 才续）自动放行，
   由客户端正常接管，两种续话机制不打架。

### 验证

MockTransport 双轮集成测试 7 项全过（续轮事件序列、output_index 前移、
completed 合并、follow-up 输入顺序），生产实测：kimi 通道
`cont: round 1, executed 1 web_search call(s)`，轮次内直接给出答案。

## 搜索反馈 UX（2026-08-31）：让"正在搜索"看得见

### 现象

续轮引擎上线后搜索功能活了，但**搜索期间 UI 无任何反馈**——
客户端只有 "Working / Thinking"，用户分不清是在搜还是卡死。

### 根因（GLM-5.3 调研线程查实，主会话独立复核）

客户端渲染"Searching the web for X"的依据是 `web_search_call` item 的
**status 字段**（`in_progress`→转圈，`completed`→完成态），由
`output_item.added/done` 两帧驱动；官方文档里的
`response.web_search_call.in_progress/searching/completed` 阶段事件
**这个客户端的 SSE 解析器没有对应 case，发了也被吞**——所以修复点
不在补发阶段事件，而在两帧的**时机与内容**：

1. 桥原来在上游函数调用一结束就把 added+done 背靠背发出且
   `status` 硬编码 `completed`——UI 闪一下"已搜索"然后整个搜索窗口静默；
2. item 缺 `action` 字段——UI 从 `action.query` 取词，缺了显示为空。

### 修复（kimi_bridge.py，bridge/test_web_search_ux.py 33 项全绿）

1. `rewrite_output_item`：web_search_call 补 `action: {type: search, query}`；
2. `SseRewriter`：web_search 拆帧——上游 done 到达时先发
   `status: in_progress` 的 added（UI 开始转圈），done 帧暂存；
3. 续轮引擎：每次 `_execute_search` 完成后 flush 对应 done
   （回填实际执行的查询词，含 fallback 词）；发合并 completed 前
   兜底清空全部暂存 done（任何退出路径条目不悬挂）。

生产实测事件时序：added(in_progress) 09:21:38 → kimi 真搜 18s →
done(completed) 09:21:56 → 续轮内容流出——搜索全程 UI 有"正在搜索"反馈。

### 运维备忘

- 搜索供应商健康波动会影响 UX 窗口长度：2026-08-31 曾测得 zhipu 60s
  超时 + kimi 无证据 + minimax 不支持的三连降级（静默 103s）。
  若用户报"搜索特别久"，先查日志里 `native search via` 的 WARNING。
- 续轮路径的搜索不写 `_SEARCH_CACHE`，同一 call 的结果不会被
  请求侧 fulfill 复用——目前可接受，若嫌浪费可后续打通。
