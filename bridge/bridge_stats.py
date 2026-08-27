"""bridge_stats.py — SQLite-backed usage stats for kimi_bridge.

Hot-path discipline: callers push tiny dicts into `stats.note*`; a daemon
thread batch-writes to SQLite. The proxy hot path never touches the DB.
"""

import json
import os
import queue
import sqlite3
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_HERE, "bridge_stats.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  method TEXT, path TEXT, model TEXT,
  status INTEGER, latency_ms REAL,
  in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0,
  cached_tok INTEGER DEFAULT 0, reasoning_tok INTEGER DEFAULT 0,
  err TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, kind TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def _since(days):
    """Calendar-day window start: days=1 -> local midnight today, days=7 ->
    midnight 6 days ago (last 7 calendar days, today included). Fractional
    values keep the old rolling behavior (used for lookback comparisons)."""
    if days >= 1:
        lt = time.localtime()
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return midnight - (int(round(days)) - 1) * 86400
    return time.time() - days * 86400


def _date_range(datestr):
    """[start, end) epoch seconds for a local calendar day 'YYYY-MM-DD'."""
    y, m, d = (int(x) for x in datestr.split("-"))
    if not (2020 <= y and 1 <= m <= 12 and 1 <= d <= 31):
        raise ValueError("bad date")
    start = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
    return start, start + 86400


def _window(days, date=None):
    """(since, until) — until is only set for a specific calendar date."""
    if date:
        try:
            return _date_range(date)
        except Exception:
            pass
    return _since(days), None


class Stats:
    def __init__(self, db_path=DB_FILE, retention_days=30, enabled=True):
        self.enabled = enabled
        self.retention_days = retention_days
        self.started_at = time.time()
        self._q = queue.Queue()
        self._db_path = db_path
        self._last_prune = 0.0
        self._stop = threading.Event()
        if enabled:
            self._t = threading.Thread(target=self._writer, daemon=True)
            self._t.start()

    def configure(self, retention_days=None, enabled=None):
        if retention_days is not None:
            self.retention_days = retention_days
        if enabled is not None:
            self.enabled = enabled
        if self.enabled and not getattr(self, "_t", None):
            self._t = threading.Thread(target=self._writer, daemon=True)
            self._t.start()

    def note_request(self, **kw):
        if self.enabled:
            self._q.put(("req", kw))

    def note_event(self, kind, detail=""):
        if self.enabled:
            self._q.put(("ev", {"kind": kind, "detail": detail}))

    def _connect(self):
        con = sqlite3.connect(self._db_path, timeout=10)
        con.executescript(_SCHEMA)
        return con

    def _writer(self):
        con = self._connect()
        cur = con.cursor()
        while True:
            try:
                tag, payload = self._q.get(timeout=2.0)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            batch = [(tag, payload)]
            while True:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            try:
                for t, p in batch:
                    if t == "req":
                        cur.execute(
                            "INSERT INTO requests(ts,method,path,model,status,latency_ms,"
                            "in_tok,out_tok,cached_tok,reasoning_tok,err)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                p.get("ts", time.time()), p.get("method"), p.get("path"),
                                p.get("model"), p.get("status"), p.get("latency_ms"),
                                p.get("in_tok", 0), p.get("out_tok", 0),
                                p.get("cached_tok", 0), p.get("reasoning_tok", 0),
                                p.get("err"),
                            ),
                        )
                    else:
                        cur.execute(
                            "INSERT INTO events(ts,kind,detail) VALUES(?,?,?)",
                            (time.time(), p.get("kind"), p.get("detail", "")[:500]),
                        )
                con.commit()
            except sqlite3.Error:
                pass
            now = time.time()
            if now - self._last_prune > 3600:
                self._last_prune = now
                try:
                    cutoff = now - self.retention_days * 86400
                    cur.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
                    cur.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                    con.commit()
                except sqlite3.Error:
                    pass
        con.close()

    def _ro(self):
        con = sqlite3.connect(self._db_path, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def summary(self, days, date=None):
        since, until = _window(days, date)
        where = "ts>=?" + (" AND ts<?" if until else "")
        args = (since, until) if until else (since,)
        con = self._ro()
        r = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(in_tok),0) tin, COALESCE(SUM(out_tok),0) tout,"
            " COALESCE(SUM(cached_tok),0) tcache, COALESCE(SUM(reasoning_tok),0) treason,"
            " COALESCE(AVG(latency_ms),0) avg_lat,"
            " SUM(CASE WHEN status>=400 OR err IS NOT NULL THEN 1 ELSE 0 END) errors"
            " FROM requests WHERE " + where, args,
        ).fetchone()
        top = con.execute(
            "SELECT model, COUNT(*) n, SUM(in_tok)+SUM(out_tok) t"
            " FROM requests WHERE " + where + " AND model IS NOT NULL"
            " GROUP BY model ORDER BY t DESC LIMIT 1", args,
        ).fetchone()
        active = con.execute(
            "SELECT COUNT(DISTINCT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')) d"
            " FROM requests WHERE " + where, args,
        ).fetchone()
        con.close()
        # Kimi/OpenAI semantics: input_tokens already includes cached_tokens (verified by probe)
        total_in = r["tin"]
        return {
            "requests": r["n"], "tokens_in": r["tin"], "tokens_out": r["tout"],
            "tokens_cached": r["tcache"], "tokens_reasoning": r["treason"],
            "tokens_total": total_in + r["tout"],
            "cache_hit_rate": (r["tcache"] / total_in) if total_in else 0,
            "errors": r["errors"], "avg_latency_ms": round(r["avg_lat"], 1),
            "active_days": active["d"],
            "top_model": ({"model": top["model"], "requests": top["n"], "tokens": top["t"]} if top else None),
        }

    def daily(self, days, date=None):
        since, until = _window(days, date)
        where = "ts>=?" + (" AND ts<?" if until else "")
        args = (since, until) if until else (since,)
        con = self._ro()
        rows = con.execute(
            "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') d,"
            " COALESCE(model,'?') m, COUNT(*) n,"
            " SUM(in_tok) tin, SUM(out_tok) tout, SUM(cached_tok) tcache"
            " FROM requests WHERE " + where + " GROUP BY d, m ORDER BY d", args,
        ).fetchall()
        con.close()
        out = {}
        for r in rows:
            day = out.setdefault(r["d"], {"requests": 0, "in": 0, "out": 0, "cached": 0, "models": {}})
            day["requests"] += r["n"]
            day["in"] += r["tin"] or 0
            day["out"] += r["tout"] or 0
            day["cached"] += r["tcache"] or 0
            day["models"][r["m"]] = (day["models"].get(r["m"], 0) + (r["tin"] or 0) + (r["tout"] or 0))
        return out

    def models(self, days, date=None):
        since, until = _window(days, date)
        where = "ts>=?" + (" AND ts<?" if until else "")
        args = (since, until) if until else (since,)
        con = self._ro()
        rows = con.execute(
            "SELECT COALESCE(model,'?') m, COUNT(*) n,"
            " SUM(in_tok) tin, SUM(out_tok) tout, SUM(cached_tok) tcache"
            " FROM requests WHERE " + where + " GROUP BY m ORDER BY (tin+tout+tcache) DESC", args,
        ).fetchall()
        con.close()
        return [
            {"model": r["m"], "requests": r["n"],
             "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0,
             "tokens_cached": r["tcache"] or 0,
             "tokens_total": (r["tin"] or 0) + (r["tout"] or 0)}
            for r in rows
        ]

    def recent_requests(self, limit=50):
        con = self._ro()
        rows = con.execute(
            "SELECT ts,method,path,model,status,latency_ms,in_tok,out_tok,cached_tok,reasoning_tok,err"
            " FROM requests ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def recent_events(self, limit=50):
        con = self._ro()
        rows = con.execute("SELECT ts,kind,detail FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        con.close()
        return [dict(r) for r in rows]

    def db_size(self):
        try:
            return os.path.getsize(self._db_path)
        except OSError:
            return 0


stats = Stats(enabled=False)  # replaced by bridge at startup with real config


def extract_usage(data):
    """Pull token usage out of a Responses-API response object."""
    if not isinstance(data, dict):
        return {}
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    ind = usage.get("input_tokens_details") or {}
    outd = usage.get("output_tokens_details") or {}
    cached = (
        ind.get("cached_tokens")
        or usage.get("cached_input_tokens")
        or usage.get("cache_read_input_tokens")
        or 0
    )
    return {
        "in_tok": usage.get("input_tokens") or 0,
        "out_tok": usage.get("output_tokens") or 0,
        "cached_tok": cached,
        "reasoning_tok": outd.get("reasoning_tokens") or usage.get("reasoning_output_tokens") or 0,
    }
