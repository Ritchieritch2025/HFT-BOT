#!/usr/bin/env python3
"""Kalshi pipeline status bot — read-only Telegram monitor.

Long-polls Telegram getUpdates and answers status queries from the pipeline's
on-disk state (work/live/*.json, the seal directory, process table, disk).
STRICTLY READ-ONLY: it never restarts, seals, deletes, orders, or mutates
anything. It only reads state and reports. Commands that would change system
state are intentionally absent — this is a monitor, not a control plane.

Runs on the EC2 box next to the pipeline. Credentials come from
~/.kalshi/env.sh (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, operator-created).
Only the allowlisted chat id (TELEGRAM_CHAT_ID) is answered; anything else is
ignored. stdlib only (urllib), matching the house SigV4 style.
"""
import datetime
import glob
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

ROOT = os.environ.get("KALSHI_ROOT", os.path.expanduser("~/hft-bot"))
LIVE = os.path.join(ROOT, "work", "live")
SEALS = os.path.join(ROOT, "work", "warehouse", "seals")
RAW = os.path.join(ROOT, "work", "raw")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
API = "https://api.telegram.org/bot%s/" % TOKEN


def _tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(API + method, data=data, timeout=65) as r:
            return json.load(r)
    except Exception as e:
        print("tg %s error: %s" % (method, e), flush=True)
        return {}


def send(text):
    _tg("sendMessage", chat_id=CHAT, text=text, disable_web_page_preview=1)


def _load(name):
    try:
        return json.load(open(os.path.join(LIVE, name)))
    except Exception:
        return None


def _ingest_alive():
    try:
        out = subprocess.run(["pgrep", "-f", "ingest.py --loop"],
                             capture_output=True, text=True, timeout=10)
        pids = [p for p in out.stdout.split() if p]
        return pids[0] if pids else None
    except Exception:
        return None


def _feed_fresh_secs():
    """Age (s) of the newest raw file, or None if raw dir missing/empty."""
    newest = 0.0
    for p in glob.glob(os.path.join(RAW, "date=*", "*")):
        try:
            m = os.path.getmtime(p)
            if m > newest:
                newest = m
        except OSError:
            pass
    if not newest:
        return None
    return time.time() - newest


def _sealed_days(n=6):
    days = sorted(os.path.basename(f)[5:-5]
                  for f in glob.glob(os.path.join(SEALS, "date=*.json")))
    return days[-n:]


def _disk_pct():
    try:
        out = subprocess.run(["df", "/"], capture_output=True, text=True, timeout=10)
        return int(out.stdout.splitlines()[1].split()[4].rstrip("%"))
    except Exception:
        return None


def cmd_status():
    cap = _load("capture_alert.json") or {}
    cap_ok = cap.get("status") == "ok"
    pid = _ingest_alive()
    fresh = _feed_fresh_secs()
    disk = _disk_pct()
    days = _sealed_days()
    latest_seal = days[-1] if days else "none"
    alarm = _load("seal_alarm.json")
    bad = []
    if not cap_ok:
        bad.append("capture=%s" % cap.get("status", "?"))
    if not pid:
        bad.append("ingest DOWN")
    if fresh is not None and fresh > 120:
        bad.append("feed stale %ds" % int(fresh))
    if disk is not None and disk > 80:
        bad.append("disk %d%%" % disk)
    if alarm:
        bad.append("seal_alarm:%s" % alarm.get("kind", "?"))
    verdict = "✅ 全绿" if not bad else "⚠️ " + " · ".join(bad)
    lines = [
        "%s" % verdict,
        "",
        "capture: %s" % cap.get("status", "?"),
        "ingest: %s" % ("pid %s" % pid if pid else "DOWN"),
        "feed: %s" % ("%.0fs ago" % fresh if fresh is not None else "n/a"),
        "disk /: %s" % ("%d%%" % disk if disk is not None else "n/a"),
        "latest seal: %s" % latest_seal,
        "",
        "%s UTC" % datetime.datetime.now(datetime.timezone.utc).strftime("%m-%d %H:%M"),
    ]
    return "\n".join(lines)


def cmd_seals():
    days = _sealed_days(8)
    if not days:
        return "no seals found"
    today = datetime.datetime.now(datetime.timezone.utc).date()
    span = [(today - datetime.timedelta(days=i)).isoformat() for i in range(1, 8)]
    rows = ["封印状态(近 7 日,不含今天):"]
    for d in reversed(span):
        rows.append("%s  %s" % (d, "✅ sealed" if d in days else "❌ 未封印"))
    return "\n".join(rows)


def cmd_pipeline():
    pid = _ingest_alive()
    cap = _load("capture_alert.json") or {}
    fresh = _feed_fresh_secs()
    ret = _load("raw_retention_alert.json") or {}
    lines = [
        "入库 daemon: %s" % ("pid %s (alive)" % pid if pid else "DOWN"),
        "capture: %s (silent %.1fs)" % (cap.get("status", "?"),
                                        cap.get("silent_secs", 0) or 0),
        "feed 最新: %s" % ("%.0fs ago" % fresh if fresh is not None else "n/a"),
        "raw 保留: cutoff %s, deleted %s" % (ret.get("cutoff", "?"),
                                             ret.get("deleted", "?")),
    ]
    return "\n".join(lines)


def cmd_disk():
    try:
        out = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=10)
        return "```\n%s\n```" % out.stdout.strip()
    except Exception as e:
        return "disk read error: %s" % e


HELP = (
    "Kalshi 管道监控 bot(只读)\n"
    "/status — 一屏健康裁决\n"
    "/seals — 近 7 日封印状态\n"
    "/pipeline — 入库/capture/feed 细节\n"
    "/disk — 磁盘用量\n"
    "/help — 本帮助\n"
    "\n只读监控,不含任何控制/下单命令。"
)

HANDLERS = {
    "/status": cmd_status, "/seals": cmd_seals, "/pipeline": cmd_pipeline,
    "/disk": cmd_disk, "/help": lambda: HELP, "/start": lambda: HELP,
}


def main():
    if not TOKEN or not CHAT:
        raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset "
                         "(source ~/.kalshi/env.sh)")
    print("status bot up; allowlisted chat=%s" % CHAT, flush=True)
    offset = None
    # skip backlog: start from the latest pending update
    init = _tg("getUpdates", timeout=0)
    if init.get("ok") and init.get("result"):
        offset = init["result"][-1]["update_id"] + 1
    while True:
        resp = _tg("getUpdates", timeout=50, offset=offset)
        if not resp.get("ok"):
            time.sleep(3)
            continue
        for u in resp.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or u.get("edited_message") or {}
            chat = str((m.get("chat") or {}).get("id", ""))
            text = (m.get("text") or "").strip()
            if chat != str(CHAT):
                continue  # allowlist: ignore everyone else
            cmd = text.split()[0].lower() if text else ""
            cmd = cmd.split("@")[0]  # strip @botname suffix
            fn = HANDLERS.get(cmd)
            try:
                send(fn() if fn else "未知命令。/help 看可用命令。")
            except Exception as e:
                send("命令出错: %s" % e)


if __name__ == "__main__":
    main()
