#!/usr/bin/env python3
"""W-TELEGRAM-01 §3 watchdog — the monitor of the monitor.

Runs on a DIFFERENT machine (the Mac, via launchd) from the box's alert/daily
timers, so the death of the box's monitoring is still reported. Every run it
asks: was a daily summary delivered in the last SUMMARY_STALE_RED_H hours?
It reads the box's receipt over SSH; if SSH fails (box unreachable) OR the
receipt is stale/absent, it sends a RED alert straight from the Mac via the
backup channel (same bot, independent host).

Credentials are read from the Mac's ~/.kalshi/env.sh (operator-written, iron
rule ②). stdlib only; no pipeline imports.
"""
import calendar
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
BOX = os.environ.get("KALSHI_EC2", "ubuntu@3.130.232.109")
KEY = os.path.expanduser(os.environ.get("KALSHI_SSH_KEY", "~/.ssh/kalshi-key.pem"))
STALE_H = float(os.environ.get("SUMMARY_STALE_RED_H", "26"))
RECEIPT = "~/hft-bot/work/live/monitor/summary_receipt.json"
LOGDIR = os.path.expanduser("~/.kalshi")
LOG = os.path.join(LOGDIR, "tg_watchdog.log")


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg))
    except OSError:
        pass


def send(text):
    if not TOKEN or not CHAT:
        log("SEND SKIPPED (creds unset)")
        return False
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text}).encode()
    url = "https://api.telegram.org/bot%s/sendMessage" % TOKEN
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
                if r.status == 200:
                    return True
        except Exception as e:
            log("send attempt %d failed: %s" % (i + 1, str(e)[:80]))
            time.sleep(2)
    return False


def fetch_receipt_age_h():
    """Return (age_hours, reason). age None => could not confirm a fresh summary."""
    try:
        out = subprocess.run(
            ["ssh", "-i", KEY, "-o", "ConnectTimeout=15", "-o", "BatchMode=yes",
             BOX, "cat %s 2>/dev/null" % RECEIPT],
            capture_output=True, text=True, timeout=25)
    except Exception as e:
        return None, "SSH 到生产盒子失败:%s" % str(e)[:80]
    if out.returncode != 0 or not out.stdout.strip():
        return None, "读不到日报回执(盒子不可达或从未发过日报)"
    try:
        r = json.loads(out.stdout)
        sent = r.get("sent_utc")
        t = calendar.timegm(time.strptime(sent, "%Y-%m-%dT%H:%M:%SZ"))  # struct is UTC
        return (time.time() - t) / 3600.0, None
    except Exception as e:
        return None, "回执解析失败:%s" % str(e)[:60]


def main():
    age, reason = fetch_receipt_age_h()
    if age is None:
        send("🔴 【watchdog】%s —— 监控系统本身可能已死。请立即人工检查生产盒子与报警服务。" % reason)
        log("RED sent: %s" % reason)
    elif age > STALE_H:
        send("🔴 【watchdog】日报已 %.1f 小时未送达(阈值 %.0fh)—— 报警系统可能挂了,请人工检查。"
             % (age, STALE_H))
        log("RED sent: stale %.1fh" % age)
    else:
        log("OK: last summary %.1fh ago" % age)


if __name__ == "__main__":
    main()
