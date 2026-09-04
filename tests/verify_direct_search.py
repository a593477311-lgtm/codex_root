"""
verify_direct_search.py — 直连搜索层离线单测。

不依赖运行中的桥与外网：直接 import bridge/kimi_bridge.py 的纯函数，
用三家真实响应形状（调研时录制）验证解析、格式化、链序、缓存与 UX 适配。

用法:  python tests/verify_direct_search.py
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import kimi_bridge as kb  # noqa: E402

results = []


def check(name, ok, fail_detail=""):
    results.append((name, ok, fail_detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {fail_detail}" if not ok and fail_detail else ""))


def run(coro):
    return asyncio.run(coro)


PROVIDERS = {
    "zhipu": {"upstream": "https://open.bigmodel.cn/api", "key": "test-key"},
    "minimax": {"upstream": "https://api.minimaxi.com", "key": "test-key"},
    "kimi": {"upstream": "https://api.kimi.com/coding", "key": "test-key"},
}

ITEMS = [{"type": "message", "role": "user",
          "content": [{"type": "input_text", "text": "测试查询"}]}]


# --- 1. 三家响应解析与归一化格式 -------------------------------------------------

ZHIPU_RESP = {"search_result": [
    {"title": "智谱结果A", "link": "https://a.example.com", "content": "摘要A",
     "publish_date": "2026-09-01", "media": "示例站"},
    {"title": "智谱结果B", "link": "https://b.example.com", "content": "摘要B",
     "publish_date": "", "media": ""},
]}
txt = kb._fmt_direct_results(ZHIPU_RESP["search_result"], title="title", link="link",
                             snippet="content", date="publish_date", site="media", limit=5)
check("1a 智谱结果格式化",
      txt is not None and txt.startswith("[1] 智谱结果A\nURL: https://a.example.com")
      and "日期: 2026-09-01" in txt and "来源: 示例站" in txt
      and "[2] 智谱结果B" in txt and "日期:" not in txt.split("[2]")[1],
      repr(txt))

MINIMAX_RESP = {"organic": [
    {"title": "MiniMax结果", "link": "https://mm.example.com",
     "snippet": "MiniMax摘要", "date": "2026/07/01 18:19:35"},
]}
txt = kb._fmt_direct_results(MINIMAX_RESP["organic"], title="title", link="link",
                             snippet="snippet", date="date", limit=5)
check("1b MiniMax结果格式化",
      txt is not None and "[1] MiniMax结果" in txt and "URL: https://mm.example.com" in txt
      and "日期: 2026/07/01 18:19:35" in txt and "摘要: MiniMax摘要" in txt,
      repr(txt))

KIMI_RESP = {"search_results": [
    {"title": "Kimi结果", "url": "https://kimi.example.com", "snippet": "Kimi摘要",
     "date": "2026-08-30", "site_name": "Kimi站"},
]}
txt = kb._fmt_direct_results(KIMI_RESP["search_results"], title="title", link="url",
                             snippet="snippet", date="date", site="site_name", limit=5)
check("1c Kimi结果格式化",
      txt is not None and "[1] Kimi结果" in txt and "URL: https://kimi.example.com" in txt
      and "来源: Kimi站" in txt,
      repr(txt))

check("1d 空结果返回 None",
      kb._fmt_direct_results([], title="t", link="l", snippet="s", date="d") is None
      and kb._fmt_direct_results([{"title": "", "link": ""}], title="title", link="link",
                                 snippet="s", date="d") is None)

check("1e top_k 截断",
      (kb._fmt_direct_results([{"title": f"r{i}", "link": f"https://x/{i}", "snippet": "", "date": ""}
                               for i in range(9)],
                              title="title", link="link", snippet="snippet", date="date", limit=3)
       or "").count("URL:") == 3)


# --- 2. top_k 解析 ---------------------------------------------------------------

check("2 top_k 解析",
      kb._extract_call_top_k(json.dumps({"query": "q", "top_k": 8})) == 8
      and kb._extract_call_top_k({"query": "q", "limit": 20}) == 10   # clamp 上限
      and kb._extract_call_top_k({"query": "q", "top_k": 0}) == 5     # 0 -> 默认
      and kb._extract_call_top_k("not-json") == 5
      and kb._extract_call_top_k({"query": "q"}) == 5)


# --- 3. 直连链序与 LLM 兜底 ------------------------------------------------------


def reset():
    kb._QUERY_CACHE.clear()
    kb._SEARCH_CACHE.clear()


async def test_chain():
    order = []

    async def fz(pinfo, query, top_k=5):
        order.append("zhipu")
        return "LINKLESS_RESULT"  # 无 URL -> 质量门丢弃 -> 降级

    async def fm(pinfo, query, top_k=5):
        order.append("minimax")
        return "MINIMAX_DIRECT_RESULT\nURL: https://mm.example.com"

    async def fk(pinfo, query, top_k=5):
        order.append("kimi")
        return "KIMI_RESULT"

    async def fnllm(pid, pinfo, query):
        order.append(f"llm:{pid}")
        return None

    keep = (kb._search_zhipu_direct, kb._search_minimax_direct, kb._search_kimi_direct, kb._native_search)
    try:
        kb._search_zhipu_direct, kb._search_minimax_direct = fz, fm
        kb._search_kimi_direct, kb._native_search = fk, fnllm
        reset()
        out = await kb._execute_search("链序测试", ITEMS, None, PROVIDERS, 5)
        ok1 = out is not None and out.startswith("MINIMAX_DIRECT_RESULT") and order == ["zhipu", "minimax"]

        # 直连全败 -> LLM 兜底链（chosen_pid 优先）
        order.clear()

        async def fz2(pinfo, query, top_k=5):
            return None

        async def fm2(pinfo, query, top_k=5):
            return None

        async def fk2(pinfo, query, top_k=5):
            return None

        async def fnllm2(pid, pinfo, query):
            order.append(f"llm:{pid}")
            return f"LLM_{pid}"

        kb._search_zhipu_direct, kb._search_minimax_direct = fz2, fm2
        kb._search_kimi_direct, kb._native_search = fk2, fnllm2
        reset()
        out = await kb._execute_search("兜底测试", ITEMS, "minimax", PROVIDERS, 5)
        ok2 = out == "LLM_minimax" and order[0] == "llm:minimax"  # chosen_pid 优先进 LLM 链
        return ok1 and ok2, f"stage1_order_mismatch or out={out!r} order={order}"
    finally:
        (kb._search_zhipu_direct, kb._search_minimax_direct,
         kb._search_kimi_direct, kb._native_search) = keep


ok, detail = run(test_chain())
check("3 直连链序 + LLM 兜底", ok, detail)


# --- 4. query TTL 缓存 -----------------------------------------------------------


async def test_cache():
    calls = []

    async def fz(pinfo, query, top_k=5):
        calls.append(query)
        return "CACHED_RESULT\nURL: https://x.example.com"

    keep = kb._search_zhipu_direct
    try:
        kb._search_zhipu_direct = fz
        reset()
        a = await kb._execute_search("缓存测试", ITEMS, None, PROVIDERS, 5)
        b = await kb._execute_search("缓存测试", ITEMS, None, PROVIDERS, 5)  # 第二次应命中缓存
        return (a is not None and a.startswith("CACHED_RESULT") and a == b
                and len(calls) == 1), f"calls={calls}"
    finally:
        kb._search_zhipu_direct = keep


ok, detail = run(test_cache())
check("4 query TTL 缓存", ok, detail)


# --- 5. Codex UX：action 双字段 --------------------------------------------------

item, changed = kb.rewrite_output_item({
    "type": "function_call", "id": "fc_1", "call_id": "ws_1", "name": "web_search",
    "arguments": json.dumps({"query": "Codex CLI 最新版本"}),
})
act = (item or {}).get("action") or {}
check("5 action query+queries 双填",
      changed and item.get("type") == "web_search_call"
      and act.get("type") == "search"
      and act.get("query") == "Codex CLI 最新版本"
      and act.get("queries") == ["Codex CLI 最新版本"],
      json.dumps(item, ensure_ascii=False))


# --- 6. fulfill_web_searches 孤儿替换 -------------------------------------------


async def test_fulfill():
    async def fexec(query, items, chosen_pid, providers, top_k=5):
        return "REAL_SEARCH_RESULTS"

    keep = kb._execute_search
    try:
        kb._execute_search = fexec
        reset()
        raw = json.dumps({"input": [
            {"type": "function_call", "name": "web_search", "call_id": "ws_t",
             "arguments": json.dumps({"query": "孤儿测试", "top_k": 7})},
            {"type": "function_call_output", "call_id": "ws_t",
             "output": kb.ORPHAN_NOTES["web_search"]},
        ]}, ensure_ascii=False).encode("utf-8")
        out = json.loads(await kb.fulfill_web_searches(raw, None, PROVIDERS))
        return out["input"][1]["output"] == "REAL_SEARCH_RESULTS", json.dumps(out, ensure_ascii=False)
    finally:
        kb._execute_search = keep


ok, detail = run(test_fulfill())
check("6 fulfill 孤儿替换", ok, detail)


# --- 汇总 -----------------------------------------------------------------------

failed = [r for r in results if not r[1]]
print(f"\n{'='*40}\n直连搜索单测: {len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
