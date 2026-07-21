#!/usr/bin/env python3
"""Read-only market-feed readiness precheck.

This tool answers one operator question without touching the network:

  "Why is the dashboard not showing real Kalshi market feed yet?"

It checks only local state:
  - KALSHI_API_KEY_ID is present, without printing it;
  - KALSHI_PRIVATE_KEY_PATH is present and points at a file;
  - build/ws_shadow and tools/exchange_check.sh are present;
  - the dashboard metrics file contains fresh non-synthetic kalshi_ws feed rows.

It never opens a socket, never reads or prints the private key contents, and
never places orders.
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def now_ms():
    return int(time.time() * 1000)


def _read_ndjson(path, limit=1000):
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= limit:
                read = min(65536, pos)
                pos -= read
                f.seek(pos)
                data = f.read(read) + data
    except OSError:
        return []
    for line in data.splitlines()[-limit:]:
        try:
            out.append(json.loads(line.decode("utf-8", "replace")))
        except Exception:
            pass
    return out


def _check(name, ok, detail):
    return {"name": name, "status": "pass" if ok else "fail", "detail": detail}


def collect_status(root=ROOT, metrics_path=None, env=None, now=None):
    env = dict(os.environ if env is None else env)
    now = now_ms() if now is None else int(now)
    metrics_path = metrics_path or os.path.join(root, "work", "metrics.ndjson")

    key_id = env.get("KALSHI_API_KEY_ID", "")
    key_path = env.get("KALSHI_PRIVATE_KEY_PATH", "")
    kalshi_env = env.get("KALSHI_ENV", "unset")
    ws_shadow = os.path.join(root, "build", "ws_shadow")
    exchange_check = os.path.join(root, "tools", "exchange_check.sh")

    checks = []
    checks.append(_check("api_key_id_env", bool(key_id),
                         "KALSHI_API_KEY_ID is set" if key_id else
                         "KALSHI_API_KEY_ID is missing"))
    checks.append(_check("private_key_path_env", bool(key_path),
                         "KALSHI_PRIVATE_KEY_PATH is set" if key_path else
                         "KALSHI_PRIVATE_KEY_PATH is missing"))
    checks.append(_check("private_key_file", bool(key_path and os.path.isfile(key_path)),
                         "private key file exists" if key_path and os.path.isfile(key_path)
                         else "private key file is not available"))
    checks.append(_check("ws_shadow_binary", os.path.exists(ws_shadow),
                         "build/ws_shadow exists" if os.path.exists(ws_shadow)
                         else "build/ws_shadow missing; run make build/ws_shadow"))
    checks.append(_check("exchange_check_script", os.path.exists(exchange_check),
                         "tools/exchange_check.sh exists" if os.path.exists(exchange_check)
                         else "tools/exchange_check.sh missing"))

    rows = _read_ndjson(metrics_path)
    real_feed = [
        r for r in rows
        if r.get("type") == "feed" and r.get("source") == "kalshi_ws" and
        r.get("synthetic") is False
    ]
    real_market = [
        r for r in rows
        if r.get("type") == "market_data" and r.get("source") == "kalshi_ws" and
        r.get("synthetic") is False
    ]
    latest_feed = max(real_feed, key=lambda r: r.get("ts_ms", 0), default=None)
    latest_market = max(real_market, key=lambda r: r.get("ts_ms", 0), default=None)
    latest_ts = max(
        [int(r.get("ts_ms", 0) or 0) for r in (latest_feed, latest_market) if r],
        default=0,
    )
    latest_age = None if latest_ts <= 0 else max(0, now - latest_ts)
    feed_active = bool(
        latest_feed and latest_market and latest_age is not None and latest_age <= 5000 and
        latest_feed.get("connected") is True and latest_feed.get("valid") is not False
    )

    if feed_active:
        checks.append(_check("real_feed_metrics", True,
                             "fresh non-synthetic kalshi_ws feed + market_data rows present"))
    elif real_feed or real_market:
        checks.append(_check("real_feed_metrics", False,
                             "kalshi_ws metrics exist but are stale or incomplete"))
    else:
        checks.append(_check("real_feed_metrics", False,
                             "no non-synthetic kalshi_ws metrics in %s" %
                             os.path.relpath(metrics_path, root)))

    local_ready = all(c["status"] == "pass" for c in checks
                      if c["name"] != "real_feed_metrics")
    if feed_active:
        status = "active"
        summary = "Real Kalshi market feed is active."
    elif local_ready:
        status = "ready"
        summary = "Local prerequisites are ready; run the read-only exchange check."
    else:
        status = "missing_prerequisites"
        summary = "Market feed is not ready; local prerequisites are missing."

    return {
        "type": "feed_readiness",
        "status": status,
        "summary": summary,
        "generated_at_ms": now,
        "env": {
            "kalshi_env": kalshi_env,
            "api_key_id_present": bool(key_id),
            "private_key_path_present": bool(key_path),
            "private_key_file_exists": bool(key_path and os.path.isfile(key_path)),
        },
        "binaries": {
            "ws_shadow_built": os.path.exists(ws_shadow),
            "exchange_check_present": os.path.exists(exchange_check),
        },
        "metrics": {
            "path": os.path.relpath(metrics_path, root),
            "present": os.path.exists(metrics_path),
            "real_feed_rows": len(real_feed),
            "real_market_rows": len(real_market),
            "latest_real_age_ms": latest_age,
            "active": feed_active,
        },
        "checks": checks,
        "next_command": (
            'KALSHI_ENV=prod KALSHI_ALLOW_PROD=1 '
            'KALSHI_API_KEY_ID="<your_key_id>" '
            'KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi/private_key.pem" '
            'tools/exchange_check.sh --env prod --ws-seconds 30'
        ),
    }


def report_lines(status):
    lines = []
    ok = status["status"] in ("ready", "active")
    for c in status.get("checks", []):
        prefix = "PASS: " if c.get("status") == "pass" else "FAIL: "
        lines.append(prefix + "%s - %s" % (c.get("name"), c.get("detail")))
    if status["status"] == "active":
        lines.append("PASS: %s" % status["summary"])
    elif status["status"] == "ready":
        lines.append("PASS: %s" % status["summary"])
    else:
        lines.append("FAIL: %s" % status["summary"])
    return ok, lines


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--metrics", default=os.path.join(ROOT, "work", "metrics.ndjson"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])

    status = collect_status(ROOT, os.path.abspath(args.metrics))
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0 if status["status"] in ("ready", "active") else 1
    ok, lines = report_lines(status)
    for line in lines:
        print(line)
    print("ALL PASS" if ok else "FEED READINESS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
