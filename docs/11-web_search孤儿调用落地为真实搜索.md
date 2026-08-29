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
   **最近一条用户消息**作为查询词；
3. 经由具备原生联网能力的供应商链执行（`SEARCH_CAPABLE = ("zhipu", "minimax")`，
   2026-08-28 实测：kimi 上游 400/忽略，gemini 代理不通）：
   以 `{"tools": [{"type": "web_search"}]}` 调上游 `/v1/responses`，
   抽取 message 正文 + web_search_call 里的 sources 拼成结果文本；
4. 按 call_id 缓存最终结果（**失败也缓存为原 note**），避免同一个孤儿
   在每一轮请求里重复触发真实搜索拖慢会话；
5. 替换 note 为真实结果后重新序列化请求体。无目标时字节级原样透传。

## 验证

离线直测（导入模块直接调函数，不动生产进程）：

- `_native_search("zhipu", ...)` 真实联网返回结果 + 来源链接 ✅
- 显式 query 的孤儿 note 被替换为真实结果 ✅
- 空 query 孤儿经用户消息兜底拿到真实结果 ✅
- 无目标请求体字节级透传 ✅

## 运维注意

- **热重启会断在途 SSE 流**。本网关承载着正在进行的会话流量，
  重启瞬间恰好有推理请求的话，那一轮会直接中断（2026-08-29 早
  K3 线程反复"抽风中断"即此：agent 改完网关代码热重启，把自己的
  在途请求杀了）。改网关代码尽量在会话空闲窗口重启，或接受重启后
  需要用户手动"继续"。
- `_SEARCH_CACHE` 是进程内存，重启即清空；孤儿 note 会在重启后的
  下一轮请求重新触发一次真实搜索，属预期行为。
