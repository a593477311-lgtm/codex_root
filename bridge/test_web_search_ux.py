# -*- coding: utf-8 -*-
"""test_web_search_ux.py — web_search UX 离线回归（in_progress 壳 + 延迟 done 帧）。

纯 asyncio 脚本，不依赖 pytest：
    python E:\\codex_root\\bridge\\test_web_search_ux.py

覆盖：
  - 收编旧 test_continuation.py 的 7 项（续轮双轮：单 created / 原生 ws_call /
    output_index 前移 / 文本增量 / 合并 completed / follow-up 结果与顺序）
  - 新增 8-14：in_progress→搜索→done→follow-up→completed 事件序、sequence_number
    单调、多轮 oi 前移、混合工具不续轮的兜底 flush、follow-up 失败收尾、
    空 query 回填 fallback 词、回程 input 转换 + fulfill 兼容。

隔离说明：把运行副本 kimi_bridge.py 复制到 %TEMP% 沙箱导入，并 fake 掉
bridge_stats / dashboard 两个带落盘/读生产配置的依赖——测试全程不写
C:\\Users\\gg1\\.codex\\tools 下的任何文件。
"""
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

TOOLS_DIR = r"C:\Users\gg1\.codex\tools"
RUNTIME_COPY = os.path.join(TOOLS_DIR, "kimi_bridge.py")
import httpx  # noqa: E402


def load_bridge_sandboxed():
    sandbox = tempfile.mkdtemp(prefix="kbtest_ux_")
    shutil.copyfile(RUNTIME_COPY, os.path.join(sandbox, "kimi_bridge.py"))

    fake_stats = types.ModuleType("bridge_stats")

    class _Stats:
        def configure(self, **kw):
            pass

        def note_request(self, **kw):
            pass

        def note_event(self, *a, **kw):
            pass

    fake_stats.stats = _Stats()
    fake_stats.extract_usage = lambda r: {}

    fake_dash = types.ModuleType("dashboard")
    from fastapi import APIRouter

    fake_dash.router = APIRouter()
    fake_dash.load_config = lambda: {
        "upstream": "http://mock",
        "host": "127.0.0.1",
        "port": 15731,
        "stats_retention_days": 7,
        "stats_enabled": False,
        "learn_namespaces": False,
        "active_provider": "kimi",
        "providers": {
            "kimi": {
                "upstream": "http://mock",
                "key": "k",
                "active_model": "k3",
                "models": ["k3"],
            }
        },
    }
    sys.modules["bridge_stats"] = fake_stats
    sys.modules["dashboard"] = fake_dash
    sys.path.insert(0, sandbox)
    import kimi_bridge as kb

    return kb


kb = load_bridge_sandboxed()
_ORIG_EXECUTE_SEARCH = kb._execute_search  # some cases monkeypatch it; restore between cases

# ---------------------------------------------------------------- SSE 构造/解析


def sse(frames):
    """frames: [(event_name, payload)] → 带 sequence_number 的 SSE 字节流。"""
    parts = []
    for seq, (ev, payload) in enumerate(frames):
        d = dict(payload)
        d["type"] = ev
        d["sequence_number"] = seq
        parts.append(f"event: {ev}\ndata: {json.dumps(d, ensure_ascii=False)}\n")
    parts.append("data: [DONE]\n")
    return ("\n\n".join(parts) + "\n").encode("utf-8")


def fr_created(rid):
    return ("response.created",
            {"response": {"id": rid, "model": "k3", "status": "in_progress", "output": []}})


def fr_fc_added(iid, name, oi=0):
    return ("response.output_item.added",
            {"output_index": oi,
             "item": {"id": iid, "type": "function_call", "name": name,
                      "call_id": iid, "arguments": ""}})


def fr_fc_delta(iid, delta, oi=0):
    return ("response.function_call_arguments.delta",
            {"item_id": iid, "output_index": oi, "delta": delta})


def _fc_item(iid, name, args, status="completed"):
    return {"id": iid, "type": "function_call", "status": status,
            "name": name, "call_id": iid, "arguments": args}


def fr_fc_done(iid, name, args, oi=0):
    return ("response.output_item.done",
            {"output_index": oi, "item": _fc_item(iid, name, args)})


def fr_msg_added(mid, oi=0):
    return ("response.output_item.added",
            {"output_index": oi,
             "item": {"id": mid, "type": "message", "role": "assistant",
                      "status": "in_progress", "content": []}})


def fr_msg_delta(mid, text, oi=0):
    return ("response.output_text.delta",
            {"item_id": mid, "output_index": oi, "content_index": 0, "delta": text})


def fr_msg_done(mid, text, oi=0):
    return ("response.output_item.done",
            {"output_index": oi,
             "item": {"id": mid, "type": "message", "role": "assistant",
                      "status": "completed",
                      "content": [{"type": "output_text", "text": text, "annotations": []}]}})


def fr_completed(rid, outputs, usage):
    return ("response.completed",
            {"response": {"id": rid, "model": "k3", "status": "completed",
                          "output": outputs, "usage": usage}})


def parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        ev, dl = None, []
        for ln in block.splitlines():
            if ln.startswith("event:"):
                ev = ln[6:].strip()
            elif ln.startswith("data:"):
                dl.append(ln[5:].lstrip())
        if dl:
            try:
                events.append((ev, json.loads("\n".join(dl))))
            except Exception:
                pass
    return events


def user_msg(text):
    return {"type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}


def req_body(text):
    return {"model": "k3", "stream": True, "input": [user_msg(text)],
            "tools": [{"type": "web_search"}]}


async def run_case(handler, body):
    kb.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = httpx.ASGITransport(app=kb.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://b") as cli:
        return await cli.post("/v1/responses", json=body,
                              headers={"Authorization": "Bearer x"}, timeout=30)


def seqs(events):
    return [d.get("sequence_number") for _, d in events
            if isinstance(d.get("sequence_number"), int)]


def idx_of(events, pred):
    for i, (_, d) in enumerate(events):
        if pred(d):
            return i
    return -1


CHECKS = []


def check(name, ok):
    CHECKS.append((name, bool(ok)))


# ---------------------------------------------------------------- 场景


async def s1_two_round():
    """双轮（旧 1-7 + 新 8/9 的单轮部分）。"""
    r1 = sse([
        fr_created("resp_r1"),
        fr_fc_added("fc_1", "web_search"),
        fr_fc_delta("fc_1", json.dumps({"query": "今天的新闻"}, ensure_ascii=False)),
        fr_fc_done("fc_1", "web_search", json.dumps({"query": "今天的新闻"}, ensure_ascii=False)),
        fr_completed("resp_r1", [_fc_item("fc_1", "web_search",
                                          json.dumps({"query": "今天的新闻"}, ensure_ascii=False))],
                     {"input_tokens": 100, "output_tokens": 20}),
    ])
    r2 = sse([
        fr_created("resp_r2"),
        fr_msg_added("msg_1"),
        fr_msg_delta("msg_1", "答案正文"),
        fr_msg_done("msg_1", "答案正文"),
        fr_completed("resp_r2",
                     [{"id": "msg_1", "type": "message", "status": "completed",
                       "content": [{"type": "output_text", "text": "答案正文", "annotations": []}]}],
                     {"input_tokens": 150, "output_tokens": 30}),
    ])
    calls = {"n": 0, "followup_input": None, "queries": []}

    async def fake_search(query, items, chosen_pid, providers):
        calls["queries"].append(query)
        return "MOCK搜索结果"

    kb._execute_search = fake_search
    kb._SEARCH_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/responses") and request.method == "POST":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=r1)
            calls["followup_input"] = json.loads(request.content.decode("utf-8")).get("input")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=r2)
        return httpx.Response(404, content=b"{}")

    r = await run_case(handler, req_body("查一下今天的新闻"))
    check("S1 HTTP 200", r.status_code == 200)
    events = parse_sse(r.text)
    types_ = [d.get("type") for _, d in events]

    ws_added = [d for _, d in events if d.get("type") == "response.output_item.added"
                and (d.get("item") or {}).get("type") == "web_search_call"]
    ws_done = [d for _, d in events if d.get("type") == "response.output_item.done"
               and (d.get("item") or {}).get("type") == "web_search_call"]
    msg_added_i = idx_of(events, lambda d: d.get("type") == "response.output_item.added"
                         and (d.get("item") or {}).get("id") == "msg_1")

    # 旧 1：续轮 created 被丢弃
    check("S1[1] one response.created", types_.count("response.created") == 1)
    # 旧 2：原生 web_search_call（done 帧恰一个）
    check("S1[2] native web_search_call done", len(ws_done) == 1)
    # 新 8a：added 是 in_progress 壳且带 action.query
    check("S1[8a] added in_progress + action.query",
          len(ws_added) == 1
          and ws_added[0]["item"]["status"] == "in_progress"
          and (ws_added[0]["item"].get("action") or {}).get("query") == "今天的新闻")
    # 新 8b：事件序 added(in_progress) → done(completed) → follow-up 内容
    i_added = idx_of(events, lambda d: d is ws_added[0]) if ws_added else -1
    i_done = idx_of(events, lambda d: d is ws_done[0]) if ws_done else -1
    comp_i = idx_of(events, lambda d: d.get("type") == "response.completed")
    check("S1[8b] order added→done→follow-up→completed",
          0 <= i_added < i_done < msg_added_i < comp_i)
    check("S1[8c] done completed + action.query",
          ws_done and ws_done[0]["item"]["status"] == "completed"
          and (ws_done[0]["item"].get("action") or {}).get("query") == "今天的新闻")
    # 旧 3：msg output_index 前移 1
    msg_added = [d for _, d in events if d.get("type") == "response.output_item.added"
                 and (d.get("item") or {}).get("id") == "msg_1"]
    check("S1[3] msg_1 output_index == 1",
          bool(msg_added) and msg_added[0].get("output_index") == 1)
    # 旧 4：文本增量
    deltas = [d for _, d in events if d.get("type") == "response.output_text.delta"]
    check("S1[4] text delta arrived", any(d.get("delta") == "答案正文" for d in deltas))
    # 旧 5：合并 completed
    comps = [d for _, d in events if d.get("type") == "response.completed"]
    ok = len(comps) == 1
    if ok:
        rr = comps[0].get("response") or {}
        outs = rr.get("output") or []
        ok = (rr.get("id") == "resp_r1"
              and [it.get("type") for it in outs] == ["web_search_call", "message"]
              and (rr.get("usage") or {}).get("input_tokens") == 250
              and outs[0].get("status") == "completed"
              and (outs[0].get("action") or {}).get("query") == "今天的新闻")
    check("S1[5] merged completed (id/output/usage/action)", ok)
    # 旧 6：follow-up 带搜索结果
    fi = calls["followup_input"] or []
    outs = [it for it in fi if isinstance(it, dict) and it.get("type") == "function_call_output"]
    check("S1[6] follow-up has search result",
          calls["n"] == 2 and calls["queries"] == ["今天的新闻"]
          and bool(outs) and outs[-1].get("output") == "MOCK搜索结果"
          and outs[-1].get("call_id") == "fc_1")
    # 旧 7：follow-up input 顺序
    check("S1[7] follow-up input order",
          [it.get("type") for it in fi if isinstance(it, dict)]
          == ["message", "function_call", "function_call_output"])
    # 新 9：sequence_number 严格递增（含延迟 done 帧）
    sq = seqs(events)
    check("S1[9] sequence_number strictly increasing",
          len(sq) >= 6 and all(a < b for a, b in zip(sq, sq[1:])))


async def s2_empty_query_fallback():
    """空 query：added 无 action，done 回填 fallback 词。"""
    r1 = sse([
        fr_created("resp_e1"),
        fr_fc_added("fc_e1", "web_search"),
        fr_fc_done("fc_e1", "web_search", "{}"),
        fr_completed("resp_e1", [_fc_item("fc_e1", "web_search", "{}")],
                     {"input_tokens": 10, "output_tokens": 5}),
    ])
    r2 = sse([
        fr_created("resp_e2"),
        fr_msg_added("msg_e"),
        fr_msg_done("msg_e", "答案"),
        fr_completed("resp_e2",
                     [{"id": "msg_e", "type": "message", "status": "completed",
                       "content": [{"type": "output_text", "text": "答案", "annotations": []}]}],
                     {"input_tokens": 20, "output_tokens": 6}),
    ])
    got = {"q": None}

    async def fake_search(query, items, chosen_pid, providers):
        got["q"] = query
        return "FALLBACK结果"

    kb._execute_search = fake_search
    kb._SEARCH_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v1/responses"):
            if handler.n == 0:  # noqa
                handler.n = 1
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=r1)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=r2)
        return httpx.Response(404, content=b"{}")

    handler.n = 0
    r = await run_case(handler, req_body("查一下空查询场景"))
    check("S2 HTTP 200", r.status_code == 200)
    events = parse_sse(r.text)
    ws_added = [d for _, d in events if d.get("type") == "response.output_item.added"
                and (d.get("item") or {}).get("type") == "web_search_call"]
    ws_done = [d for _, d in events if d.get("type") == "response.output_item.done"
               and (d.get("item") or {}).get("type") == "web_search_call"]
    check("S2[13a] _execute_search got fallback query",
          got["q"] == "查一下空查询场景")
    check("S2[13b] added shell without action",
          len(ws_added) == 1 and ws_added[0]["item"]["status"] == "in_progress"
          and not (ws_added[0]["item"].get("action") or {}).get("query"))
    check("S2[13c] done backfills fallback query",
          len(ws_done) == 1 and ws_done[0]["item"]["status"] == "completed"
          and (ws_done[0]["item"].get("action") or {}).get("query") == "查一下空查询场景")


async def s3_multi_round():
    """多轮搜索：oi 前移、跨轮 seq 单调、逐轮 done 先于下轮内容。"""
    r1 = sse([
        fr_created("resp_m1"),
        fr_fc_added("fc_m1", "web_search"),
        fr_fc_done("fc_m1", "web_search", json.dumps({"query": "q1"})),
        fr_completed("resp_m1", [_fc_item("fc_m1", "web_search", json.dumps({"query": "q1"}))],
                     {"input_tokens": 10, "output_tokens": 1}),
    ])
    r2 = sse([
        fr_created("resp_m2"),
        fr_fc_added("fc_m2", "web_search"),
        fr_fc_done("fc_m2", "web_search", json.dumps({"query": "q2"})),
        fr_completed("resp_m2", [_fc_item("fc_m2", "web_search", json.dumps({"query": "q2"}))],
                     {"input_tokens": 20, "output_tokens": 2}),
    ])
    r3 = sse([
        fr_created("resp_m3"),
        fr_msg_added("msg_m"),
        fr_msg_done("msg_m", "最终答案"),
        fr_completed("resp_m3",
                     [{"id": "msg_m", "type": "message", "status": "completed",
                       "content": [{"type": "output_text", "text": "最终答案", "annotations": []}]}],
                     {"input_tokens": 30, "output_tokens": 3}),
    ])
    n = {"n": 0}

    async def fake_search(query, items, chosen_pid, providers):
        return f"RES:{query}"

    kb._execute_search = fake_search
    kb._SEARCH_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v1/responses"):
            n["n"] += 1
            payload = {1: r1, 2: r2, 3: r3}.get(n["n"], r3)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)
        return httpx.Response(404, content=b"{}")

    r = await run_case(handler, req_body("多轮搜索测试"))
    check("S3 HTTP 200", r.status_code == 200)
    events = parse_sse(r.text)

    def added_iid(iid):
        return idx_of(events, lambda d: d.get("type") == "response.output_item.added"
                      and (d.get("item") or {}).get("id") == iid)

    def done_call(cid):
        return idx_of(events, lambda d: d.get("type") == "response.output_item.done"
                      and (d.get("item") or {}).get("call_id") == cid)

    a1, a2, a3 = added_iid("fc_m1"), added_iid("fc_m2"), added_iid("msg_m")
    d1, d2 = done_call("fc_m1"), done_call("fc_m2")
    check("S3[10a] oi offsets (ws2=1, msg=2)",
          a1 >= 0 and a2 >= 0 and a3 >= 0
          and events[a1][1]["output_index"] == 0
          and events[a2][1]["output_index"] == 1
          and events[a3][1]["output_index"] == 2)
    check("S3[10b] per-round done precedes next round",
          0 <= d1 < a2 and 0 <= d2 < a3)
    sq = seqs(events)
    check("S3[9] cross-round sequence_number increasing",
          len(sq) >= 8 and all(a < b for a, b in zip(sq, sq[1:])))
    comps = [d for _, d in events if d.get("type") == "response.completed"]
    outs = ((comps[0].get("response") or {}).get("output") or []) if len(comps) == 1 else []
    check("S3[10c] merged three-round output",
          [it.get("type") for it in outs] == ["web_search_call", "web_search_call", "message"]
          and all(it.get("status") == "completed" for it in outs[:2]))


async def s4_mixed_tools_no_cont():
    """混合工具不续轮：completed 前兜底 flush，done 不悬挂。"""
    ws_args = json.dumps({"query": "混合工具查询"})
    r1 = sse([
        fr_created("resp_x1"),
        fr_fc_added("fc_x1", "web_search"),
        fr_fc_done("fc_x1", "web_search", ws_args),
        fr_fc_added("fc_x2", "exec_command"),
        fr_fc_done("fc_x2", "exec_command", '{"cmd":"dir"}'),
        fr_completed("resp_x1",
                     [_fc_item("fc_x1", "web_search", ws_args),
                      _fc_item("fc_x2", "exec_command", '{"cmd":"dir"}')],
                     {"input_tokens": 9, "output_tokens": 9}),
    ])
    n = {"n": 0}

    async def fake_search(query, items, chosen_pid, providers):
        return "SHOULD_NOT_RUN"

    kb._execute_search = fake_search
    kb._SEARCH_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v1/responses"):
            n["n"] += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=r1)
        return httpx.Response(404, content=b"{}")

    r = await run_case(handler, req_body("混合工具场景"))
    check("S4 HTTP 200", r.status_code == 200)
    events = parse_sse(r.text)
    comp_i = idx_of(events, lambda d: d.get("type") == "response.completed")
    ws_done_i = idx_of(events, lambda d: d.get("type") == "response.output_item.done"
                       and (d.get("item") or {}).get("type") == "web_search_call")
    ws_added_i = idx_of(events, lambda d: d.get("type") == "response.output_item.added"
                        and (d.get("item") or {}).get("type") == "web_search_call")
    exec_done_i = idx_of(events, lambda d: d.get("type") == "response.output_item.done"
                         and (d.get("item") or {}).get("name") == "exec_command")
    check("S4[11a] no follow-up (single upstream call)", n["n"] == 1)
    check("S4[11b] fallback done flushed before completed",
          0 <= ws_added_i < ws_done_i < comp_i)
    check("S4[11c] done keeps completed + original action",
          ws_done_i >= 0
          and events[ws_done_i][1]["item"]["status"] == "completed"
          and (events[ws_done_i][1]["item"].get("action") or {}).get("query") == "混合工具查询")
    check("S4[11d] sibling exec call passthrough intact", exec_done_i >= 0)


async def s5_followup_failure():
    """follow-up HTTP 500：done 已在 follow-up 前 flush，completed 仍收尾。"""
    r1 = sse([
        fr_created("resp_f1"),
        fr_fc_added("fc_f1", "web_search"),
        fr_fc_done("fc_f1", "web_search", json.dumps({"query": "失败场景"})),
        fr_completed("resp_f1", [_fc_item("fc_f1", "web_search", json.dumps({"query": "失败场景"}))],
                     {"input_tokens": 11, "output_tokens": 4}),
    ])
    n = {"n": 0}

    async def fake_search(query, items, chosen_pid, providers):
        return "结果但followup会挂"

    kb._execute_search = fake_search
    kb._SEARCH_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/v1/responses"):
            n["n"] += 1
            if n["n"] == 1:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=r1)
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(404, content=b"{}")

    r = await run_case(handler, req_body("follow-up 失败场景"))
    check("S5 HTTP 200 (stream already started)", r.status_code == 200)
    events = parse_sse(r.text)
    a = idx_of(events, lambda d: d.get("type") == "response.output_item.added"
               and (d.get("item") or {}).get("type") == "web_search_call")
    d_ = idx_of(events, lambda d: d.get("type") == "response.output_item.done"
                and (d.get("item") or {}).get("type") == "web_search_call")
    c = idx_of(events, lambda d: d.get("type") == "response.completed")
    check("S5[12a] follow-up attempted once", n["n"] == 2)
    check("S5[12b] added→done→completed tail intact", 0 <= a < d_ < c)
    check("S5[12c] completed is last event", c == len(events) - 1)


async def s6_roundtrip_fulfill_compat():
    """断言14：input 回程转换仍能从 arguments 取词；fulfill 正常替换 note。"""
    ws_call = {"type": "web_search_call", "id": "ws_c1", "call_id": "call_c1",
               "status": "completed", "execution": "client",
               "action": {"type": "search", "query": "compat q"},
               "arguments": {"query": "compat q"}}
    out = kb.normalize_input_item(dict(ws_call))
    fc = out[0]
    check("S6[14a] input web_search_call -> function_call",
          fc.get("type") == "function_call" and fc.get("name") == "web_search"
          and fc.get("call_id") == "call_c1")
    check("S6[14b] _extract_call_query still reads arguments",
          kb._extract_call_query(fc.get("arguments")) == "compat q")

    note = kb.ORPHAN_NOTES["web_search"]
    body = {"input": [
        {"type": "function_call", "name": "web_search", "call_id": "call_c2",
         "arguments": json.dumps({"query": "fulfill q"})},
        {"type": "function_call_output", "call_id": "call_c2", "output": note},
    ]}

    async def fake_search(query, items, chosen_pid, providers):
        return "FULFILLED"

    kb._execute_search = fake_search
    kb._SEARCH_CACHE.clear()
    raw = await kb.fulfill_web_searches(json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                        "kimi", {"kimi": {}})
    got = json.loads(raw)["input"][1]
    check("S6[14c] fulfill replaces orphan note", got.get("output") == "FULFILLED")


async def s7_rollout_shape_fulfill():
    """S7: rollout real shape (web_search_call with only action) + delegation-only
    user messages — fulfill must re-execute each exact action.query (incident 2026-08-31)."""
    items = [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text",
                      "text": "<environment_context>\n<cwd>E:\\code</cwd>\n</environment_context>"}]},
        {"type": "message", "role": "user",
         "content": [{"type": "input_text",
                      "text": "<codex_delegation>\n<input>调研运行时信息注入</input>\n</codex_delegation>"}]},
    ]
    queries = ["LangChain context injection", "Anthropic prompt caching"]
    for i, q in enumerate(queries):
        items.append({"type": "web_search_call", "id": f"fc_call_s7_{i}",
                      "status": "completed",
                      "action": {"type": "search", "query": q},
                      "internal_chat_message_metadata_passthrough": {"turn_id": "t7"}})
    executed = []

    async def fake_native(pid, pinfo, q):
        executed.append(q)
        return f"MOCK结果:{q}"

    kb._execute_search = _ORIG_EXECUTE_SEARCH
    kb._native_search = fake_native
    kb._SEARCH_CACHE.clear()
    raw = kb.normalize_body(json.dumps({"input": items}, ensure_ascii=False).encode("utf-8"))
    check("S7[15a] delegation-only fallback recovers <input> body",
          kb._fallback_query(json.loads(raw)["input"]) == "调研运行时信息注入")
    out = await kb.fulfill_web_searches(
        raw, "zhipu",
        {"zhipu": {"upstream": "http://x", "key": "k", "active_model": "m"}})
    ob = json.loads(out)
    replaced = [it for it in ob["input"]
                if isinstance(it, dict) and str(it.get("output", "")).startswith("MOCK结果:")]
    check("S7[15b] fulfill replaced both notes via action.query",
          len(replaced) == 2 and executed == queries)
    fc_args = [it.get("arguments") for it in ob["input"]
               if isinstance(it, dict) and it.get("type") == "function_call"
               and it.get("name") == "web_search"]
    check("S7[15c] normalized function_call keeps original query",
          [json.loads(a).get("query") for a in fc_args] == queries)


async def s8_delegation_fallback_and_warning():
    """S8: delegation <input> body works as query fallback; a fully unusable
    query must be observable (warning + stats event), never silent."""
    check("S8[16a] _extract_delegation_text recovers <input> body",
          kb._extract_delegation_text(
              "<codex_delegation>\n<input>调研运行时注入</input>\n</codex_delegation>"
          ) == "调研运行时注入")
    check("S8[16b] malformed delegation falls back to original",
          kb._extract_delegation_text(
              "<codex_delegation>no input tag</codex_delegation>"
          ).startswith("<codex_delegation>"))
    check("S8[16c] _fallback_query uses delegation <input> body",
          kb._fallback_query([{
              "type": "message", "role": "user",
              "content": [{"type": "input_text",
                           "text": "<codex_delegation>\n<input>调研运行时信息注入最佳实践</input>\n</codex_delegation>"}],
          }]) == "调研运行时信息注入最佳实践")

    events = []
    kb._execute_search = _ORIG_EXECUTE_SEARCH  # undo S6/S7 mocks — test the real path
    kb.stats.note_event = lambda name, detail="": events.append((name, detail))
    got = await kb._execute_search(
        "", [{"type": "message", "role": "user",
              "content": [{"type": "input_text",
                           "text": "<codex_delegation>无 input 标签</codex_delegation>"}]}],
        "zhipu", {})
    check("S8[16d] no-query call returns note + emits event",
          got == kb.ORPHAN_NOTES["web_search"]
          and events and events[-1][0] == "web_search_no_query")


async def s9_final_round_wrapup():
    """S9: at SEARCH_CONT_MAX the final follow-up drops web_search and injects
    a wrap-up instruction — every search really executed, final text produced,
    no fake 1ms-completed search."""
    old_max = kb.SEARCH_CONT_MAX
    kb.SEARCH_CONT_MAX = 3
    try:
        def ws_resp(rid, fc_id, q):
            args = json.dumps({"query": q}, ensure_ascii=False)
            return sse([
                fr_created(rid),
                fr_fc_added(fc_id, "web_search"),
                fr_fc_delta(fc_id, args),
                fr_fc_done(fc_id, "web_search", args),
                fr_completed(rid, [_fc_item(fc_id, "web_search", args)],
                             {"input_tokens": 10, "output_tokens": 2}),
            ])

        finals = sse([
            fr_created("resp_final"),
            fr_msg_added("msg_final"),
            fr_msg_delta("msg_final", "最终结论"),
            fr_msg_done("msg_final", "最终结论"),
            fr_completed("resp_final",
                         [{"id": "msg_final", "type": "message", "status": "completed",
                           "content": [{"type": "output_text", "text": "最终结论", "annotations": []}]}],
                         {"input_tokens": 30, "output_tokens": 9}),
        ])
        responses = [ws_resp("resp_w1", "fc_w1", "q1"),
                     ws_resp("resp_w2", "fc_w2", "q2"),
                     ws_resp("resp_w3", "fc_w3", "q3"),
                     finals]
        n = {"n": 0}
        captured = []
        searches = []

        async def fake_search(query, items, chosen_pid, providers):
            searches.append(query)
            return f"RES:{query}"

        kb._execute_search = fake_search
        kb._SEARCH_CACHE.clear()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path.endswith("/v1/responses"):
                idx = n["n"]
                n["n"] += 1
                if idx > 0:
                    captured.append(json.loads(request.content.decode("utf-8")))
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      content=responses[min(idx, len(responses) - 1)])
            return httpx.Response(404, content=b"{}")

        body = req_body("深度调研问题")
        body["tools"] = [
            {"type": "web_search"},
            {"type": "function", "name": "exec_command", "description": "exec",
             "parameters": {"type": "object", "properties": {}}},
        ]
        r = await run_case(handler, body)
        check("S9 HTTP 200", r.status_code == 200)
        events = parse_sse(r.text)
        ws_done = [d for _, d in events
                   if d.get("type") == "response.output_item.done"
                   and (d.get("item") or {}).get("type") == "web_search_call"]
        comp = [d for _, d in events if d.get("type") == "response.completed"]
        msg_done = [d for _, d in events
                    if d.get("type") == "response.output_item.done"
                    and (d.get("item") or {}).get("id") == "msg_final"]

        check("S9[17a] exactly 4 upstream calls (initial + 3 follow-ups)", n["n"] == 4)
        check("S9[17b] every search really executed (no fake done)",
              len(searches) == 3 and len(ws_done) == 3
              and [d["item"].get("call_id") for d in ws_done] == ["fc_w1", "fc_w2", "fc_w3"]
              and [(d["item"].get("action") or {}).get("query") for d in ws_done] == ["q1", "q2", "q3"])
        last_fu = captured[-1] if captured else {}
        final_tools = [t.get("name") for t in (last_fu.get("tools") or [])
                       if isinstance(t, dict)]
        last_in = last_fu.get("input") or []
        wrapup = any(isinstance(it, dict) and it.get("type") == "message"
                     and "Search round budget (3) is exhausted" in json.dumps(
                         it.get("content"), ensure_ascii=False)
                     for it in last_in)
        check("S9[17c] final follow-up drops web_search but keeps other tools",
              len(captured) == 3 and "web_search" not in final_tools
              and "exec_command" in final_tools)
        check("S9[17d] final follow-up carries wrap-up instruction", wrapup)
        check("S9[17e] wrap-up text arrives and completes cleanly",
              bool(msg_done) and len(comp) == 1
              and comp[0].get("type") == "response.completed"
              and [it.get("type") for it in (comp[0].get("response") or {}).get("output", [])]
              == ["web_search_call", "web_search_call", "web_search_call", "message"])
    finally:
        kb.SEARCH_CONT_MAX = old_max


async def main():
    await s1_two_round()
    await s2_empty_query_fallback()
    await s3_multi_round()
    await s4_mixed_tools_no_cont()
    await s5_followup_failure()
    await s6_roundtrip_fulfill_compat()
    await s7_rollout_shape_fulfill()
    await s8_delegation_fallback_and_warning()
    await s9_final_round_wrapup()

    failed = 0
    for name, ok in CHECKS:
        print(("PASS " if ok else "FAIL "), name)
        failed += 0 if ok else 1
    print(f"total={len(CHECKS)} failed={failed}")
    print("ALL PASS" if failed == 0 else "SOME FAILED")
    sys.exit(0 if failed == 0 else 1)


asyncio.run(main())
