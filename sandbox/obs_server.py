#!/usr/bin/env python3
"""Observatory dashboard — REAL-DATA prototype (read-only, localhost only).

Serves live JSON from the ACTUAL pipeline artifacts (no hardcoded data) plus a
single-page frontend. S5: read-only, localhost-bound, no order/panic actions.
This is a throwaway prototype under sandbox/ to nail the dynamic behaviour +
visual direction; it does NOT touch the production dashboard_server.py.

  python3 sandbox/obs_server.py            # -> http://127.0.0.1:8790
"""
import csv
import datetime as dt
import json
import os
import statistics
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
METRICS = os.path.join(ROOT, "work/metrics.ndjson")
GAPS = os.path.join(ROOT, "work/event_packs/capture_gaps.csv")
ALERT = os.path.join(ROOT, "work/live/capture_alert.json")
LIFECYCLE = os.path.join(ROOT, "work/lifecycle_status.json")
HTML = os.path.join(HERE, "obs.html")
UPLOT = os.path.join(ROOT, "third_party/uplot")  # vendored, pinned (no CDN)


def tail_feed(n=200, tail_bytes=28_000_000):
    """Last n 'feed' health records from metrics.ndjson. Fast: string-filter for
    the feed marker before json.loads (market_data lines outnumber feed ~500:1)."""
    try:
        sz = os.path.getsize(METRICS)
    except OSError:
        return []
    out = []
    with open(METRICS, "rb") as f:
        if sz > tail_bytes:
            f.seek(sz - tail_bytes)
            f.readline()  # discard the partial line the seek landed in
        for line in f:
            if b'"type":"feed"' in line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out[-n:]


def parse_gaps():
    days = {}
    try:
        with open(GAPS) as f:
            for r in csv.DictReader(f):
                try:
                    s, e = int(r["start_us"]), int(r["end_us"])
                except (KeyError, ValueError):
                    continue
                day = dt.datetime.utcfromtimestamp(s / 1e6).strftime("%Y-%m-%d")
                d = days.setdefault(day, {"date": day, "count": 0, "downtime_s": 0.0,
                                          "largest_s": 0.0, "intervals": []})
                dur = (e - s) / 1e6
                d["count"] += 1
                d["downtime_s"] += dur
                d["largest_s"] = max(d["largest_s"], dur)
                d["intervals"].append([s, e])
    except FileNotFoundError:
        pass
    return {"days": [days[k] for k in sorted(days)],
            "total": sum(d["count"] for d in days.values())}


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def build_feed():
    recs = tail_feed()
    keys = ("ts_ms", "msg_rate_hz", "freshness_ms", "reconnects", "messages",
            "trades", "tickers", "recorder_dropped", "telemetry_dropped",
            "gaps", "connected", "valid")
    series = [{k: r.get(k) for k in keys} for r in recs]
    latest = recs[-1] if recs else None
    live_age_s = None
    if latest:
        live_age_s = round(time.time() - latest["ts_ms"] / 1000.0, 1)
    return {"series": series, "latest": latest, "live_age_s": live_age_s,
            "now_ms": int(time.time() * 1000)}


def build_gates():
    lc = read_json(LIFECYCLE) or {}
    stages = []
    for s in lc.get("stages", []):
        stages.append({
            "id": s.get("id"), "label": s.get("label"),
            "status": (s.get("status") or "unknown"),
            "reason": s.get("blocking_reason", ""),
            "checked_at_ms": s.get("checked_at_ms"),
            "checks": [{"name": c.get("name"), "status": c.get("status")}
                       for c in s.get("checks", [])],
        })
    return {"overall": lc.get("status", "unknown"), "stages": stages,
            "generated_at_ms": lc.get("generated_at_ms")}


MANIFEST = os.path.join(ROOT, "work/warehouse/manifest.csv")


# ---- Q1: is it healthy NOW? ----
def _proc(pattern):
    """(alive, pid, etime) for a process matched by pgrep -f, else (False,..)."""
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=3)
        pid = out.stdout.split()[0] if out.stdout.strip() else None
        if not pid:
            return {"alive": False, "pid": None, "etime": None}
        et = subprocess.run(["ps", "-o", "etime=", "-p", pid], capture_output=True, text=True, timeout=3)
        return {"alive": True, "pid": int(pid), "etime": et.stdout.strip()}
    except Exception:
        return {"alive": None, "pid": None, "etime": None}  # UNKNOWN


_baseline_cache = {"t": 0, "val": None}


def _rate_window(f, sz, target, first, last, half_ms=600_000):
    """msg_rate_hz values within ±half of `target` ms. Binary-search the byte
    offset (records are monotonic in ts; gaps break a linear estimate), then
    scan the window. Bounded read so a bad estimate can't run away."""
    if not (first and last and first < target < last):
        return []
    lo, hi = 0, sz
    while hi - lo > 131072:
        mid = (lo + hi) // 2
        f.seek(mid)
        f.readline()
        ts = None
        for _ in range(400):
            line = f.readline()
            if not line:
                break
            if b'"ts_ms"' in line:
                try:
                    ts = json.loads(line)["ts_ms"]
                    break
                except ValueError:
                    pass
        if ts is None or ts < target:
            lo = mid
        else:
            hi = mid
    f.seek(lo)
    f.readline()
    rates, read = [], 0
    for line in f:
        read += len(line)
        if read > 90_000_000:
            break
        if b'"type":"feed"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        d = r["ts_ms"] - target
        if d < -half_ms:
            continue
        if d > half_ms:
            break
        rates.append(r["msg_rate_hz"])
    return rates


def _rate_baseline(recs_now):
    """msg_rate_hz band at the SAME clock-time over the last N days (up to 7,
    bounded by the metrics span). Pooling multiple days AUTO-DEGRADES past a
    single gap day (mod 2): if yesterday was a hole, the other days still anchor
    the median band. Cached 5 min; per-day p50s surfaced for provenance."""
    if time.time() - _baseline_cache["t"] < 300 and _baseline_cache["val"] is not None:
        return _baseline_cache["val"]
    band = {"status": "unknown"}
    try:
        sz = os.path.getsize(METRICS)
        with open(METRICS, "rb") as f:
            first = None
            for line in f:
                if b'"ts_ms"' in line:
                    first = json.loads(line)["ts_ms"]
                    break
            f.seek(max(0, sz - 2_000_000))
            last = None
            for line in f:
                if b'"ts_ms"' in line:
                    try:
                        last = json.loads(line)["ts_ms"]
                    except ValueError:
                        pass
            now = recs_now[-1]["ts_ms"] if recs_now else last
            ndays = min(7, max(1, int((last - first) / 86_400_000))) if first and last else 1
            pooled, per_day = [], []
            for k in range(1, ndays + 1):
                w = _rate_window(f, sz, now - k * 86_400_000, first, last)
                if w:
                    ws = sorted(w)
                    per_day.append({"day_ago": k, "p50": ws[len(ws) // 2], "n": len(ws)})
                    pooled.extend(w)
        if len(pooled) >= 5:
            pooled.sort()
            band = {"status": "ok", "lo": pooled[len(pooled) // 10],
                    "p50": pooled[len(pooled) // 2], "hi": pooled[-1 - len(pooled) // 10],
                    "n": len(pooled), "ndays": ndays, "days": per_day,
                    "degraded": len([d for d in per_day if d["p50"] > 1]) < ndays,
                    "prov": {"source": "metrics.ndjson · type=feed · msg_rate_hz",
                             "window": "same clock-time ±10min, last %d day(s)" % ndays,
                             "per_day": per_day}}
    except Exception:
        band = {"status": "unknown"}
    _baseline_cache.update(t=time.time(), val=band)
    return band


def build_q1():
    recs = tail_feed(120)
    L = recs[-1] if recs else None
    # channel breakdown (breakdown over global): trade vs ticker rate from last 2
    ch = {"status": "unknown"}
    if len(recs) >= 2:
        a, b = recs[-2], recs[-1]
        dt_s = (b["ts_ms"] - a["ts_ms"]) / 1000.0
        if dt_s > 0:
            ch = {"status": "ok",
                  "trades_per_s": max(0, (b["trades"] - a["trades"]) / dt_s),
                  "tickers_per_s": max(0, (b["tickers"] - a["tickers"]) / dt_s)}
    try:
        vfs = os.statvfs(ROOT)
        disk = {"free_gb": vfs.f_bavail * vfs.f_frsize / 1e9,
                "total_gb": vfs.f_blocks * vfs.f_frsize / 1e9}
        disk["used_pct"] = 100 * (1 - disk["free_gb"] / disk["total_gb"])
    except Exception:
        disk = {"status": "unknown"}
    return {
        "procs": {"ws_shadow": _proc("build/ws_shadow"),
                  "ingest": _proc("tools/ingest.py"),
                  "supervisor": _proc("pipeline_supervisor.sh")},
        "feed": {"freshness_ms": (L or {}).get("freshness_ms"),
                 "msg_rate_hz": (L or {}).get("msg_rate_hz"),
                 "connected": (L or {}).get("connected"),
                 "age_s": (round(time.time() - L["ts_ms"] / 1000, 1) if L else None),
                 "capture": (L or {}).get("capture"),
                 "prov": {"source": "work/metrics.ndjson · type=feed (newest)", "record": L}},
        "channels": ch,
        "baseline": _rate_baseline(recs),
        "disk": disk,
    }


# ---- Q2: is the data USABLE? ----
def build_q2():
    # coverage matrix from the warehouse manifest (breakdown over global), with a
    # per-cell BASELINE (mod 1): each cell's rows vs its category's own median
    # across days, so a low-but-normal category reads differently from a real
    # shortfall. Manifest rows kept per cell for provenance (click -> source).
    raw = {}
    try:
        with open(MANIFEST) as f:
            for r in csv.DictReader(f):
                d, c = r.get("date"), r.get("category")
                if not d or not c:
                    continue
                e = raw.setdefault((d, c), {"rows": 0, "srcs": []})
                rows = int(r.get("row_count") or 0)
                e["rows"] += rows
                e["srcs"].append({"table": r.get("table"), "subcategory": r.get("subcategory"),
                                  "rows": rows, "file_path": r.get("file_path"),
                                  "md5": (r.get("file_md5") or "")[:12]})
    except FileNotFoundError:
        pass
    dates = sorted({d for d, _ in raw})
    cats = sorted({c for _, c in raw})
    matrix = []
    for c in cats:
        day_rows = [raw[(d, c)]["rows"] for d in dates if (d, c) in raw]
        med = statistics.median(day_rows) if day_rows else 0
        cells = []
        for d in dates:
            e = raw.get((d, c))
            rows = e["rows"] if e else 0
            cells.append({"date": d, "rows": rows, "median": med,
                          "pct": ((rows - med) / med * 100 if med else None),
                          "prov": {"source": "work/warehouse/manifest.csv",
                                   "category": c, "date": d, "rows": rows,
                                   "median_over_days": med, "files": (e["srcs"] if e else [])}})
        matrix.append({"category": c, "cells": cells, "median": med})
    # latency distribution p50/p95/p99 (distribution over average — NO mean headline)
    recs = tail_feed(600)
    fr = sorted(max(0, r["freshness_ms"]) for r in recs if r.get("freshness_ms") is not None)
    def pct(p):
        return fr[min(len(fr) - 1, int(p / 100 * len(fr)))] if fr else None
    lat = ({"p50": pct(50), "p95": pct(95), "p99": pct(99), "max": fr[-1], "n": len(fr),
            "prov": {"source": "work/metrics.ndjson · type=feed · freshness_ms",
                     "window": "last %d feed records (tail)" % len(fr)}}
           if fr else {"status": "unknown"})
    L = recs[-1] if recs else {}
    counts = {"recorder_dropped": L.get("recorder_dropped"),
              "telemetry_dropped": L.get("telemetry_dropped"),
              "corrupt": {"status": "not_persisted",
                          "note": "per-day unparsed count available via capture_gaps --date; not yet persisted"}}
    # 7-clean-days progress (with evidence status) from the gap record
    g = parse_gaps()
    gapdays = {d["date"]: d["count"] for d in g["days"]}
    today = dt.datetime.utcnow().date()
    days7 = []
    streak = 0
    for i in range(7):
        day = (today - dt.timedelta(days=i)).isoformat()
        scanned = day in gapdays
        clean = scanned and gapdays[day] == 0
        days7.append({"date": day, "scanned": scanned,
                      "gaps": gapdays.get(day), "clean": clean,
                      "evidence": "ok" if clean else ("gaps" if scanned else "no-scan")})
    for d in days7:  # streak from today backwards
        if d["clean"]:
            streak += 1
        else:
            break
    return {"coverage": {"dates": dates, "matrix": matrix},
            "latency": lat, "counts": counts,
            "clean": {"streak": streak, "target": 7, "days": days7}}


ROUTES = {
    "/api/feed": build_feed,
    "/api/gaps": parse_gaps,
    "/api/alert": lambda: (read_json(ALERT) or {"status": "unknown"}),
    "/api/gates": build_gates,
    "/api/q1": build_q1,
    "/api/q2": build_q2,
}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(HTML, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(404, b"obs.html missing", "text/plain")
        if path.startswith("/uplot/"):  # vendored static (read-only, pinned)
            fn = os.path.basename(path)
            fp = os.path.join(UPLOT, fn)
            if fn in ("uPlot.iife.min.js", "uPlot.min.css") and os.path.exists(fp):
                ct = "text/javascript" if fn.endswith(".js") else "text/css"
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), ct)
            return self._send(404, b"nf", "text/plain")
        if path == "/stream":
            return self.stream()
        if path in ROUTES:
            try:
                body = json.dumps(ROUTES[path]()).encode()
            except Exception as e:  # never 500 the dashboard; surface as UNKNOWN
                body = json.dumps({"error": str(e)}).encode()
            return self._send(200, body, "application/json")
        self._send(404, b"not found", "text/plain")

    def stream(self):
        """SSE push (replaces client polling). Sends a full snapshot on connect,
        then tails metrics.ndjson and pushes each new 'feed' record as it lands;
        re-pushes gaps/alert/gates every 8s; keepalive ping every 15s. Same
        event-stream shape as the production dashboard_server.py."""
        self.send_response(200)
        for k, v in (("Content-Type", "text/event-stream; charset=utf-8"),
                     ("Cache-Control", "no-store"), ("Connection", "keep-alive"),
                     ("X-Accel-Buffering", "no")):
            self.send_header(k, v)
        self.end_headers()

        def w(s):
            self.wfile.write(s.encode("utf-8"))
            self.wfile.flush()

        try:
            w("retry: 3000\n\n")
            snap = {"feed": build_feed(), "gaps": parse_gaps(),
                    "alert": read_json(ALERT) or {"status": "unknown"},
                    "gates": build_gates(), "q1": build_q1(), "q2": build_q2()}
            w("event: snapshot\ndata: " + json.dumps(snap) + "\n\n")
            pos = os.path.getsize(METRICS)  # tail forward from current EOF
            last_aux = last_ping = time.time()
            while True:
                time.sleep(0.5)
                try:
                    sz = os.path.getsize(METRICS)
                except OSError:
                    sz = pos
                if sz > pos:
                    with open(METRICS, "rb") as f:
                        f.seek(pos)
                        chunk = f.read(sz - pos)
                    nl = chunk.rfind(b"\n")
                    if nl >= 0:
                        pos += nl + 1
                        for line in chunk[:nl].split(b"\n"):
                            if b'"type":"feed"' in line:
                                try:
                                    rec = json.loads(line)
                                except ValueError:
                                    continue
                                w("event: feed\ndata: " + json.dumps(rec) + "\n\n")
                now = time.time()
                if now - last_aux >= 8:
                    w("event: aux\ndata: " + json.dumps({
                        "gaps": parse_gaps(),
                        "alert": read_json(ALERT) or {"status": "unknown"},
                        "gates": build_gates(),
                        "q1": build_q1(), "q2": build_q2()}) + "\n\n")
                    last_aux = now
                if now - last_ping >= 15:
                    w(": ping\n\n")
                    last_ping = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client went away


if __name__ == "__main__":
    port = int(os.environ.get("OBS_PORT", "8790"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)  # localhost only (S5)
    print("observatory prototype (read-only) -> http://127.0.0.1:%d" % port)
    srv.serve_forever()
