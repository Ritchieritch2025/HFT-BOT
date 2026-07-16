#!/usr/bin/env python3
"""Minimal READ-ONLY Kalshi balance probe for the status bot.

GET /portfolio/balance only. Signing mirrors src/client.cpp sign_request()
(and tools/account_view.py), reimplemented self-contained so the status bot
does not drag in the gold_load/numpy chain. Contains NO order-mutation code;
it is a single authenticated GET. Credentials come from the environment
(KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH). Prints one line:
    $<balance>  (portfolio $<value>)
or "balance error: ..." on failure. Money is parsed byte-exactly from the
fixed-point dollar string (no float).
"""
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

API_PREFIX = "/trade-api/v2"
BASE = os.environ.get("KALSHI_BASE_URL", "https://external-api.kalshi.com")


def _sign(key_path, message):
    p = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sigopt", "rsa_padding_mode:pss",
         "-sigopt", "rsa_pss_saltlen:digest", "-sign", key_path, "-binary"],
        input=message.encode(), capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("openssl signing failed")
    return base64.b64encode(p.stdout).decode()


def _get(path, key_id, key_path, timeout=15):
    ts = str(int(time.time() * 1000))
    message = ts + "GET" + API_PREFIX + path.split("?", 1)[0]
    req = urllib.request.Request(BASE + API_PREFIX + path, method="GET")
    req.add_header("KALSHI-ACCESS-KEY", key_id)
    req.add_header("KALSHI-ACCESS-SIGNATURE", _sign(key_path, message))
    req.add_header("KALSHI-ACCESS-TIMESTAMP", ts)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _dollars(s):
    """Fixed-point dollar string -> display string, byte-exact, no float."""
    s = str(s)
    neg = s.startswith("-")
    s = s.lstrip("-")
    if "." in s:
        whole, frac = s.split(".", 1)
    else:
        whole, frac = s, ""
    frac = (frac + "00")[:2]  # cents display
    return "%s$%s.%s" % ("-" if neg else "", whole or "0", frac)


def balance_line():
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        return "balance error: KALSHI creds unset"
    try:
        b = _get("/portfolio/balance", key_id, key_path)
    except urllib.error.HTTPError as e:
        return "balance error: HTTP %d" % e.code
    except Exception as e:
        return "balance error: %s" % str(e)[:120]
    bal = b.get("balance_dollars")
    if bal is None and "balance" in b:  # cents int fallback
        c = int(b["balance"])
        bal = "%d.%02d" % (c // 100, c % 100)
    pv = b.get("portfolio_value_dollars")
    if pv is None and "portfolio_value" in b:
        c = int(b["portfolio_value"])
        pv = "%d.%02d" % (c // 100, c % 100)
    out = "💰 余额 %s" % _dollars(bal)
    if pv is not None:
        out += "  ·  组合价值 %s" % _dollars(pv)
    return out


if __name__ == "__main__":
    print(balance_line())
