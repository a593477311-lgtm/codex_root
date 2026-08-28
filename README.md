# Codex All-In-One (Codex 完整修复与多厂商模型网关套件)

> 本仓库由 **`codex_xiufu`**（多厂商协议中枢、可视化控制台、动态 Key 注入、自定义供应商生态）与 **`codex_ture`**（Codex Desktop Electron 客户端底层加固、`features.js_repl` 防篡改、救急手册）两大核心工程深度融合而成。
> 彻底解决 Codex 在本地使用第三方模型（Kimi、Gemini、MiniMax、DeepSeek 等）及内置工具（Node REPL、浏览器控制、Tool Search）时的协议阻断、Key 缓存与底层反写冲突。

---

## 核心架构概览

```
+-------------------------------------------------------------------------+
|                           Codex Desktop / CLI                           |
|       (MSIX 安装 / Electron app.asar / Node REPL / 内置浏览器控制)      |
+------------------------------------+------------------------------------+
                                     | (本地 HTTP 请求: 127.0.0.1:15731)
                                     v
+-------------------------------------------------------------------------+
|                  Codex 本地网关中枢 (bridge/kimi_bridge.py)             |
|                                                                         |
|  * 动态 Key 智能注入: 解决客户端内存缓存旧 Key 引起的 401 报错，免重启热切  |
|  * 七层协议改写: tool_search 转 function、namespace 展平、SSE 流缓冲合成    |
|  * 多渠道模型轮换: Kimi k3 / Gemini Antigravity / MiniMax-M3 / 自定义渠道 |
|  * 可视化控制台: http://127.0.0.1:15731/dashboard (极客暗色 UI、监控与测活) |
+--------------+---------------------+---------------------+--------------+
               |                     |                     |
               v                     v                     v
      [Kimi For Coding]       [Gemini / AGY]         [MiniMax 官方]
       api.kimi.com           127.0.0.1:8044        api.minimaxi.com
               |                     |                     |
               +---------------------+---------------------+
                                     |
                                     v
                       [自定义供应商矩阵 / 第三方兼容端点]
                       DeepSeek / Ollama / SiliconFlow / Groq
```

---

## 两大核心子系统

### 一、网关与管理系统 (`bridge/`)
* **全渠道模型中枢（内置 + 自定义扩展）**：
  * 预置支持 **Kimi For Coding (k3)**、**Gemini (gemini-3.7-flash-high)**、**MiniMax (MiniMax-M3)**；
  * 原生支持**接入任意自定义供应商**（DeepSeek、Ollama、SiliconFlow、Groq 等符合 OpenAI Responses 协议的上游）；
  * 具备自定义模型自动入库机制：新增渠道时自动同步写入 `~/.codex/models.json` 模型字典，供 Codex 客户端原生检索识别。
* **免重启零感知热切**：出站请求由网关按当前配置动态重写 Authorization 头并注入对应厂商专属 Key，彻底脱耦客户端内存中的旧凭证缓存，切换立即可用。
* **原生配置原子联动**：切换时原子同步回写 `~/.codex/config.toml`（`model`）与 `~/.codex/auth.json`（`OPENAI_API_KEY`）。
* **可视化仪表盘 (SPA)**：挂载于 `http://127.0.0.1:15731/dashboard`，单文件零外部依赖，极客暗色 UI，纯矢量暗色图标资产，0 Emoji。提供实时延迟、Token 用量走势、模型消耗分布与在线实时测活。
* **协议安全改写与自学习**：
  * 完整修复 `tool_search` 孤儿调用、工具参数反向补全；
  * 动态学习双向流量中的 Function -> Namespace 映射并本地持久化，支持在控制台实时审查与单键删除。

### 二、客户端底层加固系统 (`client_patches/`)
* **配置防篡改 Patch**：解决 Codex Desktop 在与远端同步时强行通过 `config/batchWrite` 将 `features.js_repl` 反写为 `false` 的问题。
* **安全沙盒原则**：原包只读，绝不修改 `C:\Program Files\WindowsApps` 下的源文件，全部修改位于 `%LOCALAPPDATA%\Codex-patched\<版本>` 副本。
* **自动化与幂等性**：支持版本自检、解包、定位混淆锚点、校验与重打包，带静态与动态哈希校验。
* **救急恢复机制**：配套 [RESCUE.md](RESCUE.md) 救回手册，非本系统的任何 Agent 或人类均可在 30 秒内完整恢复官方环境。

---

## 仓库目录结构

```
E:\codex_root\
├── .gitignore                         # 统一忽略文件（日志、数据库、密钥、临时备份）
├── README.md                          # 项目主入口导航（本文档）
├── RESCUE.md                          # 客户端异常救回手册（任何 Agent / 人类均可操作）
├── docs/新机部署指南.md               # ★ 新电脑从零部署 + 验证一条龙指南（另一台机器上的 Codex 照此执行）
│
├── bridge/                            # 【网关模块】模型中枢、协议转换与可视化仪表盘
│   ├── kimi_bridge.py                 # 网关核心服务（多厂商路由、免重启 Key 注入、协议补丁）
│   ├── antigravity_bridge.py          # Antigravity 专用轻量桥接服务
│   ├── dashboard.py                   # 仪表盘 FastAPI 管理路由与数据分析接口
│   ├── dashboard.html                 # 仪表盘控制台主界面（单页 SPA，极客暗色 UI，纯矢量资产，0 Emoji）
│   ├── bridge_stats.py                # Token 消耗与请求延迟 SQLite 异步采集库
│   ├── bridge_config.example.json     # 多厂商配置模板（含自定义渠道结构示例）
│   └── assets/                        # 字体（Smiley Sans）、动效库（GSAP）与暗色状态图标资产
│
├── client_patches/                    # 【客户端加固】Codex Desktop 客户端修复与防篡改
│   ├── patch-js_repl.ps1              # app.asar 幂等 Patch 脚本（防止 js_repl 被强制反写）
│   ├── verify-js_repl.ps1             # asar 静态检测与关键哈希校验脚本
│   ├── patch-core.js                  # asar AST / 正则定位修改核心逻辑
│   ├── tool-search-shim.js            # tool_search 本地垫片
│   ├── fake-upstream.js               # 仿真上游测试服务
│   └── shim-replay-test.js            # 流量回放测试脚本
│
├── scripts/                           # 【运维与工具】一键运行与环境管理脚本
│   ├── start_bridge.bat               # 启动网关服务（后台运行）
│   ├── stop_bridge.bat                # 停止网关服务
│   └── install_autostart.ps1          # 注册 Windows 登录开机自启动计划任务
│
├── tests/                             # 【测试验证】全链路自动化验证套件
│   ├── verify_bridge.py               # 网关协议转换 6 项端到端测试套件
│   └── verify_bridge_antigravity.py   # Antigravity 专用协议验证套件
│
├── docs/                              # 【技术文档】架构设计、技术根因与排障手册
│   ├── 01-问题与根因-js_repl.md         # 现象、batchWrite 根因证据与修复策略
│   ├── 02-客户端操作流程-asar_patch.md  # 详细操作步骤与 fuses 签名检查
│   ├── 03-验证方案与避坑指南.md        # 静态/动态验证清单与实测踩坑汇总
│   ├── 04-深度方案-tool_search与本地代理.md # Responses API 与 tool_search 深度分析
│   ├── 05-antigravity-bridge使用指南.md   # Antigravity 本地代理运维手册
│   ├── 06-网关七层协议转换与诊断.md     # Kimi Bridge 7 层协议拦截排障手册
│   ├── 07-网关运维与排障手册.md         # 端口、自启、日志排查与应急回退
│   ├── 08-供应商轮换与UI重构落地.md     # 供应商轮换中枢与前端现代化重构设计
│   ├── 09-antigravity与Gemini适配说明.md # Gemini 127.0.0.1:8044 端点兼容指南
│   └── 10-模型能力元数据与tool_search门控.md # models.json 条目完整度门控客户端能力注入（GLM 无线程工具根因）
│
└── reference/                         # 【实测证据】
    └── evidence-26.818.3698.0.md      # 真实环境完整构建、哈希与验证日志证据链
```

---

## 快速开始

### 1. 部署与启动网关
```powershell
# 1. 安装 Python 依赖
pip install fastapi uvicorn httpx

# 2. 将 bridge 部署至用户目录（或直接从本地运行）
mkdir "$env:USERPROFILE\.codex\tools" -Force | Out-Null
Copy-Item "bridge\*" "$env:USERPROFILE\.codex\tools\" -Recurse -Force

# 3. 启动网关服务
.\scripts\start_bridge.bat

# 4. 可选：注册 Windows 登录时自启
powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
```

### 2. 访问控制台
在浏览器中打开：
```
http://127.0.0.1:15731/dashboard
```
* **监控页**：查看请求统计、Token 吞吐走势与实时往返延迟。
* **渠道页**：在线管理、热切、测活供应商（Kimi、Gemini、MiniMax、智谱 BigModel 与自定义渠道）。

### 3. 应用客户端加固 Patch（可选，防止 js_repl 被反写）
```powershell
# 以管理员权限执行
powershell -ExecutionPolicy Bypass -File client_patches\patch-js_repl.ps1
powershell -ExecutionPolicy Bypass -File client_patches\verify-js_repl.ps1
```
> 如遇到客户端异常，可直接查阅 [RESCUE.md](RESCUE.md) 进行秒级还原。

---

## 安全与免责声明
1. 本仓库严格遵守**敏感数据隔离原则**，示例配置（`bridge_config.example.json`）仅包含占位符，真实生产密钥、本地运行数据库与运行日志由 `.gitignore` 全面排除，绝不上云。
2. 客户端 Patch 采用副本非侵入机制，完全不损坏 WindowsApps 官方包签名。
