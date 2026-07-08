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


ROUTES = {
    "/api/feed": build_feed,
    "/api/gaps": parse_gaps,
    "/api/alert": lambda: (read_json(ALERT) or {"status": "unknown"}),
    "/api/gates": build_gates,
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
                    "gates": build_gates()}
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
                        "gates": build_gates()}) + "\n\n")
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
