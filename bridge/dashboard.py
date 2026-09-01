"""dashboard.py — admin API + dashboard page routes for kimi_bridge.

Mounted on the same FastAPI app. The page itself is a single static HTML
file (dashboard.html) with zero external dependencies.
"""

import datetime
import copy
import json
import os
import re
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from bridge_stats import stats

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_HERE, "bridge_config.json")
NS_MAP_FILE = os.path.join(_HERE, "namespace_map.json")
LOG_FILE = os.path.join(_HERE, "kimi_bridge.log")
HTML_FILE = os.path.join(_HERE, "dashboard.html")

DEFAULT_CONFIG = {
    "upstream": "https://api.kimi.com/coding",
    "host": "127.0.0.1",
    "port": 15731,
    "log_level": "INFO",
    "bridge_tool_search": True,
    "bridge_web_search": True,
    "learn_namespaces": True,
    "stats_enabled": True,
    "stats_retention_days": 30,
}

HOT_KEYS = {"upstream", "log_level", "bridge_tool_search", "bridge_web_search",
            "learn_namespaces", "stats_enabled", "stats_retention_days"}
RESTART_KEYS = {"host", "port"}

router = APIRouter()
bridge = None  # set by kimi_bridge at startup (module reference)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        save_config(cfg, backup=False)
    except Exception:
        pass
    return cfg


def save_config(cfg, backup=True):
    if backup and os.path.exists(CONFIG_FILE):
        ts = time.strftime("%Y%m%d-%H%M%S")
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                old = f.read()
            with open(CONFIG_FILE + f".bak-{ts}", "w", encoding="utf-8") as f:
                f.write(old)
        except OSError:
            pass
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def validate_config(body):
    if not isinstance(body, dict):
        return None, "config must be a JSON object"
    cfg = load_config()
    changed, restart_required = [], []
    for k, v in body.items():
        if k not in DEFAULT_CONFIG:
            return None, f"unknown key: {k}"
        if isinstance(DEFAULT_CONFIG[k], bool) and not isinstance(v, bool):
            return None, f"{k} must be boolean"
        if isinstance(DEFAULT_CONFIG[k], int) and not isinstance(v, int):
            return None, f"{k} must be integer"
        if isinstance(DEFAULT_CONFIG[k], str) and not isinstance(v, str):
            return None, f"{k} must be string"
        if cfg.get(k) != v:
            cfg[k] = v
            changed.append(k)
            if k in RESTART_KEYS:
                restart_required.append(k)
    if "port" in cfg and not (1 <= cfg["port"] <= 65535):
        return None, "port out of range"
    if cfg.get("stats_retention_days", 1) < 1:
        return None, "stats_retention_days must be >= 1"
    return (cfg, changed, restart_required), None


ASSETS_DIR = os.path.join(_HERE, "assets")


@router.get("/dashboard/assets/{name}")
def asset(name: str):
    p = os.path.join(ASSETS_DIR, os.path.basename(name))
    if os.path.isfile(p):
        return FileResponse(p)
    return JSONResponse({"error": "not found"}, status_code=404)



@router.get("/favicon.ico")
def favicon():
    p = os.path.join(ASSETS_DIR, "bridge.ico")
    if os.path.isfile(p):
        return FileResponse(p, media_type="image/x-icon")
    return JSONResponse({"error": "not found"}, status_code=404)


@router.get("/dashboard", response_class=HTMLResponse)
def page():
    try:
        with open(HTML_FILE, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html missing</h1>", status_code=500)


@router.get("/dashboard/api/summary")
def api_summary(days: float = 7, date: str = ""):
    return stats.summary(max(0.04, min(days, 120)), date or None)


@router.get("/dashboard/api/daily")
def api_daily(days: int = 30, date: str = ""):
    return stats.daily(max(1, min(days, 120)), date or None)


@router.get("/dashboard/api/models")
def api_models(days: int = 30, date: str = ""):
    return stats.models(max(1, min(days, 120)), date or None)


@router.get("/dashboard/api/requests")
def api_requests(limit: int = 50):
    return stats.recent_requests(max(1, min(limit, 200)))


@router.get("/dashboard/api/events")
def api_events(limit: int = 50):
    return stats.recent_events(max(1, min(limit, 200)))


@router.get("/dashboard/api/events/counts")
def api_event_counts():
    try:
        con = stats._ro()
        today_start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        row_today = con.execute("SELECT COUNT(*) as n FROM events WHERE ts >= ?", (today_start,)).fetchone()
        row_conv = con.execute("SELECT COUNT(*) as n FROM events WHERE kind = 'tool_convert'").fetchone()
        row_inj = con.execute("SELECT COUNT(*) as n FROM events WHERE kind = 'deferred_inject'").fetchone()
        row_learn = con.execute("SELECT COUNT(*) as n FROM events WHERE kind = 'namespace_learn'").fetchone()
        row_last_learn = con.execute("SELECT ts FROM events WHERE kind = 'namespace_learn' ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        last_learn_time = ""
        if row_last_learn and row_last_learn["ts"]:
            last_learn_time = datetime.datetime.fromtimestamp(row_last_learn["ts"]).strftime("%H:%M:%S")
        return {
            "events_today": row_today["n"] if row_today else 0,
            "total_convert": row_conv["n"] if row_conv else 0,
            "total_inject": row_inj["n"] if row_inj else 0,
            "total_learn": row_learn["n"] if row_learn else 0,
            "last_learn_time": last_learn_time,
        }
    except Exception as e:
        return {"events_today": 0, "total_convert": 0, "total_inject": 0, "total_learn": 0, "last_learn_time": ""}


@router.get("/dashboard/api/health")
async def api_health():
    t0 = time.time()
    upstream_ok, upstream_ms = False, None
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            cfg = load_config()
            r = await c.get(cfg["upstream"] + "/v1/models")
            upstream_ok = r.status_code < 500
            upstream_ms = round((time.time() - t0) * 1000, 1)
    except Exception:
        upstream_ms = round((time.time() - t0) * 1000, 1)
    ns_count = 0
    try:
        with open(NS_MAP_FILE, encoding="utf-8") as f:
            ns_count = len(json.load(f))
    except Exception:
        pass
    return {
        "uptime_sec": round(time.time() - stats.started_at),
        "upstream_ok": upstream_ok,
        "upstream_ms": upstream_ms,
        "ns_map_count": ns_count,
        "log_size": os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0,
        "db_size": stats.db_size(),
        "stats_enabled": stats.enabled,
    }


@router.get("/dashboard/api/config")
def api_get_config():
    return load_config()


@router.put("/dashboard/api/config")
async def api_put_config(request: Request):
    body = await request.json()
    result, err = validate_config(body)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    cfg, changed, restart_required = result
    save_config(cfg, backup=True)
    if bridge is not None:
        bridge.apply_config(cfg, [k for k in changed if k in HOT_KEYS])
    if True:
        stats.configure(enabled=cfg["stats_enabled"])
    stats.retention_days = cfg["stats_retention_days"]
    return {"ok": True, "changed": changed, "restart_required": restart_required}


@router.get("/dashboard/api/namespaces")
def api_namespaces():
    try:
        with open(NS_MAP_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


@router.delete("/dashboard/api/namespaces/{name}")
def api_delete_namespace(name: str):
    try:
        with open(NS_MAP_FILE, encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        m = {}
    if name in m:
        del m[name]
        with open(NS_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=1)
        if bridge is not None:
            bridge.name_to_ns.pop(name, None)
        return {"ok": True, "deleted": name}
    return JSONResponse({"ok": False, "error": "not found"}, status_code=404)


@router.get("/dashboard/api/log")
def api_log_tail(lines: int = 200):
    lines = max(1, min(lines, 2000))
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            content = f.readlines()
        return {"lines": [l.rstrip("\n") for l in content[-lines:]]}
    except FileNotFoundError:
        return {"lines": []}


# --- Provider, Model & Key Rotation Endpoints ---

@router.get("/dashboard/api/providers")
def api_get_providers():
    cfg = load_config()
    codex_dir = os.path.expanduser("~/.codex")
    auth_file = os.path.join(codex_dir, "auth.json")
    config_file = os.path.join(codex_dir, "config.toml")
    current_key = ""
    current_model = ""

    if os.path.exists(auth_file):
        try:
            with open(auth_file, "r", encoding="utf-8") as f:
                current_key = json.load(f).get("OPENAI_API_KEY", "")
        except Exception:
            pass

    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("model") and "=" in line:
                        parts = line.split("=", 1)
                        if parts[0].strip() == "model":
                            current_model = parts[1].strip().strip('"').strip("'")
                            break
        except Exception:
            pass

    providers = cfg.get("providers", {})
    return {
        "active_provider": cfg.get("active_provider", "kimi"),
        "providers": providers,
        "current_key": current_key,
        "current_model": current_model or cfg.get("active_model", "k3"),
        "upstream": cfg.get("upstream", "")
    }


@router.post("/dashboard/api/switch-provider")
async def api_switch_provider(request: Request):
    data = await request.json()
    p_id = data.get("provider_id")
    target_model = data.get("model")
    target_key = data.get("key")

    cfg = load_config()
    providers = cfg.get("providers", {})
    if p_id not in providers:
        return JSONResponse({"ok": False, "error": f"未知供应商: {p_id}"}, status_code=400)

    p = providers[p_id]
    if target_key:
        p["key"] = target_key
    if target_model:
        p["active_model"] = target_model

    cfg["active_provider"] = p_id
    cfg["upstream"] = p["upstream"]

    # 1. Update bridge_config.json
    save_config(cfg, backup=False)

    # 2. Update auth.json
    codex_dir = os.path.expanduser("~/.codex")
    auth_file = os.path.join(codex_dir, "auth.json")
    active_key = p.get("key", "")
    if active_key:
        tmp_auth = auth_file + ".tmp"
        with open(tmp_auth, "w", encoding="utf-8") as f:
            json.dump({"OPENAI_API_KEY": active_key}, f, indent=2)
        os.replace(tmp_auth, auth_file)

    # 3. Update config.toml
    config_file = os.path.join(codex_dir, "config.toml")
    active_model = p.get("active_model") or (p.get("models", ["k3"])[0])
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as f:
            cfg_lines = f.readlines()
        new_lines = []
        replaced = False
        for line in cfg_lines:
            if line.strip().startswith("model") and "=" in line and line.strip().split("=")[0].strip() == "model":
                new_lines.append(f'model = "{active_model}"\n')
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.insert(0, f'model = "{active_model}"\n')
        tmp_cfg = config_file + ".tmp"
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        os.replace(tmp_cfg, config_file)

    # 4. Notify bridge upstream in memory if assigned
    if bridge and hasattr(bridge, "UPSTREAM"):
        bridge.UPSTREAM = cfg["upstream"]

    return {"ok": True, "provider": p_id, "model": active_model, "upstream": cfg["upstream"]}


@router.post("/dashboard/api/test-provider")
async def api_test_provider(request: Request):
    data = await request.json()
    upstream = data.get("upstream", "").rstrip("/")
    key = data.get("key", "")
    model = data.get("model", "")
    t0 = time.time()

    req_body = json.dumps({
        "model": model,
        "input": [{"role": "user", "content": "ping"}]
    }).encode("utf-8")

    test_url = f"{upstream}/v1/responses"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(test_url, content=req_body, headers=headers)
            lat = int((time.time() - t0) * 1000)
            if resp.status_code < 300:
                return {"ok": True, "status": resp.status_code, "latency_ms": lat, "message": "连通正常 (200 OK)"}
            else:
                body_prev = resp.text[:120] if hasattr(resp, "text") else ""
                return {"ok": False, "status": resp.status_code, "latency_ms": lat, "message": f"上游响应 {resp.status_code}: {body_prev}"}
    except Exception as e:
        lat = int((time.time() - t0) * 1000)
        return {"ok": False, "status": 0, "latency_ms": lat, "message": f"探测超时或失败: {str(e)[:80]}"}


def ensure_models_in_catalog(models: list):
    """确保自定义模型在 ~/.codex/models.json 中注册，便于 Codex 客户端识别与展示"""
    codex_dir = os.path.expanduser("~/.codex")
    models_json = os.path.join(codex_dir, "models.json")
    if not os.path.exists(models_json):
        return
    try:
        with open(models_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        existing = {m.get("slug"): m for m in data.get("models", [])}
        template = existing.get("k3") or existing.get("gemini-3.7-flash-high") or (data.get("models")[0] if data.get("models") else None)
        if not template:
            return

        std_levels = [
            {"description": "Fast responses with lighter reasoning", "effort": "low"},
            {"description": "Balances speed and reasoning depth for everyday tasks", "effort": "medium"},
            {"description": "Greater reasoning depth for complex problems", "effort": "high"},
            {"description": "Extra high reasoning depth for complex problems", "effort": "xhigh"}
        ]

        updated = False
        for m in models:
            m_str = str(m).strip()
            if m_str and m_str not in existing:
                new_m = copy.deepcopy(template)
                new_m["slug"] = m_str
                new_m["display_name"] = m_str
                new_m["description"] = f"Custom Model: {m_str}"
                new_m["supported_reasoning_levels"] = copy.deepcopy(std_levels)
                new_m["default_reasoning_level"] = "medium"
                new_m["support_verbosity"] = True
                old_name = template.get("slug", "k3")
                if isinstance(new_m.get("base_instructions"), str):
                    new_m["base_instructions"] = new_m["base_instructions"].replace(old_name, m_str).replace("GPT-5", m_str)
                mm = new_m.get("model_messages")
                if isinstance(mm, dict) and isinstance(mm.get("instructions_template"), str):
                    mm["instructions_template"] = mm["instructions_template"].replace(old_name, m_str).replace("GPT-5", m_str)
                data["models"].append(new_m)
                existing[m_str] = new_m
                updated = True
        if updated:
            tmp_file = models_json + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, models_json)
    except Exception as e:
        log.warning(f"Failed to update models.json: {e}")


@router.post("/dashboard/api/save-provider")
async def api_save_provider(request: Request):
    data = await request.json()
    p_id = data.get("id", "").strip().lower()
    name = data.get("name", "").strip()
    upstream = data.get("upstream", "").strip().rstrip("/")
    raw_models = data.get("models", [])
    key = data.get("key", "").strip()

    if not p_id or not re.match(r"^[a-zA-Z0-9_\-]+$", p_id):
        return JSONResponse({"ok": False, "error": "供应商 ID 必须由英文字母、数字、下划线或连字符组成"}, status_code=400)
    if not name:
        return JSONResponse({"ok": False, "error": "请输入供应商展示名称"}, status_code=400)
    if not upstream or not upstream.startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "上游 Base URL 必须以 http:// 或 https:// 开头"}, status_code=400)

    if isinstance(raw_models, str):
        models = [m.strip() for m in re.split(r"[,;\n\r]+", raw_models) if m.strip()]
    elif isinstance(raw_models, list):
        models = [str(m).strip() for m in raw_models if str(m).strip()]
    else:
        models = []

    if not models:
        models = [p_id]

    cfg = load_config()
    providers = cfg.get("providers", {})
    existing = providers.get(p_id, {})

    active_model = data.get("active_model") or existing.get("active_model", models[0])
    if active_model not in models:
        active_model = models[0]

    providers[p_id] = {
        "id": p_id,
        "name": name,
        "upstream": upstream,
        "models": models,
        "active_model": active_model,
        "key": key if key else existing.get("key", ""),
        "custom": True
    }
    cfg["providers"] = providers
    save_config(cfg, backup=False)

    # 自动将新增模型同步写入 models.json
    ensure_models_in_catalog(models)

    return {"ok": True, "provider": providers[p_id]}


@router.post("/dashboard/api/delete-provider")
async def api_delete_provider(request: Request):
    data = await request.json()
    p_id = data.get("id", "").strip()
    cfg = load_config()
    if p_id == cfg.get("active_provider"):
        return JSONResponse({"ok": False, "error": f"无法删除当前正在生效的供应商 [{p_id}]，请先切换到其他供应商"}, status_code=400)

    providers = cfg.get("providers", {})
    if p_id not in providers:
        return JSONResponse({"ok": False, "error": f"供应商不存在: {p_id}"}, status_code=404)

    del providers[p_id]
    cfg["providers"] = providers
    save_config(cfg, backup=False)
    return {"ok": True, "deleted": p_id}


# --- Coding Plan balance ----------------------------------------------------
# Queries each provider's official subscription-quota endpoint and normalizes
# the result into {windows: [{label, used, total, remaining, used_pct, reset_ms}]}.
# Keys never leave the server; failures degrade to per-provider error cards.

_BALANCE_CACHE = {"ts": 0.0, "data": None}
_BALANCE_TTL = 60.0
_KIMI_LEVELS = {
    "LEVEL_ADVANCED": "Advanced",
    "LEVEL_PRO": "Pro",
    "LEVEL_MAX": "Max",
    "LEVEL_STANDARD": "Standard",
    "LEVEL_LITE": "Lite",
}


def _to_num(v):
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except (TypeError, ValueError):
        return None


def _pct(used, total):
    try:
        u, t = float(used), float(total)
        return round(u / t * 100, 1) if t > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _iso_to_ms(v):
    try:
        return int(datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                   .timestamp() * 1000)
    except Exception:
        return None


async def _fetch_kimi_balance(client, key):
    r = await client.get("https://api.kimi.com/coding/v1/usages",
                         headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    d = r.json()
    windows = []
    top = d.get("usage") or {}
    windows.append({
        "label": "周配额",
        "used": _to_num(top.get("used")),
        "total": _to_num(top.get("limit")),
        "remaining": _to_num(top.get("remaining")),
        "reset_ms": _iso_to_ms(top.get("resetTime")),
    })
    for lim in d.get("limits") or []:
        det = lim.get("detail") or {}
        win = lim.get("window") or {}
        hours = None
        if win.get("timeUnit") == "TIME_UNIT_MINUTE":
            hours = (win.get("duration") or 0) / 60
        windows.append({
            "label": f"{int(hours)} 小时窗口" if hours else "窗口配额",
            "used": _to_num(det.get("used")),
            "total": _to_num(det.get("limit")),
            "remaining": _to_num(det.get("remaining")),
            "reset_ms": _iso_to_ms(det.get("resetTime")),
        })
    for w in windows:
        w["used_pct"] = _pct(w["used"], w["total"])
    lvl = ((d.get("user") or {}).get("membership") or {}).get("level") or ""
    return {"level": _KIMI_LEVELS.get(lvl, lvl or None), "windows": windows}


async def _fetch_zhipu_balance(client, key):
    r = await client.get("https://open.bigmodel.cn/api/monitor/usage/quota/limit",
                         headers={"Authorization": key})
    r.raise_for_status()
    d = r.json()
    data = d.get("data") or {}
    windows = []
    for lim in data.get("limits") or []:
        if lim.get("type") != "CREDIT_LIMIT":
            continue
        unit, number = lim.get("unit"), lim.get("number")
        if unit == 3 and number == 5:
            label = "5 小时额度"
        elif unit == 6 and number == 1:
            label = "周额度"
        else:
            label = f"额度 {number}/{unit}"
        total = _to_num(lim.get("usage"))
        used = _to_num(lim.get("currentValue"))
        pct = lim.get("percentage")
        windows.append({
            "label": label,
            "used": used,
            "total": total,
            "remaining": _to_num(lim.get("remaining")),
            "used_pct": _to_num(pct) if pct is not None else _pct(used, total),
            "reset_ms": lim.get("nextResetTime"),
        })
    return {"level": data.get("level") or None, "windows": windows}


async def _fetch_minimax_balance(client, key):
    r = await client.get(
        "https://www.minimaxi.com/v1/api/openplatform/coding_plan/remains",
        headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    d = r.json()
    if (d.get("base_resp") or {}).get("status_code") not in (0, None):
        raise RuntimeError((d.get("base_resp") or {}).get("status_msg") or "minimax api error")
    gen = next((m for m in d.get("model_remains") or []
                if m.get("model_name") == "general"), None)
    if not gen:
        raise RuntimeError("no general-plan entry")
    now_ms = int(time.time() * 1000)
    windows = []
    # NOTE: per MiniMax issue #99, *_usage_count actually means REMAINING.
    i_total = _to_num(gen.get("current_interval_total_count"))
    i_rem = _to_num(gen.get("current_interval_usage_count"))
    w_total = _to_num(gen.get("current_weekly_total_count"))
    w_rem = _to_num(gen.get("current_weekly_usage_count"))
    windows.append({
        "label": "5 小时窗口",
        "used": (i_total - i_rem) if i_total and i_rem is not None else None,
        "total": i_total or None,
        "remaining": i_rem or None,
        "used_pct": _to_num(100 - (gen.get("current_interval_remaining_percent") or 0)),
        "reset_ms": now_ms + int(gen.get("remains_time") or 0),
    })
    windows.append({
        "label": "周配额",
        "used": (w_total - w_rem) if w_total and w_rem is not None else None,
        "total": w_total or None,
        "remaining": w_rem or None,
        "used_pct": _to_num(100 - (gen.get("current_weekly_remaining_percent") or 0)),
        "reset_ms": now_ms + int(gen.get("weekly_remains_time") or 0),
    })
    return {"level": None, "windows": windows}
async def _fetch_antigravity_gemini_balance(client: httpx.AsyncClient):
    gui_config_path = os.path.expanduser("~/.antigravity_tools/gui_config.json")
    if not os.path.exists(gui_config_path):
        return []
    try:
        with open(gui_config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        proxy_port = cfg.get("proxy", {}).get("port", 8044)
        api_key = cfg.get("proxy", {}).get("api_key", "")
    except Exception:
        return []

    try:
        r = await client.get(
            f"http://127.0.0.1:{proxy_port}/api/accounts",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return [{"id": "gemini_local", "vendor": "gemini", "name": "Gemini (Antigravity)", "ok": False, "error": f"连接代理失败: {e}"}]

    accounts = data.get("accounts", [])
    curr_id = data.get("current_account_id")
    res = []
    for acc in accounts:
        acc_id = acc.get("id")
        email = acc.get("email", "unknown")
        is_curr = bool(acc.get("is_current") or (curr_id and acc_id == curr_id))
        prefix = email.split("@")[0]
        name = f"Gemini ({prefix})"
        tier = (acc.get("quota") or {}).get("subscription_tier") or "Google AI Pro"
        level = f"{tier} · 当前" if is_curr else tier

        windows = []
        quota_groups = (acc.get("quota") or {}).get("quota_groups") or []
        gemini_group = next((g for g in quota_groups if "gemini" in (g.get("display_name") or "").lower()), None)
        if gemini_group:
            for b in gemini_group.get("buckets") or []:
                win = b.get("window", "")
                frac = b.get("remaining_fraction")
                rem_pct = round(frac * 100, 1) if frac is not None else None
                used_pct = round((1 - frac) * 100, 1) if frac is not None else None
                label = "5 小时窗口" if "5h" in win.lower() else ("周配额" if "week" in win.lower() else win)
                reset_ms = _iso_to_ms(b.get("reset_time"))
                windows.append({
                    "label": label,
                    "used": None,
                    "total": None,
                    "remaining": rem_pct,
                    "used_pct": used_pct,
                    "reset_ms": reset_ms,
                })
        windows.sort(key=lambda w: 0 if "5" in w["label"] else 1)
        res.append({"id": f"gemini_{prefix}", "vendor": "gemini", "name": name, "email": email, "is_current": is_curr, "level": level, "ok": True, "windows": windows})
    res.sort(key=lambda a: 0 if a.get("is_current") else 1)
    return res




async def _fetch_one(client, pid, name, key):
    try:
        if pid == "kimi":
            data = await _fetch_kimi_balance(client, key)
        elif pid == "zhipu":
            data = await _fetch_zhipu_balance(client, key)
        elif pid == "minimax":
            data = await _fetch_minimax_balance(client, key)
        else:
            return {"id": pid, "name": name, "ok": False, "error": "unsupported"}
        return {"id": pid, "name": name, "ok": True, **data}
    except Exception as e:
        return {"id": pid, "name": name, "ok": False, "error": str(e)[:200]}


@router.get("/dashboard/api/balance")
async def api_balance(refresh: bool = False):
    import asyncio
    now = time.time()
    if (not refresh and _BALANCE_CACHE["data"] is not None
            and now - _BALANCE_CACHE["ts"] < _BALANCE_TTL):
        return {**_BALANCE_CACHE["data"], "cached": True}
    cfg = load_config()
    providers = cfg.get("providers", {})
    wanted = [("kimi", "Kimi For Coding"), ("zhipu", "智谱 BigModel"), ("minimax", "MiniMax")]
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        fetches = [
            _fetch_one(client, pid, providers.get(pid, {}).get("name", name),
                       (providers.get(pid) or {}).get("key", ""))
            for pid, name in wanted
        ]
        fetches.append(_fetch_antigravity_gemini_balance(client))
        raw_results = await asyncio.gather(*fetches)

    standard_results = list(raw_results[:-1])
    gemini_accounts = raw_results[-1] or []
    # Gemini 账号卡片放置在最前/并入
    all_providers = list(gemini_accounts) + standard_results
    data = {"fetched_at": int(now * 1000), "providers": all_providers}
    _BALANCE_CACHE.update(ts=now, data=data)
    return {**data, "cached": False}
