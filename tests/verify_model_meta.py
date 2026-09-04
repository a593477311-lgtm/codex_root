"""
verify_model_meta.py —— 供应商模型能力元数据（上下文/思考档位）离线单测。

不依赖运行中的桥：直接 import bridge/dashboard.py 的纯函数，
用临时 models.json 验证 ensure_models_in_catalog 的 upsert 与能力注入。

用法:  python tests/verify_model_meta.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import dashboard as dash  # noqa: E402

results = []


def check(name, ok, fail_detail=""):
    results.append((name, ok, fail_detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {fail_detail}" if not ok and fail_detail else ""))


# --- 1. 档位对象生成 -------------------------------------------------------------

lv = dash._build_reasoning_levels(["low", "xhigh"])
check("1 档位对象生成",
      isinstance(lv, list) and len(lv) == 2
      and lv[0] == {"effort": "low", "description": "Fast responses with lighter reasoning"}
      and lv[1]["effort"] == "xhigh"
      and dash._build_reasoning_levels(["bogus"]) == [],
      repr(lv))


# --- 2. ensure_models_in_catalog：新模型带 meta 注册 ------------------------------


def make_catalog(tmpdir):
    path = os.path.join(tmpdir, "models.json")
    tpl = {
        "slug": "k3", "display_name": "k3", "context_window": 1048576,
        "max_context_window": 1048576, "effective_context_window_percent": 95,
        "supported_reasoning_levels": dash._build_reasoning_levels(dash._VALID_LEVELS),
        "default_reasoning_level": "medium", "base_instructions": "You are Codex based on GPT-5.",
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"models": [tpl]}, f)
    return path


with tempfile.TemporaryDirectory() as td:
    cat = make_catalog(td)
    meta = {"context_window": 128000, "effective_context_window_percent": 90,
            "reasoning_levels": ["medium", "high"], "default_reasoning_level": "high"}
    dash.ensure_models_in_catalog(["test-model"], meta, models_json_path=cat)
    with open(cat, encoding="utf-8") as f:
        data = json.load(f)
    m = next(x for x in data["models"] if x["slug"] == "test-model")
    ok2 = (m["context_window"] == 128000 and m["max_context_window"] == 128000
           and m["effective_context_window_percent"] == 90
           and [l["effort"] for l in m["supported_reasoning_levels"]] == ["medium", "high"]
           and m["default_reasoning_level"] == "high")
    check("2 新模型 meta 注册", ok2, json.dumps({k: m.get(k) for k in
          ("context_window", "effective_context_window_percent", "default_reasoning_level")}, ensure_ascii=False))

    # --- 3. upsert：已存在条目同步更新 ------------------------------------------
    meta2 = {"context_window": 64000, "effective_context_window_percent": 95,
             "reasoning_levels": ["low", "high"], "default_reasoning_level": "low"}
    dash.ensure_models_in_catalog(["test-model"], meta2, models_json_path=cat)
    with open(cat, encoding="utf-8") as f:
        data = json.load(f)
    m = next(x for x in data["models"] if x["slug"] == "test-model")
    ok3 = (m["context_window"] == 64000 and m["max_context_window"] == 64000
           and [l["effort"] for l in m["supported_reasoning_levels"]] == ["low", "high"]
           and m["default_reasoning_level"] == "low")
    check("3 已存在条目 upsert", ok3,
          json.dumps({"cw": m["context_window"], "lv": [l["effort"] for l in m["supported_reasoning_levels"]],
                      "dl": m["default_reasoning_level"]}))

    # 无 meta 时不得触碰已存在条目（防误伤 k3 等官方条目）
    dash.ensure_models_in_catalog(["test-model"], None, models_json_path=cat)
    with open(cat, encoding="utf-8") as f:
        m = next(x for x in json.load(f)["models"] if x["slug"] == "test-model")
    check("4 无 meta 不触碰存量", m["context_window"] == 64000, str(m.get("context_window")))


# --- 5. _resolve_model_context_window --------------------------------------------

r1 = dash._resolve_model_context_window({"model_meta": {"context_window": 256000}})
r2 = dash._resolve_model_context_window({"model_meta": {}, "active_model": "__no_such_model__"})
check("5 窗口解析（meta 优先 / 未知返回 None）", r1 == 256000 and r2 is None, f"r1={r1} r2={r2}")


failed = [r for r in results if not r[1]]
print(f"\n{'='*40}\n模型能力元数据单测: {len(results) - len(failed)}/{len(results)} PASS")
sys.exit(1 if failed else 0)
