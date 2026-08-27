# 05 antigravity_bridge 使用指南（本机 Codex 配套）

> 面向本机（ZNBZT，Windows）。桥 = `scripts/antigravity_bridge.py`，让 Codex 经 Antigravity Tools（127.0.0.1:8045，Gemini 上游）使用全部工具（node_repl / 内置浏览器 / Computer Use / deferred MCP 工具）。

## 架构

```
Codex Desktop ──base_url──> 127.0.0.1:18045 (antigravity_bridge.py)
                                 │  双向规范化（见 docs/04）
                                 ▼
                          127.0.0.1:8045 (Antigravity Tools)
                                 ▼
                            Gemini 上游
```

## 日常启动 / 停止

```powershell
# 启动（前台看日志可用 python，后台常驻用 pythonw）
python  D:\code_ture\scripts\antigravity_bridge.py
# 停止
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'antigravity_bridge' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

开机自启（可选）：参考 `codex_xiufu\scripts\install_autostart.ps1` 的模式，把启动命令注册为计划任务（按需再做，本指南不替你装）。

## config.toml 配套（已就位，勿重复改）

```toml
[model_providers.custom]
base_url = "http://127.0.0.1:18045/v1"   # 指向桥，不是 8045
```

模型名保持 `gemini-3.7-flash-high` 系（**不要加 `-online` 后缀**，会 429）。改 config 前先备份：`Copy-Item ~\.codex\config.toml ~\.codex\config.toml.bak-<时间戳>`。

## 部署/排障验证（7 项必须全 PASS）

```powershell
python D:\code_ture\scripts\verify_bridge_antigravity.py http://127.0.0.1:18045
```

覆盖：最小请求 / tool_search 定义转换 / 历史条目转换 / SSE 原生改写 / deferred 注入 + namespace 恢复 / $ref 内联 / web_search 孤儿修复。

## 日志在哪看

| 日志 | 位置 | 内容 |
|---|---|---|
| **仪表盘（推荐）** | **http://127.0.0.1:18045/dashboard** | 三页：仪表盘（token 趋势/活跃热力图/模型环图/调用明细，10s 自刷）、桥接事件（转换/注入/学习流水）、设置（热改配置 + namespace 管理 + 日志查看） |
| bridge 主日志 | `D:\code_ture\scripts\antigravity_bridge.log` | 每个请求的状态码、tools 全量快照、转换动作（convert/inject/learned）、上游 4xx/5xx 错误体（滚动 5MB×3） |
| namespace 映射 | `D:\code_ture\scripts\namespace_map.json` | name→namespace 学习结果（仪表盘"设置"页可删单条） |
| 统计库 | `D:\code_ture\scripts\bridge_stats.db` | 请求/token/事件统计（SQLite，仪表盘数据源，默认保留 30 天） |
| 桥配置 | `D:\code_ture\scripts\bridge_config.json` | 首次启动自动生成；仪表盘"设置"页保存时自动留 `.bak-时间戳` 备份；upstream/log_level/各开关热生效，host/port 需重启桥 |
| 核心日志 | `~/.codex/logs_2.sqlite`（复制三件套再查） | `unsupported payload` 等核心错误——修好后不应再新增 |
| 会话项 | `~/.codex/sessions\**\rollout-*.jsonl` | tool_search 的 function_call 应都有 output |

## 排障速查

| 现象 | 先查 |
|---|---|
| Codex 报 429 / "All accounts exhausted" | 上游偶发网络问题，等 20-60s 重试；别动配置 |
| Codex 报 502 / bridge 日志无记录 | bridge 没在跑，或端口被占；确认 `curl http://127.0.0.1:18045/v1/models` 有响应（502 也是活着） |
| 工具又消失了 | 跑验收套件；若 7/7 PASS 但 Codex 仍无工具，查 config.toml 的 base_url 是否被改回 8045 |
| 升级 Antigravity 后异常 | 重跑验收套件；上游行为变化会在 4xx/5xx 错误体里留痕 |
| 需要彻底绕开桥 | base_url 改回 `http://127.0.0.1:8045/v1`，重启 Codex（tool_search 会重新全灭，属预期） |

## 关键纪律

- bridge 只绑 127.0.0.1，勿暴露到局域网；
- `antigravity_bridge.log` 会记录 tools 快照（含路径等本机信息），分享日志前自行脱敏；
- Codex / Antigravity 任何一方升级后，先跑验收套件再正常使用。
