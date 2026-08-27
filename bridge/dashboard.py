"""dashboard.py — admin API + dashboard page routes for kimi_bridge.

Mounted on the same FastAPI app. The page itself is a single static HTML
file (dashboard.html) with zero external dependencies.
"""

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
    codex_dir = r"C:\Users\gg1\.codex"
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
    codex_dir = r"C:\Users\gg1\.codex"
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
