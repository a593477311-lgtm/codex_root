# Antigravity 适配（Gemini 上游）

> 本仓库的 kimi_bridge.py 面向 Kimi For Coding。同一套规范化契约已适配到
> Antigravity Tools（本地 127.0.0.1:8045，Gemini 上游），适配版与完整调查
> 记录见姐妹仓库 **codex_ture**（docs/04、docs/05、scripts/antigravity_bridge.py）。
> 这里只记适配差异与两个新坑。

## 适配点（相对 kimi_bridge.py 的最小改动）

| 项 | kimi_bridge | antigravity_bridge |
|---|---|---|
| UPSTREAM | `https://api.kimi.com/coding` | `http://127.0.0.1:8045` |
| PORT | 15731 | 18045 |
| httpx client | 默认 | **`trust_env=False`（必须，见坑 1）** |
| 验证套件模型名 | `k3` | `gemini-3.7-flash-high`（无 `-online` 后缀，带错名字会 429） |
| dashboard.py 健康检查 | `status_code < 500` 判活 | `trust_env=False` + 任何 HTTP 响应即"可达"（Antigravity 对 /v1/models 回 5xx 也是活着） |

仪表盘三件套（dashboard.py / dashboard.html / bridge_stats.py）已随适配版
一并移植，页面在 `http://127.0.0.1:18045/dashboard`。

其余逻辑（tool/web_search 定义转换、namespace 拍平+恢复、$ref 内联、
deferred 再注入、孤儿调用修复、SSE 原生改写 + sequence 重编号）原样通用，
与上游是 Kimi 还是 Gemini 无关。

## 坑 1：httpx 会被 Windows 系统代理劫持（trust_env=True 的代价）

httpx 默认 trust_env=True，会读取 Windows 注册表里的系统代理。配了系统
代理的机器上，**发往 127.0.0.1 的请求也被劫给代理**：上游返回 502 空响应
（connection: close, content-length: 0），Antigravity 监控里根本看不到
请求（"根本没发过去"）。http.client/urllib/requests 有 localhost 绕过、
curl 对 localhost 默认直连，所以只有 httpx 中招——排查时"有的客户端通
有的不通"极具迷惑性。本地桥必须 `httpx.AsyncClient(..., trust_env=False)`。

## 坑 2：SSE 改写测试要断言线上字节

早期一个 Node 版 shim 把事件边界的空行吃掉（`\r\n\r\n` → `\r\n`），所有
事件粘成一个，客户端报 `stream closed before response.completed`。
教训：重放/单元测试必须断言**输出线上字节（含空行边界）**，不能只断言
解析后的 JSON 内容。

## 坑 3：429/503 是上游偶发网络问题

重试即可，不要误判为账号池耗尽或配置错误。
