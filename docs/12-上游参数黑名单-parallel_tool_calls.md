# 12 - 上游参数黑名单：kimi 端点拒收 parallel_tool_calls（2026-08-29 教训沉淀）

## 现象

某 k3 会话的轮次连续 6 次 400 全灭（每个 ~1s 快速失败、0 token），
dashboard 调用明细一片红；客户端重试也全部失败。

## 根因

上游错误体：`parallel_tool_calls: false is not supported`。

Codex 客户端会例行发送 `parallel_tool_calls: false`（合法 Responses API 参数，
用来禁止模型并行工具调用）。各上游对该参数的容忍度实测：

| 上游 | 带 parallel_tool_calls: false |
| --- | --- |
| kimi (api.kimi.com/coding) | ❌ HTTP 400 |
| zhipu (open.bigmodel.cn) | ✅ 200 |
| minimax (api.minimaxi.com) | ✅ 200 |

不是 Codex 的 bug，也不是模型问题——是 kimi coding 端点对这个参数不兼容。
注意它只影响**携带该参数的请求**（并非每个请求都带），所以同一会话
大部分轮次正常、个别轮次（如 tools 为空的请求类型）会触发。

## 修复

`kimi_bridge.py` 新增 `UNSUPPORTED_BODY_PARAMS` 上游参数黑名单机制：
按上游 host 匹配（`api.kimi.com` → 剥掉 `parallel_tool_calls`），
在 fulfill 之后、转发之前执行；剥掉时写日志 + stats 事件。
语义影响可忽略：剥掉后只是不再禁止并行调用，模型极少主动并行。

验证：单元（kimi 剥 / zhipu 留）+ 生产 e2e（带参打活体桥 → kimi 200）。

## 关联辨析

与 2026-08-28 的"create_thread 建窗校验失败"（thinking 档位/模型名被拒）
是**两个不同的 bug**：那次是客户端 models.json 目录校验（ffbd013 已修），
这次是上游参数兼容性。两者都表现为"连试 N 次全挂"，容易混淆；
诊断时以 dashboard 的状态码和 err 字段为准。
