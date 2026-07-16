#!/usr/bin/env python3
"""Shared helpers for the W-TELEGRAM-01 monitor (send + config + state).

STRICTLY monitoring-side. Never imports any live_order-class module; the only
Kalshi call anywhere in this package is the read-only GET /portfolio/balance
in balance_probe.py. Telegram delivery is a plain HTTPS POST with retry; a
delivery failure logs and never raises into a caller that touches the pipeline.
stdlib only.
"""
import json
import os
import time
import urllib.parse
import urllib.request

ROOT = os.environ.get("KALSHI_ROOT", os.path.expanduser("~/hft-bot"))
LIVE = os.path.join(ROOT, "work", "live")
MON = os.path.join(LIVE, "monitor")          # our own state dir (never pipeline's)
LOG = os.path.join(MON, "telegram.log")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

RED, YELLOW, INFO = "🔴", "⚠️", "ℹ️"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg):
    os.makedirs(MON, exist_ok=True)
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (_now(), msg))
    except OSError:
        pass


def load_conf(path=None):
    """Parse telegram_monitor.conf -> dict[str,str]. Missing file = empty."""
    path = path or os.path.join(ROOT, "deploy", "telegram_monitor.conf")
    conf = {}
    try:
        for ln in open(path):
            ln = ln.split("#", 1)[0].strip()
            if "=" in ln:
                k, v = ln.split("=", 1)
                conf[k.strip()] = v.strip()
    except OSError:
        pass
    return conf


def send(text, retries=3):
    """POST to Telegram with retry. Returns True on delivery, False otherwise.
    NEVER raises — monitoring must not break its caller."""
    if not TOKEN or not CHAT:
        log("SEND SKIPPED (creds unset): %s" % text.replace("\n", " ")[:120])
        return False
    data = urllib.parse.urlencode({
        "chat_id": CHAT, "text": text, "disable_web_page_preview": 1,
    }).encode()
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 200:
                    log("SENT: %s" % text.replace("\n", " ")[:120])
                    return True
        except Exception as e:
            log("SEND attempt %d failed: %s" % (attempt + 1, str(e)[:80]))
            time.sleep(2 * (attempt + 1))
    log("SEND GAVE UP: %s" % text.replace("\n", " ")[:120])
    return False


def state_path(name):
    os.makedirs(MON, exist_ok=True)
    return os.path.join(MON, name)


def read_json(name, default=None):
    try:
        return json.load(open(state_path(name)))
    except Exception:
        return default


def write_json(name, obj):
    p = state_path(name)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)
