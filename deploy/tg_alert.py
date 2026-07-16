#!/usr/bin/env python3
"""W-TELEGRAM-01 §1 three-tier alert engine. Runs every minute via
kalshi-tg-alert.timer (independent unit; never on the capture/ingest/export
path). Reads pipeline state read-only, classifies conditions into
red/yellow/info, and pushes with per-condition anti-spam (one push per state
CHANGE + a re-send every REALERT_MIN while still bad).

Checks implemented now:
  RED    capture down / feed stale; disk > DISK_RED_PCT; balance CHANGED
         (5-min poll — no live authz, balance must be flat); chrony offset;
         seal_alarm present (export/seal failed past alarm line)
  YELLOW capture gap; W09 powered-on idle > N h
Info-tier and cost/event-volume/upstream live in the daily summary + intel
modules; this engine owns the fast event-triggered signals.

Test hook: touch work/live/monitor/TEST_GAP to force a synthetic yellow gap
alert (acceptance §④ — flag only, never touches real capture data).
STRICTLY read-only; imports no live_order module. Balance read is the
read-only GET in balance_probe.py.
"""
import glob
import importlib.util
import os
import subprocess
import time

import tg_common as tg

CONF = tg.load_conf()


def _f(key, default):
    try:
        return float(CONF.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _load_live_json(name):
    import json
    try:
        return json.load(open(os.path.join(tg.LIVE, name)))
    except Exception:
        return None


def _disk_pct(path="/"):
    try:
        out = subprocess.run(["df", path], capture_output=True, text=True, timeout=10)
        return int(out.stdout.splitlines()[1].split()[4].rstrip("%"))
    except Exception:
        return None


def _ingest_alive():
    try:
        out = subprocess.run(["pgrep", "-f", "ingest.py --loop"],
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.split())
    except Exception:
        return None  # unknown -> don't false-alarm


def _feed_stale_s():
    newest = 0.0
    for p in glob.glob(os.path.join(tg.ROOT, "work", "raw", "date=*", "*")):
        try:
            m = os.path.getmtime(p)
            if m > newest:
                newest = m
        except OSError:
            pass
    return (time.time() - newest) if newest else None


def _chrony_offset():
    try:
        out = subprocess.run(["chronyc", "tracking"], capture_output=True,
                             text=True, timeout=10)
        for ln in out.stdout.splitlines():
            if "Last offset" in ln:
                return abs(float(ln.split(":")[1].split()[0]))
    except Exception:
        return None
    return None


def _balance_now():
    """Read-only GET /portfolio/balance via balance_probe (returns raw dict-ish
    string). Returns (ok, value_str)."""
    try:
        p = os.path.join(tg.ROOT, "deploy", "balance_probe.py")
        spec = importlib.util.spec_from_file_location("balance_probe", p)
        bp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bp)
        key_id = os.environ.get("KALSHI_API_KEY_ID")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not key_id or not key_path:
            return False, None
        b = bp._get("/portfolio/balance", key_id, key_path)
        val = b.get("balance_dollars")
        if val is None and "balance" in b:
            val = str(b["balance"])
        return True, str(val)
    except Exception as e:
        tg.log("balance read error: %s" % str(e)[:80])
        return False, None


def check_balance_tripwire(fired):
    """5-min poll; ANY change from the last seen value = RED. 3 consecutive
    read failures = YELLOW. State in monitor/balance_state.json."""
    st = tg.read_json("balance_state.json", {}) or {}
    now = time.time()
    if now - st.get("last_poll", 0) < _f("BALANCE_POLL_MIN", 5) * 60:
        return
    ok, val = _balance_now()
    st["last_poll"] = now
    if not ok:
        st["fail_streak"] = st.get("fail_streak", 0) + 1
        if st["fail_streak"] >= _f("BALANCE_FAIL_YELLOW", 3):
            fired.append((tg.YELLOW, "balance",
                          "余额端点连续 %d 次读取失败" % st["fail_streak"]))
    else:
        st["fail_streak"] = 0
        prev = st.get("value")
        if prev is not None and val != prev:
            fired.append((tg.RED, "balance",
                          "Kalshi 余额变动:%s → %s(当前无实盘授权,余额应为直线;"
                          "疑似未授权活动或账户异常)" % (prev, val)))
        st["value"] = val
    tg.write_json("balance_state.json", st)


def collect():
    """Return list of (tier, key, message) currently-firing conditions."""
    fired = []
    # test hook (acceptance §④): synthetic yellow gap, never touches real data
    if os.path.exists(tg.state_path("TEST_GAP")):
        fired.append((tg.YELLOW, "test_gap",
                      "【测试】人为注入的 capture gap(验收标志,未碰真实采集数据)"))
    # capture / feed
    cap = _load_live_json("capture_alert.json") or {}
    if cap.get("status") and cap.get("status") != "ok":
        fired.append((tg.RED, "capture", "采集异常:capture status=%s" % cap["status"]))
    stale = _feed_stale_s()
    if stale is not None and stale > _f("FEED_STALE_S", 120):
        fired.append((tg.RED, "feed", "采集数据流停滞:最新 raw %d 秒前" % int(stale)))
    if _ingest_alive() is False:
        fired.append((tg.RED, "ingest", "入库 daemon 不在(pgrep 未找到)"))
    # disk
    disk = _disk_pct()
    if disk is not None and disk > _f("DISK_RED_PCT", 85):
        fired.append((tg.RED, "disk", "生产 EC2 磁盘 %d%% > %d%%" % (disk, int(_f("DISK_RED_PCT", 85)))))
    # clock
    off = _chrony_offset()
    if off is not None and off > _f("CHRONY_OFFSET_RED_S", 0.5):
        fired.append((tg.RED, "clock", "时钟失步:chrony offset %.3fs" % off))
    # seal alarm (export/seal failed past the alarm line)
    alarm = _load_live_json("seal_alarm.json")
    if alarm:
        fired.append((tg.RED, "seal", "封存告警:%s(%s)"
                      % (alarm.get("kind", "?"), alarm.get("date", "?"))))
    # capture gap (yellow) — capture_alert carries silent_secs even when recovered
    if cap.get("status") == "gap":
        fired.append((tg.YELLOW, "capture_gap",
                      "capture gap:静默 %.0fs" % (cap.get("silent_secs", 0) or 0)))
    # balance tripwire (own cadence)
    check_balance_tripwire(fired)
    return fired


def main():
    fired = collect()
    now = int(time.time())
    realert = int(_f("REALERT_MIN", 30)) * 60
    seen = tg.read_json("alert_state.json", {}) or {}
    active_keys = set()
    for tier, key, msg in fired:
        active_keys.add(key)
        prev = seen.get(key)
        due = (prev is None) or (now - prev.get("last_sent", 0) >= realert)
        if due:
            if tg.send("%s %s" % (tier, msg)):
                seen[key] = {"tier": tier, "last_sent": now,
                             "since": (prev or {}).get("since", now)}
    # recovery notices for conditions that cleared
    for key in list(seen.keys()):
        if key not in active_keys:
            info = seen.pop(key)
            dur_min = max(0, (now - info.get("since", now)) // 60)
            tg.send("✅ 已恢复:%s(持续约 %d 分钟)" % (key, dur_min))
    tg.write_json("alert_state.json", seen)


if __name__ == "__main__":
    main()
