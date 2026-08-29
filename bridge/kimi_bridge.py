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

import sys
import time

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
                    out.append(st)
                    learn_ns(sub["name"], ns)
                    n += 1
                else:
                    log.info("  skip subtool in namespace '%s': %r", ns, sub if not isinstance(sub, dict) else sub.get("type"))
            log.info("  flatten namespace '%s': %d function tools exposed", ns, n)
            continue
        out.append(normalize_tool(t))
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
SEARCH_CAPABLE = ("zhipu", "minimax")   # probed 2026-08-28: kimi 400/ignored, gemini proxy dead
_SEARCH_CACHE = {}                       # call_id -> final output text (results or note)


async def _native_search(pid, pinfo, query):
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
    try:
        r = await client.post(f"{up}/v1/responses", json=payload,
                              headers={"Authorization": f"Bearer {key}"}, timeout=60)
        if r.status_code != 200:
            log.warning("native search via %s: HTTP %s", pid, r.status_code)
            return None
        data = r.json()
        parts = []
        for it in data.get("output") or []:
            if it.get("type") == "web_search_call":
                act = it.get("action") or {}
                srcs = [s.get("url") for s in (act.get("sources") or []) if isinstance(s, dict) and s.get("url")]
                if srcs:
                    parts.append("来源: " + ", ".join(srcs[:8]))
            elif it.get("type") == "message":
                for c in it.get("content") or []:
                    if isinstance(c, dict) and c.get("text"):
                        parts.append(c["text"])
        text = "\n\n".join(parts).strip()
        return text[:6000] if text else None
    except Exception as e:
        log.warning("native search via %s failed: %s", pid, e)
        return None


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
            try:
                _a = json.loads(it.get("arguments") or "{}")
                q = _a.get("query") or _a.get("input") or ""
            except Exception:
                q = None
            calls[it.get("call_id")] = str(q or "")
        elif it.get("type") == "function_call_output" and it.get("output") == note and it.get("call_id") in calls:
            targets.append((it.get("call_id"), calls[it["call_id"]], it))
    if not targets:
        return raw
    # 客户端原生 web_search 无参数 schema，模型常发出空 arguments；
    # 兜底用最近一条用户消息作为查询词
    fallback_q = ""
    for it in reversed(items):
        if isinstance(it, dict) and it.get("type") == "message" and it.get("role") == "user":
            c = it.get("content")
            if isinstance(c, str):
                fallback_q = c
            elif isinstance(c, list):
                fallback_q = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            if fallback_q.strip():
                break
    fallback_q = fallback_q.strip()[:300]
    chain = []
    for pid in (chosen_pid, *SEARCH_CAPABLE):
        if pid in SEARCH_CAPABLE and pid in providers and pid not in chain:
            chain.append(pid)
    for cid, query, out_item in targets:
        if cid not in _SEARCH_CACHE:
            text = None
            if not query:
                query = fallback_q
                log.info("  web_search orphan %s: empty query, fallback to last user msg (%r)", cid, query[:60])
            for pid in chain if query else []:
                text = await _native_search(pid, providers[pid], query)
                if text:
                    log.info("  web_search executed via provider '%s' (call_id=%s, q=%s)", pid, cid, query[:40])
                    stats.note_event("web_search", f"{pid}: {query[:60]}")
                    break
            _SEARCH_CACHE[cid] = text or note   # 失败也缓存，避免每轮重试拖慢会话
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
        return {
            "type": special_names[name],
            "id": item.get("id"),
            "call_id": item.get("call_id"),
            "status": "completed",
            "execution": "client",
            "arguments": args_obj,
        }, True
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
        rewriter = SseRewriter()

        async def gen():
            block = []
            err = None
            try:
                resp.encoding = "utf-8"
                async for line in resp.aiter_lines():
                    if line == "":
                        if block:
                            for c in rewriter.handle_block(block):
                                yield c
                            block = []
                    else:
                        block.append(line)
                if block:
                    for c in rewriter.handle_block(block):
                        yield c
            except Exception as e:
                err = str(e)[:300]
            finally:
                await resp.aclose()
                stats.note_request(ts=t0, method=request.method, path=path,
                                   model=rewriter.model or model, status=resp.status_code,
                                   latency_ms=(time.time() - t0) * 1000, err=err,
                                   **(rewriter.usage or {}))

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
    log.info("kimi-bridge starting on %s:%d -> %s", HOST, PORT, UPSTREAM)
    uvicorn.run(app, host=HOST, port=PORT, log_config=None)
