"""
kimi_bridge.py — Codex <-> Kimi For Coding normalization bridge.

Kimi's /responses endpoint rejects several item types Codex 0.149+ emits.
This proxy bridges the gap in BOTH directions — nothing is disabled,
every tool stays usable:

Request path (body):
  - tools: unsupported tool types (tool_search, web_search, ...) are
    converted to equivalent function tools; namespace blocks are FLATTENED
    into their member function tools (bare names). Tools discovered via
    tool_search (deferred loading) are re-injected from history into the
    tools array. Every tool schema passes a sanitizer that inlines
    $ref/$defs (Kimi rejects "$ref" with sibling keys).
  - input: conversation-history items like tool_search_call /
    tool_search_output (produced when the app executes bridged tools
    client-side) are converted to plain function_call /
    function_call_output pairs; non-string payloads are JSON-stringified.

Response path (SSE stream and JSON bodies):
  - a function_call whose name belongs to a converted special tool is
    buffered (arguments stream as deltas) and re-emitted as the native
    item type the Codex app dispatches client-side:
    tool_search_call / web_search_call (arguments as an OBJECT,
    execution="client", status="completed").
  - a function_call for a namespaced dynamic tool gets its
    "namespace" field restored (name->namespace map is learned from
    namespace blocks and tool_search_output items flowing through,
    persisted to namespace_map.json).

Listen:   http://127.0.0.1:15731/v1
Upstream: https://api.kimi.com/coding/v1
Log:      kimi_bridge.log next to this script
"""

import json
import logging
import os
import re

import sys
import time
import threading

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from logging.handlers import RotatingFileHandler

import bridge_stats
import dashboard as dash
from bridge_stats import extract_usage, stats

CFG = dash.load_config()
UPSTREAM = CFG["upstream"]
HOST = CFG["host"]
PORT = CFG["port"]
NS_MAP_FILE = os.path.join(_HERE, "namespace_map.json")
LOG_FILE = os.path.join(_HERE, "kimi_bridge.log")

logging.basicConfig(
    handlers=[RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")],
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("kimi-bridge")
stats.configure(retention_days=CFG["stats_retention_days"], enabled=CFG["stats_enabled"])

# --- special tools the app executes client-side -------------------------------
# name of the bridged function -> native call item type the app dispatches
SPECIAL_CALL_TYPES = {"tool_search": "tool_search_call", "web_search": "web_search_call"}
special_names = dict(SPECIAL_CALL_TYPES)  # extended when convert uses a custom name

# parameter schemas given to the model for bridged special tools
SPECIAL_PARAMS = {
    "tool_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search keywords for tools"},
            "limit": {"type": "integer", "description": "Max number of results"},
        },
        "additionalProperties": True,
    },
    "web_search": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Search query"}},
        "additionalProperties": True,
    },
}

# Non-OpenAI models do not reliably know Codex's V4A patch language.  The
# original freeform tool carries a Lark grammar, but that grammar is not sent
# upstream after converting the tool to a plain function.  Keep the key syntax
# in the model-visible description, especially for Gemini.
APPLY_PATCH_FORMAT_GUIDE = """

Patch format (required):
*** Begin Patch
*** Add File: path/to/new-file
+file contents
*** Update File: path/to/existing-file
@@ optional context
-old line
+new line
*** Delete File: path/to/file
*** End Patch

Use exactly Add File, Update File, or Delete File. Never use Create File,
New File, Modify File, or JSON wrapping.
"""

# function-name -> namespace, learned from traffic, persisted to disk
name_to_ns: dict = {}


def load_ns_map():
    try:
        with open(NS_MAP_FILE, encoding="utf-8") as f:
            name_to_ns.update(json.load(f))
        if name_to_ns:
            log.info("loaded %d namespace mappings", len(name_to_ns))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("failed to load %s: %s", NS_MAP_FILE, e)


def learn_ns(name, ns):
    if not CFG.get("learn_namespaces", True):
        return
    if not name or not ns or name_to_ns.get(name) == ns:
        return
    name_to_ns[name] = ns
    stats.note_event("namespace_learn", f"{name} -> {ns}")
    try:
        with open(NS_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(name_to_ns, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.warning("failed to save ns map: %s", e)
    log.info("  learned: %s -> namespace '%s'", name, ns)


def learn_from_tool_defs(defs, default_ns=None):
    if not isinstance(defs, list):
        return
    for d in defs:
        if not isinstance(d, dict):
            continue
        if d.get("type") == "namespace":
            learn_from_tool_defs(d.get("tools"), d.get("name"))
        elif d.get("type") == "function" and d.get("name"):
            learn_ns(d["name"], default_ns)


def flatten_tool_defs(defs, default_ns=None):
    """Flatten (possibly namespaced) tool definitions into plain function
    tools, stripping defer_loading — used both to learn from and to
    re-inject tools discovered via tool_search."""
    out = []
    if not isinstance(defs, list):
        return out
    for d in defs:
        if not isinstance(d, dict):
            continue
        if d.get("type") == "namespace":
            out.extend(flatten_tool_defs(d.get("tools"), d.get("name")))
        elif d.get("type") == "function" and d.get("name"):
            st = {k: v for k, v in d.items() if k != "defer_loading"}
            _clean_tool_params(st, "injected tool")
            if default_ns:
                learn_ns(st["name"], default_ns)
            out.append(st)
    return out


def collect_deferred_tools(inp) -> dict:
    """Tools discovered via tool_search must become callable on the NEXT
    request (the deferred-loading contract). On the OpenAI path the server
    does this; here the bridge merges them from history into tools.
    Stateless: every request carries the full history."""
    found = {}
    if not isinstance(inp, list):
        return found
    for it in inp:
        if isinstance(it, dict) and it.get("type") == "tool_search_output":
            for d in flatten_tool_defs(it.get("tools")):
                found.setdefault(d["name"], d)
    return found


# --- request path: tools ------------------------------------------------------


def normalize_tool(t: dict) -> dict:
    """Keep the tool usable; only reshape what Kimi rejects."""
    ty = t.get("type")
    if ty == "function":
        _clean_tool_params(t, "tool")
        return t
    name = t.get("name") or ty or "unknown_tool"
    desc = (t.get("description") or "").strip()
    if ty in SPECIAL_CALL_TYPES:
        # app dispatches these natively by item type; bridge them back on the
        # response path (see rewrite_output_item).
        special_names[name] = SPECIAL_CALL_TYPES[ty]
        params = dict(SPECIAL_PARAMS.get(ty) or {"type": "object", "properties": {}, "additionalProperties": True})
        if not desc:
            desc = {
                "tool_search": "Search available (deferred-loading) tools by keyword.",
                "web_search": "Search the web.",
            }.get(ty, ty)
        log.info("  convert tool type '%s' -> function '%s' (native rewrite on response)", ty, name)
        stats.note_event("tool_convert", f"{ty} -> {name}")
        return {"type": "function", "name": name, "description": desc, "parameters": params}
    if ty == "custom":
        # freeform custom tool (e.g. apply_patch): give the model a single
        # raw-text slot, and restore the native custom_tool_call on response.
        special_names[name] = "custom_tool_call"
        params = {
            "type": "object",
            "properties": {"input": {
                "type": "string",
                "description": "Raw payload for this freeform tool (for apply_patch: the full *** Begin Patch text)."}},
            "required": ["input"],
            "additionalProperties": False,
        }
        if name == "apply_patch":
            desc = desc.rstrip() + APPLY_PATCH_FORMAT_GUIDE
        log.info("  convert tool type 'custom' -> function '%s' (freeform rewrite on response)", name)
        stats.note_event("tool_convert", f"custom -> {name}")
        return {"type": "function", "name": name, "description": desc, "parameters": params}
    desc = f"[bridged from '{ty}'] {desc}".strip()
    params = t.get("parameters")
    if not isinstance(params, dict) or not params:
        params = {"type": "object", "properties": {}, "additionalProperties": True}
    params.setdefault("type", "object")
    log.info("  convert tool type '%s' -> function '%s'", ty, name)
    stats.note_event("tool_convert", f"{ty} -> {name}")
    return {"type": "function", "name": name, "description": desc, "parameters": params}


def normalize_tools_list(tools: list) -> list:
    out = []
    seen_names = set()

    def _add(t_dict):
        if not isinstance(t_dict, dict):
            out.append(t_dict)
            return
        name = t_dict.get("name")
        if name:
            if name in seen_names:
                log.debug("  skip duplicate tool declaration: %s", name)
                return
            seen_names.add(name)
        out.append(t_dict)

    for t in tools:
        if not isinstance(t, dict):
            out.append(t)
            continue
        if t.get("type") == "namespace":
            # flatten: expose member tools as plain functions (bare names)
            ns = t.get("name")
            n = 0
            for sub in t.get("tools") or []:
                if isinstance(sub, dict) and sub.get("type") == "function" and sub.get("name"):
                    st = {k: v for k, v in sub.items() if k != "defer_loading"}
                    _clean_tool_params(st, f"namespace tool[{ns}]")
                    learn_ns(sub["name"], ns)
                    _add(st)
                    n += 1
                else:
                    log.info("  skip subtool in namespace '%s': %r", ns, sub if not isinstance(sub, dict) else sub.get("type"))
            log.info("  flatten namespace '%s': %d function tools exposed", ns, n)
            continue
        _add(normalize_tool(t))
    return out


# --- request path: input history + schema sanitizer ----------------------------


def _as_str(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def sanitize_schema(node, defs=None, _resolving=frozenset()):
    """Inline $ref nodes — Kimi rejects '$ref' carrying sibling keys
    ("when using $ref, type should be defined in the referenced schema
    instead of the parent schema"). Sibling keys override the referenced
    schema (JSON Schema AND-semantics); cyclic or unresolvable refs
    degrade to a generic object. The $defs block is dropped afterwards."""
    if isinstance(node, list):
        return [sanitize_schema(x, defs, _resolving) for x in node]
    if not isinstance(node, dict):
        return node
    if defs is None:
        defs = node.get("$defs") or {}
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1] if ref.startswith("#/") else None
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        target = (defs or {}).get(name) if name else None
        if target is None or name in _resolving:
            siblings.setdefault("type", "object")
            return sanitize_schema(siblings, defs, _resolving)
        return sanitize_schema({**target, **siblings}, defs, _resolving | {name})
    return {k: sanitize_schema(v, defs, _resolving) for k, v in node.items() if k != "$defs"}


def _clean_tool_params(st: dict, ctx: str):
    p = st.get("parameters")
    if not isinstance(p, dict):
        return
    had_ref = '"$ref"' in json.dumps(p, ensure_ascii=False)
    p2 = sanitize_schema(p)
    if "type" not in p2:
        p2["type"] = "object"
    if had_ref:
        log.info("  sanitize schema ($ref inlined): %s '%s'", ctx, st.get("name"))
    st["parameters"] = p2


def normalize_input_item(item):
    """Map conversation-history items onto plain function_call /
    function_call_output pairs Kimi understands. Returns a list."""
    if not isinstance(item, dict):
        return [item]
    ty = item.get("type")
    if not isinstance(ty, str):
        return [item]
    if ty == "function_call":
        ns = item.pop("namespace", None)  # app-side metadata; Kimi rejects unknown fields
        if ns and item.get("name"):
            learn_ns(item["name"], ns)
        if "arguments" in item and not isinstance(item["arguments"], str):
            item["arguments"] = _as_str(item["arguments"])
        return [item]
    if ty == "function_call_output":
        if not isinstance(item.get("output"), str):
            item["output"] = _as_str(item.get("output"))
        return [item]
    if ty == "tool_search_output":
        learn_from_tool_defs(item.get("tools"))
        val = item.get("tools", item.get("output", ""))
        log.info("  convert input 'tool_search_output' -> function_call_output")
        return [{
            "type": "function_call_output",
            "call_id": item.get("call_id") or item.get("id"),
            "output": _as_str({"tools": val} if isinstance(val, list) else val),
        }]
    if ty.endswith("_call"):
        # tool_search_call / web_search_call / custom_tool_call / *_call
        cid = item.get("call_id") or item.get("id")
        args = item.get("arguments")
        if args is None and "input" in item:
            args = {"input": item["input"]}
        if args is None:
            # The Codex app records web_search_call as {id, status,
            # action:{query}} with no call_id/arguments — recover the query
            # from action so fulfill_web_searches can re-execute the exact
            # original search (delegated threads have no usable fallback).
            action = item.get("action")
            if isinstance(action, dict) and action.get("type") == "search":
                aq = action.get("query")
                if isinstance(aq, str) and aq.strip():
                    args = {"query": aq}
        log.info("  convert input '%s' -> function_call '%s'", ty, item.get("name") or ty[:-5])
        out = [{
            "type": "function_call",
            "name": item.get("name") or ty[:-5],
            "arguments": _as_str(args if args is not None else {}),
            "call_id": cid,
        }]
        embedded = item.get("output", item.get("results"))
        if embedded is not None:
            out.append({"type": "function_call_output", "call_id": cid, "output": _as_str(embedded)})
        return out
    if ty.endswith("_output"):
        val = item.get("output", item.get("result", item.get("content", "")))
        log.info("  convert input '%s' -> function_call_output", ty)
        return [{
            "type": "function_call_output",
            "call_id": item.get("call_id") or item.get("id"),
            "output": _as_str(val),
        }]
    return [item]


ORPHAN_NOTES = {
    "web_search": ("[bridge note] web_search has server-side semantics in the Codex app: the call "
                   "was recorded but never executed, and no results are relayed through this bridge. "
                   "Answer from your own knowledge, or use exec_command (curl/Invoke-WebRequest) "
                   "if you truly need the web page."),
}


def repair_orphan_calls(items: list) -> list:
    """The app records server-semantic tool calls (web_search_call, ...) in
    history WITHOUT any result item. Converted to plain function_call those
    become orphan tool_calls, which Kimi rejects ("an assistant message with
    'tool_calls' must be followed by tool messages responding to each
    'tool_call_id'"). Synthesize the missing function_call_output, telling
    the model the truth so it can pick another tool."""
    answered = {it.get("call_id") for it in items
                if isinstance(it, dict) and it.get("type") == "function_call_output"}
    out = []
    for it in items:
        out.append(it)
        if isinstance(it, dict) and it.get("type") == "function_call":
            cid = it.get("call_id")
            if cid and cid not in answered:
                name = it.get("name") or ""
                note = ORPHAN_NOTES.get(
                    name,
                    "[bridge note] this client-side tool call produced no captured result; "
                    "treat it as completed without output.",
                )
                out.append({"type": "function_call_output", "call_id": cid, "output": note})
                log.info("  synthesized function_call_output for orphan call '%s' (call_id=%s)", name, cid)
    return out


def normalize_body(raw: bytes) -> bytes:
    try:
        body = json.loads(raw)
    except Exception:
        return raw
    inp = body.get("input")
    deferred = collect_deferred_tools(inp)
    tools = body.get("tools")
    if isinstance(tools, list):
        # packet capture: full tool list as Codex actually sent it
        log.info("tools (%d): %s", len(tools), json.dumps(tools, ensure_ascii=False))
        body["tools"] = normalize_tools_list(tools)
        if deferred:
            existing = {t.get("name") for t in body["tools"] if isinstance(t, dict)}
            added = []
            for n, d in deferred.items():
                if n not in existing:
                    body["tools"].append(d)
                    added.append(n)
            if added:
                log.info("  injected %d deferred tools from tool_search history: %s", len(added), added)
                stats.note_event("deferred_inject", f"{len(added)} tools: {','.join(added[:8])}")
    if isinstance(inp, list):
        log.info("input items (%d): %s", len(inp), [
            (it.get("type"), it.get("name") or it.get("role")) if isinstance(it, dict) else type(it).__name__
            for it in inp
        ])
        body["input"] = repair_orphan_calls([r for it in inp for r in normalize_input_item(it)])
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


# --- bridge-executed web_search ------------------------------------------------
# web_search has server-side semantics in the Codex app: through the bridge the
# call is recorded but never executed, so repair_orphan_calls fills a note.
# Here we upgrade those notes into REAL results by re-issuing the query against
# a provider whose upstream natively executes {"type": "web_search"}.
SEARCH_CAPABLE = ("gemini", "zhipu", "kimi", "minimax")
_SEARCH_CACHE = {}                       # call_id -> final output text (results or note)

# Responses 形状的按供应商额外载荷
NATIVE_SEARCH_EXTRA = {"minimax": {"tool_choice": "required"}}


async def _native_search(pid, pinfo, query):
    if pid == "kimi":
        return await _native_search_kimi(pinfo, query)
    if pid == "gemini":
        return await _native_search_gemini(pinfo, query)
    up = (pinfo.get("upstream") or "").rstrip("/")
    key = pinfo.get("key")
    model = pinfo.get("active_model") or (pinfo.get("models") or [None])[0]
    if not (up and key and model and query):
        return None
    payload = {
        "model": model,
        "input": [{"role": "user", "content": f"请联网搜索并简要回答:{query}"}],
        "tools": [{"type": "web_search"}],
        "stream": False,
    }
    payload.update(NATIVE_SEARCH_EXTRA.get(pid) or {})
    try:
        r = await client.post(f"{up}/v1/responses", json=payload,
                              headers={"Authorization": f"Bearer {key}"}, timeout=60)
        if r.status_code != 200:
            log.warning("native search via %s: HTTP %s", pid, r.status_code)
            return None
        data = r.json()
        parts = []
        executed = False
        for it in data.get("output") or []:
            if it.get("type") == "web_search_call":
                executed = True   # 上游真执行了搜索——与"无视工具脑补"区分的证据
                act = it.get("action") or {}
                srcs = [s.get("url") for s in (act.get("sources") or []) if isinstance(s, dict) and s.get("url")]
                if srcs:
                    parts.append("来源: " + ", ".join(srcs[:8]))
            elif it.get("type") == "message":
                for c in it.get("content") or []:
                    if isinstance(c, dict) and c.get("text"):
                        parts.append(c["text"])
        if not executed:
            # 没有 web_search_call 就是没有真搜，视同不支持，顺延供应商链
            log.warning("native search via %s: no web_search_call executed, treat as unsupported", pid)
            return None
        text = "\n\n".join(parts).strip()
        return text[:6000] if text else None
    except Exception as e:
        log.warning("native search via %s failed: %s", pid, e)
        return None


async def _native_search_kimi(pinfo, query):
    """Kimi For Coding 的原生搜索形状：Anthropic /v1/messages + web_search_20250305
    server tool。单请求内完成：server_tool_use -> web_search_tool_result(带来源)
    -> 最终答案文本。（OpenAI 形状的 $web_search builtin 也在，但需两轮握手，弃用。）"""
    up = (pinfo.get("upstream") or "").rstrip("/")
    key = pinfo.get("key")
    model = pinfo.get("active_model") or (pinfo.get("models") or [None])[0]
    if not (up and key and model and query):
        return None
    try:
        r = await client.post(f"{up}/v1/messages", timeout=90, json={
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": f"请联网搜索并简要回答:{query}"}],
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        }, headers={"Authorization": f"Bearer {key}",
                    "anthropic-version": "2023-06-01", "content-type": "application/json"})
        if r.status_code != 200:
            log.warning("kimi native search: HTTP %s", r.status_code)
            return None
        srcs, texts = [], []
        for blk in (r.json().get("content") or []):
            t = blk.get("type")
            if t == "web_search_tool_result":
                for res in blk.get("content") or []:
                    if isinstance(res, dict) and res.get("url") and res["url"] not in srcs:
                        srcs.append(res["url"])
            elif t == "text":
                txt = (blk.get("text") or "").strip()
                if txt and not txt.startswith("Search results for query:"):
                    texts.append(txt)
        if not srcs:
            log.warning("kimi native search: no web_search_tool_result, treat as unsupported")
            return None
        text = "\n\n".join(["来源: " + ", ".join(srcs[:8])] + texts).strip()
        return text[:6000] if text else None
    except Exception as e:
        log.warning("kimi native search failed: %s", e)
        return None


async def _native_search_gemini(pinfo, query):
    """Gemini via Antigravity local proxy (/v1/responses with web_search tool).
    Antigravity wraps Google Search Grounding natively and returns grounding
    citations & search summary text directly in message output."""
    up = (pinfo.get("upstream") or "").rstrip("/")
    key = pinfo.get("key")
    model = pinfo.get("active_model") or (pinfo.get("models") or ["gemini-3.7-flash-high"])[0]
    if not (up and query):
        return None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model,
        "input": [{"role": "user", "content": f"搜索并总结回答：{query}"}],
        "tools": [{"type": "web_search"}],
        "stream": False,
    }
    try:
        r = await client.post(f"{up}/v1/responses", json=payload,
                              headers=headers, timeout=60)
        if r.status_code != 200:
            log.warning("native search via gemini: HTTP %s", r.status_code)
            return None
        data = r.json()
        parts = []
        executed = False
        for it in data.get("output") or []:
            if it.get("type") == "web_search_call":
                executed = True
                act = it.get("action") or {}
                srcs = [s.get("url") for s in (act.get("sources") or []) if isinstance(s, dict) and s.get("url")]
                if srcs:
                    parts.append("来源: " + ", ".join(srcs[:8]))
            elif it.get("type") == "message":
                for c in it.get("content") or []:
                    if isinstance(c, dict) and c.get("text"):
                        txt = c["text"]
                        parts.append(txt)
                        if "已为您搜索" in txt or "来源引文" in txt or "http" in txt:
                            executed = True
        if not executed and not parts:
            log.warning("native search via gemini: no search output produced")
            return None
        text = "\n\n".join(parts).strip()
        return text[:6000] if text else None
    except Exception as e:
        log.warning("native search via gemini failed: %s", e)
        return None


def _extract_call_query(arguments) -> str:
    """从 web_search function_call 的 arguments 里取查询词（query 或 input 键）。"""
    try:
        a = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except Exception:
        return ""
    if not isinstance(a, dict):
        return ""
    return str(a.get("query") or a.get("input") or "")


_DELEGATION_INPUT_RE = re.compile(
    r"<codex_delegation\b.*?<input>(.*?)</input>", re.DOTALL | re.IGNORECASE
)


def _extract_delegation_text(text: str) -> str:
    """Best-effort recovery of the delegated task text from a
    <codex_delegation> wrapper. Delegated threads have no plain user
    message — without this, their only user message starts with '<' and
    _fallback_query would skip it entirely (2026-08-31 incident)."""
    m = _DELEGATION_INPUT_RE.search(text or "")
    return m.group(1).strip() if m else (text or "")


def _fallback_query(items) -> str:
    """空查询兜底：取最近一条有实质内容的用户消息——跳过"继续/好"这类
    推进指令；剥掉 app 的 environment_context 包装，只留 ## My request: 正文。"""
    _TRIVIAL = {"继续", "继续任务", "把任务做完", "好", "好的", "嗯", "嗯嗯", "行", "可以",
                "做吧", "执行", "收了", "ok", "okay", "go", "continue", "？", "?"}
    last_any = ""
    for it in reversed(items or []):
        if not (isinstance(it, dict) and it.get("type") == "message" and it.get("role") == "user"):
            continue
        c = it.get("content")
        if isinstance(c, str):
            cand = c
        elif isinstance(c, list):
            cand = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
        else:
            continue
        cand = cand.strip()
        if "## My request:" in cand:
            cand = cand.split("## My request:", 1)[1].strip()
        if cand.startswith("<codex_delegation"):
            cand = _extract_delegation_text(cand)
        if not cand or cand.startswith("<"):
            continue
        if not last_any:
            last_any = cand
        if len(cand) >= 6 and cand.lower() not in _TRIVIAL:
            return cand[:300]
    return (last_any or "").strip()[:300]


async def _execute_search(query, items, chosen_pid, providers):
    """按供应商链执行一次真实搜索；全链失败返回诚实的孤儿 note。"""
    note = ORPHAN_NOTES["web_search"]
    q = (query or "").strip()
    if not q:
        q = _fallback_query(items)
        if q:
            log.info("  web_search: empty query, fallback to last user msg (%r)", q[:60])
    if not q:
        log.warning("web_search: no usable query (call noted, not executed)")
        stats.note_event("web_search_no_query", "empty query and no fallback")
        return note
    chain = []
    for pid in (chosen_pid, *SEARCH_CAPABLE):
        if pid in SEARCH_CAPABLE and pid in providers and pid not in chain:
            chain.append(pid)
    for pid in chain:
        text = await _native_search(pid, providers[pid], q)
        if text:
            log.info("  web_search executed via provider '%s' (q=%s)", pid, q[:40])
            stats.note_event("web_search", f"{pid}: {q[:60]}")
            return text
    return note


async def fulfill_web_searches(raw: bytes, chosen_pid, providers) -> bytes:
    """Replace orphan web_search notes in the outgoing body with real results."""
    try:
        body = json.loads(raw)
    except Exception:
        return raw
    items = body.get("input")
    if not isinstance(items, list):
        return raw
    note = ORPHAN_NOTES.get("web_search")
    calls = {}
    targets = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("type") == "function_call" and it.get("name") == "web_search":
            calls[it.get("call_id")] = _extract_call_query(it.get("arguments"))
        elif it.get("type") == "function_call_output" and it.get("output") == note and it.get("call_id") in calls:
            targets.append((it.get("call_id"), calls[it["call_id"]], it))
    if not targets:
        return raw
    for cid, query, out_item in targets:
        if cid not in _SEARCH_CACHE:
            # 失败也缓存为 note，避免每轮重试拖慢会话
            _SEARCH_CACHE[cid] = await _execute_search(query, items, chosen_pid, providers)
        if _SEARCH_CACHE[cid] != note:
            out_item["output"] = _SEARCH_CACHE[cid]
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


# --- response path: native item restoration ------------------------------------


def rewrite_output_item(item):
    """function_call -> native special call item, or restore namespace."""
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return item, False
    name = item.get("name")
    if name in special_names:
        raw_args = item.get("arguments")
        try:
            args_obj = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except Exception:
            args_obj = {"input": raw_args}
        if special_names[name] == "custom_tool_call":
            text = args_obj.get("input") if isinstance(args_obj, dict) else None
            if not text:
                text = raw_args if isinstance(raw_args, str) else json.dumps(args_obj, ensure_ascii=False)
            return {
                "type": "custom_tool_call",
                "id": item.get("id"),
                "call_id": item.get("call_id"),
                "name": name,
                "input": text,
            }, True
        native = {
            "type": special_names[name],
            "id": item.get("id"),
            "call_id": item.get("call_id"),
            "status": "completed",
            "execution": "client",
            "arguments": args_obj,
        }
        # web_search: action (Codex client renders query from action.query)
        if native["type"] == "web_search_call" and isinstance(args_obj, dict):
            q = args_obj.get("query") or args_obj.get("input")
            if q:
                native["action"] = {"type": "search", "query": str(q)}
        return native, True
    ns = name_to_ns.get(name)
    if ns and "namespace" not in item:
        item = dict(item)
        item["namespace"] = ns
        return item, True
    return item, False


def rewrite_response_json(data):
    out = data.get("output")
    if isinstance(out, list):
        data["output"] = [rewrite_output_item(it)[0] for it in out]
    return data


class SseRewriter:
    """Per-stream state: buffers special function_call events and re-emits
    them as native call items; restores namespace on dynamic-tool calls."""

    def __init__(self):
        self.pending = {}    # item_id -> True for buffered special function_call
        self.ns_items = {}   # item_id -> namespace injected at output_item.added
        self.seq = 0
        self.seen_seq = False
        self.usage = None
        self.model = None
        self.oi_offset = 0        # 搜索续轮：本轮 output_index 需要的前移量
        self.drop_lifecycle = False  # 搜索续轮：丢弃 response.created/in_progress
        self.pending_search_dones = []  # web_search：延迟到真实搜索完成再发的 done 帧

    def _stamp(self, data):
        if isinstance(data, dict) and ("sequence_number" in data or self.seen_seq):
            data["sequence_number"] = self.seq
            self.seen_seq = True
            self.seq += 1

    @staticmethod
    def _emit(ev, data):
        parts = []
        if ev:
            parts.append(f"event: {ev}")
        parts.append("data: " + (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)))
        return ("\n".join(parts) + "\n\n").encode("utf-8")

    def handle_block(self, lines):
        ev = None
        data_lines = []
        for ln in lines:
            if ln.startswith("event:"):
                ev = ln[6:].strip()
            elif ln.startswith("data:"):
                data_lines.append(ln[5:].lstrip())
        raw = "\n".join(data_lines).strip()
        if not raw or raw == "[DONE]":
            return [("\n".join(lines) + "\n\n").encode("utf-8")]
        try:
            data = json.loads(raw)
        except Exception:
            return [("\n".join(lines) + "\n\n").encode("utf-8")]
        chunks = []
        for e, d in self.process(ev, data):
            if isinstance(d, dict):
                self._stamp(d)
            chunks.append(self._emit(e, d))
        return chunks

    def process(self, ev, data):
        t = data.get("type")
        if self.drop_lifecycle and t in ("response.created", "response.in_progress"):
            return []
        if self.oi_offset and isinstance(data, dict) and isinstance(data.get("output_index"), int):
            data = dict(data)
            data["output_index"] = data["output_index"] + self.oi_offset
        if t == "response.output_item.added":
            item = data.get("item") or {}
            if item.get("type") == "function_call":
                name = item.get("name")
                if name in special_names:
                    self.pending[item.get("id")] = True
                    log.info("  resp: buffer special function_call '%s'", name)
                    return []
                ns = name_to_ns.get(name)
                if ns and "namespace" not in item:
                    item["namespace"] = ns
                    self.ns_items[item.get("id")] = ns
                    data["item"] = item
                    log.info("  resp: inject namespace '%s' for call '%s'", ns, name)
            return [(ev, data)]
        if t in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            if data.get("item_id") in self.pending:
                return []
            return [(ev, data)]
        if t == "response.output_item.done":
            item = data.get("item") or {}
            iid = item.get("id")
            if iid in self.pending or (item.get("type") == "function_call" and item.get("name") in special_names):
                self.pending.pop(iid, None)
                native, _ = rewrite_output_item(item)
                log.info("  resp: emit native '%s' (call_id=%s)", native.get("type"), native.get("call_id"))
                if native.get("type") == "web_search_call":
                    # UX：先发 status=in_progress 的壳（客户端渲染"正在搜索"），
                    # done 帧暂存，待桥真实搜索完成后再发（见 gen() 的 flush）。
                    in_progress = dict(native)
                    in_progress["status"] = "in_progress"
                    added = {"type": "response.output_item.added", "output_index": data.get("output_index"), "item": in_progress}
                    done_item = dict(native)  # status 保持 completed（历史项语义）
                    done = {"type": "response.output_item.done", "output_index": data.get("output_index"), "item": done_item}
                    self.pending_search_dones.append(done)
                    return [("response.output_item.added", added)]
                added = {"type": "response.output_item.added", "output_index": data.get("output_index"), "item": native}
                done = {"type": "response.output_item.done", "output_index": data.get("output_index"), "item": native}
                return [("response.output_item.added", added), ("response.output_item.done", done)]
            if iid in self.ns_items:
                item.setdefault("namespace", self.ns_items[iid])
                data["item"] = item
            return [(ev, data)]
        if t == "response.completed":
            r = data.get("response")
            if isinstance(r, dict):
                self.usage = extract_usage(r)
                self.model = self.model or r.get("model")
                if isinstance(r.get("output"), list):
                    r["output"] = [rewrite_output_item(it)[0] for it in r["output"]]
            return [(ev, data)]
        return [(ev, data)]


# --- proxy --------------------------------------------------------------------


# --- search continuation：服务端语义的 web_search 续轮 -------------------------
# 上游响应若以 web_search 调用结尾，桥自己执行搜索、把"带结果的追问"流式拼进
# 同一条客户端响应——对齐官方 Codex 的服务端工具语义，轮次不再"断"在搜索上。
SEARCH_CONT_MAX = 8  # cap serial search rounds; the final follow-up drops web_search and injects a wrap-up instruction


def _parse_sse_block(lines):
    ev = None
    data_lines = []
    for ln in lines:
        if ln.startswith("event:"):
            ev = ln[6:].strip()
        elif ln.startswith("data:"):
            data_lines.append(ln[5:].lstrip())
    raw = "\n".join(data_lines).strip()
    if not raw or raw == "[DONE]":
        return None
    try:
        return ev, json.loads(raw)
    except Exception:
        return None


def _merge_usage(acc, u):
    if not isinstance(u, dict):
        return
    for k, v in u.items():
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + v
        elif isinstance(v, dict):
            sub = acc.setdefault(k, {})
            for sk, sv in v.items():
                if isinstance(sv, (int, float)):
                    sub[sk] = sub.get(sk, 0) + sv


# 上游不支持的请求体参数黑名单（按上游 host 匹配）。
# 2026-08-29 实测：kimi coding 端点对 Codex 例行发送的 parallel_tool_calls: false
# 直接 400（"parallel_tool_calls: false is not supported"），同参数 zhipu/minimax 均接受。
UNSUPPORTED_BODY_PARAMS = [
    ("api.kimi.com", ("parallel_tool_calls",)),
]


def _strip_unsupported_params(raw: bytes, upstream: str) -> bytes:
    drop = [k for host, keys in UNSUPPORTED_BODY_PARAMS if host in (upstream or "") for k in keys]
    if not drop:
        return raw
    try:
        body = json.loads(raw)
    except Exception:
        return raw
    hit = [k for k in drop if k in body]
    if not hit:
        return raw
    for k in hit:
        body.pop(k, None)
    log.info("  strip unsupported params for %s: %s", upstream, hit)
    stats.note_event("param_strip", f"{upstream}: {','.join(hit)}")
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


app = FastAPI()
app.include_router(dash.router)
dash.bridge = sys.modules[__name__]
client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15.0))


def apply_config(cfg, changed):
    global CFG, UPSTREAM
    CFG = cfg
    if "upstream" in changed:
        UPSTREAM = cfg["upstream"]
    if "log_level" in changed:
        log.setLevel(getattr(logging, str(cfg["log_level"]).upper(), logging.INFO))

HOP_BY_HOP = {
    "host", "content-length", "connection", "transfer-encoding",
    "keep-alive", "te", "trailers", "upgrade", "accept-encoding",
}

load_ns_map()

_last_main_model = None

def record_main_model(m: str):
    global _last_main_model
    if m and not any(k in str(m).lower() for k in ("luna", "terra")):
        _last_main_model = str(m).strip()

def get_current_main_model(cfg=None) -> str:
    global _last_main_model
    # 1) config.toml 的 model 是权威来源（客户端当前默认模型），每次实时读取。
    #    避免其他会话/子代理的直接请求污染"主模型"记忆（历史 bug：任何非 luna
    #    请求都会覆盖 _last_main_model，导致后台 luna 任务被重写到错误模型）。
    try:
        codex_dir = os.path.dirname(_HERE)
        toml_path = os.path.join(codex_dir, "config.toml")
        if os.path.exists(toml_path):
            with open(toml_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("model ") or line.startswith("model="):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            val = parts[1].strip().strip('"').strip("'")
                            if val and not any(k in str(val).lower() for k in ("luna", "terra")):
                                _last_main_model = val
                                return val
    except Exception as e:
        log.warning("Failed to read model from config.toml: %s", e)
    # 2) 兜底（仅当 config.toml 缺失/无 model 时才会走到这里）：最近观察到的主模型
    if _last_main_model:
        return _last_main_model
    # 3) 兜底：当前活跃供应商的默认模型
    if cfg:
        active_pid = cfg.get("active_provider")
        providers = cfg.get("providers", {})
        if active_pid in providers:
            act_m = providers[active_pid].get("active_model")
            if act_m and not any(k in str(act_m).lower() for k in ("luna", "terra")):
                _last_main_model = act_m
                return act_m
    return "GLM-5.3-Flash"


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request):
    t0 = time.time()
    raw = await request.body()
    model = None
    body_dict = None
    if request.method == "POST" and raw:
        try:
            body_dict = json.loads(raw)
            if isinstance(body_dict, dict):
                model = body_dict.get("model")
        except Exception:
            pass

    current_upstream = UPSTREAM
    active_key = None
    matched_pid = None

    try:
        cfg = dash.load_config() if dash else {}
        providers = cfg.get("providers", {})
        active_pid = cfg.get("active_provider")

        # 智能模型别名机制 (Model Aliases):
        # 当检测到 Codex 客户端发来 gpt-5.6-luna 等后台任务模型时，自动重写为当前主对话正在使用的 LLM 模型
        if model and any(k in str(model).lower() for k in ("luna", "terra")):
            target_model = get_current_main_model(cfg)
            log.info("  alias: rewriting background model '%s' -> main model '%s'", model, target_model)
            stats.note_event("model_alias", f"{model} -> {target_model}")
            model = target_model
            if isinstance(body_dict, dict):
                body_dict["model"] = target_model
                raw = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
        elif model and not any(k in str(model).lower() for k in ("luna", "terra")):
            record_main_model(model)
            try:
                _toml_model = get_current_main_model(cfg)
                if _toml_model and str(model) != _toml_model:
                    log.info("  direct: client declared model '%s' (default is '%s')", model, _toml_model)
                    stats.note_event("model_direct", f"{model} (default {_toml_model})")
            except Exception:
                pass

        if request.method == "POST" and raw:
            raw = normalize_body(raw)
            if model and isinstance(body_dict, dict):
                try:
                    nb = json.loads(raw)
                    if nb.get("model") != model:
                        nb["model"] = model
                        raw = json.dumps(nb, ensure_ascii=False).encode("utf-8")
                except Exception:
                    pass

        # 智能模型反向路由 (Model-Aware Smart Routing):
        # 若请求声明了具体 model，优先按模型归属自动匹配供应商
        if model:
            for pid, pinfo in providers.items():
                p_models = pinfo.get("models", [])
                p_act_m = pinfo.get("active_model")
                if model in p_models or model == p_act_m or any(str(model).lower() == str(m).lower() for m in p_models):
                    matched_pid = pid
                    break

        chosen_pid = matched_pid if matched_pid else (active_pid if active_pid in providers else None)

        if chosen_pid and chosen_pid in providers:
            p_info = providers[chosen_pid]
            p_up = p_info.get("upstream")
            if p_up:
                current_upstream = p_up.rstrip("/")
            active_key = p_info.get("key")
            if matched_pid and matched_pid != active_pid:
                log.info("  routing: auto-routed model '%s' to provider '%s' (%s)", model, matched_pid, current_upstream)

        if request.method == "POST" and raw:
            raw = await fulfill_web_searches(raw, chosen_pid, providers)
            raw = _strip_unsupported_params(raw, current_upstream)

        if not active_key:
            auth_path = os.path.join(os.path.dirname(_HERE), "auth.json")
            if os.path.exists(auth_path):
                with open(auth_path, "r", encoding="utf-8") as _af:
                    active_key = json.load(_af).get("OPENAI_API_KEY")
    except Exception as _e:
        log.warning("Failed to resolve dynamic provider/key: %s", _e)

    url = f"{current_upstream}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}

    # 动态注入当前解析出的生效 Key，实现真正的免重启零感知热切
    if active_key:
        for hk in list(headers.keys()):
            if hk.lower() == "authorization":
                del headers[hk]
        headers["authorization"] = f"Bearer {active_key}"

    try:
        req = client.build_request(
            request.method, url, content=raw, headers=headers,
            params=request.query_params,
        )
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        log.error("upstream connect failed: %s", e)
        stats.note_request(ts=t0, method=request.method, path=path, model=model,
                           status=502, latency_ms=(time.time() - t0) * 1000, err=str(e)[:300])
        return JSONResponse(
            {"error": {"message": f"bridge upstream error: {e}", "type": "bad_gateway"}},
            status_code=502,
        )

    log.info("%s /%s -> %s", request.method, path, resp.status_code)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() == "content-type"}

    if "text/event-stream" in resp.headers.get("content-type", ""):
        # 请求体留底：搜索续轮要用它构造 follow-up 请求
        body_obj = None
        if request.method == "POST" and raw:
            try:
                body_obj = json.loads(raw)
            except Exception:
                body_obj = None

        async def gen():
            err = None
            rewriter = SseRewriter()
            cur_resp = resp
            cont_items = []      # 续轮累积（上游格式）：各轮 output 与搜索结果按序交替
            merged_outputs = []  # 续轮累积（客户端格式）：各轮 rewrite 后的 output
            resp_id = None
            completed_data = None
            usage_raw = {}
            stats_usage = {}
            model_seen = None
            last_raw_out = []
            rounds = 0
            stop_reason = None  # max_rounds / followup_connect_failed / followup_http_error
            search_dones = []  # 各轮暂存的 web_search done 帧（gen 级汇总，兜底不丢）

            def _flush_ws_dones(call_id=None, query=None):
                """发出延迟的 web_search done 帧。call_id=None 时全部发出（completed 前兜底）；
                query 非 None 时回填实际执行的查询词（含空 query 的 fallback 词）。"""
                out = []
                for d in list(search_dones):
                    item = d.get("item") or {}
                    if call_id is not None and item.get("call_id") != call_id:
                        continue
                    if query is not None:
                        item["action"] = {"type": "search", "query": query}
                    search_dones.remove(d)
                    rewriter._stamp(d)
                    log.info("  resp: web_search_call done (call_id=%s)", item.get("call_id"))
                    out.append(SseRewriter._emit("response.output_item.done", d))
                return out

            async def _lines(stream):
                async for line in stream.aiter_lines():
                    yield line
                yield ""  # 哨兵：冲刷无空行结尾的尾部块

            try:
                while True:
                    block = []
                    cur_resp.encoding = "utf-8"
                    async for line in _lines(cur_resp):
                        if line != "":
                            block.append(line)
                            continue
                        if not block:
                            continue
                        parsed = _parse_sse_block(block)
                        if parsed and isinstance(parsed[1], dict) and parsed[1].get("type") == "response.completed":
                            # 扣下 completed：可能要续轮，最终统一发合并版
                            r = parsed[1].get("response") or {}
                            resp_id = resp_id or r.get("id")
                            if r.get("model"):
                                model_seen = r["model"]
                            last_raw_out = r.get("output") or []
                            cont_items.extend(last_raw_out)
                            merged_outputs.append([rewrite_output_item(it)[0] for it in last_raw_out])
                            _merge_usage(usage_raw, r.get("usage"))
                            _merge_usage(stats_usage, extract_usage(r))
                            completed_data = parsed[1]
                        else:
                            for c in rewriter.handle_block(block):
                                yield c
                        block = []
                    await cur_resp.aclose()
                    # 本轮流里暂存的 web_search done 帧汇入 gen 级列表（防 rewriter 换轮丢失）
                    search_dones.extend(rewriter.pending_search_dones)
                    rewriter.pending_search_dones = []

                    # —— 搜索续轮判定：本轮 output 里的工具调用全是 web_search 才续 ——
                    pending_ws = [it for it in last_raw_out
                                  if isinstance(it, dict) and it.get("type") == "function_call"]
                    all_ws = bool(pending_ws) and all(it.get("name") == "web_search" for it in pending_ws)
                    if not (all_ws and body_obj and chosen_pid):
                        break
                    if rounds >= SEARCH_CONT_MAX:
                        # Defensive: the final follow-up should have dropped
                        # web_search from tools, so a new search here means the
                        # model forced one anyway. Never continue past the cap.
                        stop_reason = "max_rounds"
                        break
                    rounds += 1
                    for it in pending_ws:
                        # 与 _execute_search 相同的取词逻辑（含空 query 的用户消息兜底），
                        # 便于把实际执行的查询词回填到 done 帧的 action.query。
                        q = (_extract_call_query(it.get("arguments")) or "").strip()
                        if not q:
                            q = _fallback_query((body_obj.get("input") or []) + cont_items)
                        res = await _execute_search(q,
                                                    (body_obj.get("input") or []) + cont_items,
                                                    chosen_pid, providers)
                        cont_items.append({"type": "function_call_output",
                                           "call_id": it.get("call_id"), "output": res})
                        for chunk in _flush_ws_dones(it.get("call_id"), q or ""):
                            yield chunk
                    log.info("  cont: round %d, executed %d web_search call(s)", rounds, len(pending_ws))
                    fbody = dict(body_obj)
                    fbody["input"] = list(body_obj.get("input") or []) + cont_items
                    fbody["stream"] = True
                    if rounds >= SEARCH_CONT_MAX:
                        # Final round: the client never auto-continues after a
                        # pure web_search ending, so instead of silently
                        # dropping further searches (fake "completed"), remove
                        # the tool and instruct the model to wrap up with the
                        # evidence already collected.
                        fbody["tools"] = [t for t in (fbody.get("tools") or [])
                                          if not (isinstance(t, dict) and t.get("name") == "web_search")]
                        fbody["input"].append({
                            "type": "message", "role": "user",
                            "content": [{"type": "input_text",
                                         "text": f"[bridge] Search round budget ({SEARCH_CONT_MAX}) is exhausted. "
                                                 f"Write your final answer using the search results already collected above. "
                                                 f"Do not attempt further web searches."}],
                        })
                        log.info("  cont: final round %d — web_search removed from tools, wrap-up instruction injected", rounds)
                    try:
                        freq = client.build_request("POST", url, json=fbody, headers=headers)
                        new_resp = await client.send(freq, stream=True)
                    except Exception as e:
                        log.warning("  cont follow-up connect failed: %s", e)
                        stop_reason = "followup_connect_failed"
                        break
                    if new_resp.status_code != 200 or "text/event-stream" not in new_resp.headers.get("content-type", ""):
                        log.warning("  cont follow-up HTTP %s", new_resp.status_code)
                        await new_resp.aclose()
                        stop_reason = "followup_http_error"
                        break
                    prev = rewriter
                    rewriter = SseRewriter()
                    rewriter.seq = prev.seq
                    rewriter.seen_seq = prev.seen_seq
                    rewriter.oi_offset = sum(len(o) for o in merged_outputs)
                    rewriter.drop_lifecycle = True
                    cur_resp = new_resp

                # 兜底：所有退出路径（混合工具不续轮/SEARCH_CONT_MAX 用尽/follow-up 失败）
                # 在合并 completed 前清空暂存的 done 帧，保证条目不会永远挂在 in_progress。
                if search_dones and stop_reason in ("max_rounds", "followup_connect_failed", "followup_http_error"):
                    log.warning("  flushing %d web_search done(s) WITHOUT execution (reason=%s)",
                                len(search_dones), stop_reason)
                for chunk in _flush_ws_dones():
                    yield chunk
                # 流结束：发出合并后的 response.completed（含各轮 output 与用量）
                if completed_data is not None:
                    r = completed_data.get("response") or {}
                    if resp_id:
                        r["id"] = resp_id
                    r["output"] = [it for ro in merged_outputs for it in ro]
                    if usage_raw:
                        r["usage"] = usage_raw
                    rewriter._stamp(completed_data)
                    yield SseRewriter._emit("response.completed", completed_data)
            except Exception as e:
                err = str(e)[:300]
            finally:
                await cur_resp.aclose()
                stats.note_request(ts=t0, method=request.method, path=path,
                                   model=model_seen or model, status=resp.status_code,
                                   latency_ms=(time.time() - t0) * 1000, err=err,
                                   **stats_usage)

        return StreamingResponse(gen(), status_code=resp.status_code, headers=out_headers)

    content = await resp.aread()
    await resp.aclose()
    usage = {}
    if resp.status_code >= 400:
        log.warning("upstream %s: %s", resp.status_code, content[:800])
    if "json" in resp.headers.get("content-type", ""):
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                usage = extract_usage(data)
                if isinstance(data.get("output"), list):
                    content = json.dumps(rewrite_response_json(data), ensure_ascii=False).encode("utf-8")
        except Exception:
            pass
    stats.note_request(ts=t0, method=request.method, path=path, model=model,
                       status=resp.status_code, latency_ms=(time.time() - t0) * 1000,
                       err=(f"upstream {resp.status_code}" if resp.status_code >= 400 else None),
                       **usage)
    return Response(content=content, status_code=resp.status_code, headers=out_headers)


if __name__ == "__main__":
    import sys

    # pythonw has no console: sys.stdout/stderr are None and uvicorn's
    # stream logging would crash the process. Sink them, and keep
    # uvicorn from touching logging config (our file log is enough).
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
    sys.stderr = open(os.devnull, "w", encoding="utf-8")
    log.info("Codex Bridge starting on %s:%d -> %s", HOST, PORT, UPSTREAM)
    try:
        import tray_icon
        tray_icon.start(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "assets", "bridge.ico"),
            f"Codex Bridge :{PORT}",
            f"http://{HOST}:{PORT}/dashboard")
        tray_ok = True
    except Exception as _e:
        tray_ok = False
        log.warning("tray icon not started: %s", _e)

    def _cn_num(n):
        n = int(n or 0)
        if n >= 100000000:
            return ("%.2f" % (n / 100000000)).rstrip("0").rstrip(".") + "亿"
        if n >= 10000:
            return "%.0f万" % (n / 10000)
        return str(n)

    def _refresh_tray_usage():
        # Shared by the periodic tooltip worker and the hover-popover's
        # "refresh immediately when shown" path.
        rows = stats.models(1) or []
        summary = stats.summary(1) or {}
        tot = sum(int(r.get("tokens_total") or 0) for r in rows)
        req = sum(int(r.get("requests") or 0) for r in rows)
        hit_rate = summary.get("cache_hit_rate")
        # The rich usage display is now the animated hover card.  Keep the
        # OS tooltip short so Windows' native tooltip does not compete with it.
        tray_icon.update_tooltip("Codex Bridge :{}".format(PORT))
        tray_icon.set_usage(rows, tot, req, "Codex Bridge", hit_rate=hit_rate)

    def _tray_tooltip_worker():
        # Hover tooltip shows today's usage; refresh every 60s so the icon
        # never shows a stale line.
        while True:
            try:
                _refresh_tray_usage()
            except Exception as _e:
                log.debug("tray tooltip refresh failed: %s", _e)
            time.sleep(60)

    if tray_ok:
        try:
            tray_icon.set_refresh_callback(_refresh_tray_usage)
        except Exception as _e:
            log.warning("tray hover callback unavailable: %s", _e)
        threading.Thread(target=_tray_tooltip_worker, daemon=True,
                         name="tray-tooltip").start()
    uvicorn.run(app, host=HOST, port=PORT, log_config=None)
