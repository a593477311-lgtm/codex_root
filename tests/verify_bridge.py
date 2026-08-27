"""
verify_bridge.py — 桥接部署验证套件。

用法:  python tests/verify_bridge.py [桥地址]     默认 http://127.0.0.1:15731

从 ~/.codex/auth.json 读取 Kimi key（不打印）。6 项全 PASS 才算部署成功。
"""

import json
import os
import sys
import urllib.request
import urllib.error

BRIDGE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:15731").rstrip("/")
URL = BRIDGE + "/v1/responses"

KEY = json.load(open(os.path.expanduser("~/.codex/auth.json"), encoding="utf-8"))["OPENAI_API_KEY"]
HEADERS = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}


def post(body, stream=False):
    h = dict(HEADERS)
    if stream:
        h["Accept"] = "text/event-stream"
    req = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"), headers=h)
    try:
        return urllib.request.urlopen(req, timeout=120), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read()[:300]}"
    except Exception as e:
        return None, str(e)


def msg(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def sse_events(resp):
    """解析 SSE 流，产出 (event, data_dict)。"""
    ev, data_lines = None, []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                try:
                    yield ev, json.loads("\n".join(data_lines))
                except Exception:
                    pass
            ev, data_lines = None, []
            continue
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


results = []


def check(name, ok, fail_detail=""):
    results.append((name, ok, fail_detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {fail_detail}" if not ok and fail_detail else ""))


# 1. 最小请求
r, err = post({"model": "k3", "stream": False, "input": [msg("say ok")]})
check("1 最小请求", r is not None and r.status == 200, err or "")

# 2. tool_search 工具定义转换
r, err = post({"model": "k3", "stream": False, "input": [msg("say ok")], "tools": [{"type": "tool_search"}]})
check("2 tool_search 工具定义转换", r is not None and r.status == 200, err or "")

# 3. 历史条目转换（tool_search_call / tool_search_output）
r, err = post({
    "model": "k3", "stream": False,
    "input": [
        msg("搜一下线程工具"),
        {"type": "tool_search_call", "id": "tsc_1", "call_id": "tool_t1", "status": "completed",
         "execution": "client", "arguments": {"query": "thread tools"}},
        {"type": "tool_search_output", "id": "tso_1", "call_id": "tool_t1", "status": "completed",
         "execution": "client", "tools": [{"type": "namespace", "name": "codex_app", "tools": [
             {"type": "function", "name": "read_thread", "description": "read a thread", "defer_loading": True,
              "parameters": {"type": "object", "properties": {"threadId": {"type": "string"}}}}]}]},
        msg("继续"),
    ],
    "tools": [{"type": "tool_search"}],
})
check("3 历史条目转换", r is not None and r.status == 200, err or "")

# 4. SSE 响应原生项改写：逼模型调 tool_search，断言出现 tool_search_call 事件
r, err = post({
    "model": "k3", "stream": True,
    "input": [msg("请调用 tool_search 工具搜索 read_thread。必须调用工具，不要直接回答。")],
    "tools": [{"type": "tool_search"}],
}, stream=True)
native = False
if r is not None:
    for _ev, d in sse_events(r):
        if d.get("type") == "response.output_item.done":
            item = d.get("item", {})
            if item.get("type") == "tool_search_call" and isinstance(item.get("arguments"), dict) \
                    and item.get("execution") == "client":
                native = True
check("4 SSE 原生项改写 (tool_search_call)", native, err or "未出现原生 tool_search_call 事件")

# 5. deferred 工具注入 + namespace 回填：模型应能真实调用注入的工具
r, err = post({
    "model": "k3", "stream": True,
    "input": [
        msg("搜创建线程的工具"),
        {"type": "tool_search_call", "id": "tsc_1", "call_id": "tool_t2", "status": "completed",
         "execution": "client", "arguments": {"query": "create thread"}},
        {"type": "tool_search_output", "id": "tso_1", "call_id": "tool_t2", "status": "completed",
         "execution": "client", "tools": [{"type": "namespace", "name": "codex_app", "tools": [
             {"type": "function", "name": "create_thread", "description": "Create a task thread.",
              "defer_loading": True,
              "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}}}]}]},
        msg("现在调用 create_thread，prompt 填 \"测试\"。必须调用工具。"),
    ],
    "tools": [{"type": "tool_search"}],
}, stream=True)
called = False
if r is not None:
    for _ev, d in sse_events(r):
        if d.get("type") == "response.output_item.done":
            item = d.get("item", {})
            if item.get("type") == "function_call" and item.get("name") == "create_thread" \
                    and item.get("namespace") == "codex_app":
                called = True
check("5 deferred 注入 + namespace 回填", called, err or "模型未调用注入的 create_thread 或缺 namespace 字段")

# 6. schema 消毒：$ref + 兄弟字段
ugly = {
    "type": "object",
    "properties": {
        "name": {"description": "the name", "$ref": "#/$defs/s1"},
        "kind": {"$ref": "#/$defs/s2"},
    },
    "$defs": {
        "s1": {"type": "string", "minLength": 1},
        "s2": {"type": "string", "enum": ["a", "b"], "description": "kind"},
    },
}
r, err = post({
    "model": "k3", "stream": False, "input": [msg("say ok")],
    "tools": [{"type": "function", "name": "ugly_tool", "description": "has $ref+siblings",
               "parameters": ugly}],
})
check("6 schema 消毒 ($ref 内联)", r is not None and r.status == 200, err or "")

# 7. 孤儿调用修复：web_search_call 无结果项（服务端语义，app 不回传）
r, err = post({
    "model": "k3", "stream": False,
    "input": [
        msg("查一下某 CLI 的用法"),
        {"type": "web_search_call", "id": "ws_1", "call_id": "web_search_0", "status": "completed",
         "execution": "server", "action": {"type": "search", "query": "some cli"}},
        msg("继续"),
    ],
    "tools": [{"type": "web_search"}],
})
check("7 孤儿调用修复 (web_search 无结果项)", r is not None and r.status == 200, err or "")

print()
failed = [n for n, ok, _ in results if not ok]
print("结果:", f"{len(results) - len(failed)}/{len(results)} PASS", ("| 失败: " + ", ".join(failed)) if failed else "—— 部署验证通过")
sys.exit(1 if failed else 0)
