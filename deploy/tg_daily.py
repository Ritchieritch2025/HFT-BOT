#!/usr/bin/env python3
"""W-TELEGRAM-01 §2 daily health summary. One message per day at
SUMMARY_HOUR_UTC via kalshi-tg-daily.timer. One-line verdict first (operator
report contract), then the fixed field block. Records a receipt
(monitor/summary_receipt.json) that the §3 watchdog checks. READ-ONLY.
"""
import datetime
import glob
import os
import subprocess

import tg_common as tg

CONF = tg.load_conf()
SEALS = os.path.join(tg.ROOT, "work", "warehouse", "seals")


def _sealed_dates():
    return set(os.path.basename(f)[5:-5]
               for f in glob.glob(os.path.join(SEALS, "date=*.json")))


def _disk_pct(path="/"):
    try:
        out = subprocess.run(["df", path], capture_output=True, text=True, timeout=10)
        return int(out.stdout.splitlines()[1].split()[4].rstrip("%"))
    except Exception:
        return None


def _seal_rows(date):
    import json
    try:
        s = json.load(open(os.path.join(SEALS, "date=%s.json" % date)))
        return s.get("archive_rows") or s.get("row_count") or s.get("rows")
    except Exception:
        return None


def _research_qualified(sealed):
    """deep03 countdown numerator: distinct sealed dates from research-publishing
    onset (2026-07-12) forward. Proxy for qualified independent dates — labelled
    as such; the RFQ-variant nuance is a research-side judgment, not claimed here."""
    return sorted(d for d in sealed if d >= "2026-07-12")


def _s3_storage_gb():
    try:
        out = subprocess.run(
            ["bash", "-lc",
             ". ~/.kalshi/env.sh 2>/dev/null; "
             "aws s3 ls s3://kalshi-vault-ritcardo/research/ --recursive --summarize "
             "2>/dev/null | awk '/Total Size/{print $3}'"],
            capture_output=True, text=True, timeout=60)
        b = float(out.stdout.strip() or 0)
        return b / (1024 ** 3)
    except Exception:
        return None


def _change_count():
    cal = os.path.join(tg.ROOT, "docs", "EXCHANGE_CHANGE_CALENDAR.md")
    if not os.path.exists(cal):
        return 0
    today = datetime.datetime.now(datetime.timezone.utc).date()
    horizon = today + datetime.timedelta(
        days=int(float(CONF.get("CHANGE_CALENDAR_LOOKAHEAD_DAYS", 14))))
    n = 0
    import re
    for ln in open(cal):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", ln)
        if m:
            try:
                d = datetime.date.fromisoformat(m.group(1))
                if today <= d <= horizon:
                    n += 1
            except ValueError:
                pass
    return n


def build():
    now = datetime.datetime.now(datetime.timezone.utc)
    yday = (now.date() - datetime.timedelta(days=1)).isoformat()
    sealed = _sealed_dates()
    seal_ok = yday in sealed
    disk = _disk_pct()
    qual = _research_qualified(sealed)
    target = int(float(CONF.get("RESEARCH_DATE_TARGET", 20)))
    countdown = max(0, target - len(qual))
    # balance ± vs yesterday (from the tripwire state)
    bstate = tg.read_json("balance_state.json", {}) or {}
    bal = bstate.get("value", "n/a")
    dstate = tg.read_json("daily_last.json", {}) or {}
    prev_bal = dstate.get("balance")
    if prev_bal is not None and bal != "n/a":
        delta = "±0.00 ✅" if str(bal) == str(prev_bal) else "变动 %s→%s ⚠️" % (prev_bal, bal)
    else:
        delta = "(基线)"
    gb = _s3_storage_gb()
    store_cost = (gb * 0.023) if gb is not None else None
    changes = _change_count()
    rows = _seal_rows(yday)

    # verdict
    bad = (not seal_ok) or (disk is not None and disk > int(float(CONF.get("DISK_RED_PCT", 85))))
    warn = (prev_bal is not None and str(bal) != str(prev_bal))
    verdict = "❌" if bad else ("⚠️" if warn else "✅")

    lines = [
        "%s %s 管道日报" % (verdict, yday),
        "采集: %s" % ("%s 行(封存计)" % (f"{rows:,}" if rows else "?") if seal_ok
                     else "昨日未封存"),
        "封存: 昨日 %s" % ("PASS" if seal_ok else "PENDING/未落"),
        "研究桶: %d/%d 已封存可研究日期(deep03 倒计时 ~%d 天;合格口径以研究会话盘点为准)" % (len(qual), target, countdown),
        "余额: $%s(较昨日 %s)" % (bal, delta),
        "磁盘: EC2 %s · 费用月累计: ~$%s" % (
            ("%d%%" % disk) if disk is not None else "n/a",
            ("%.2f" % store_cost) if store_cost is not None else "n/a"),
        "未来 14 天已知交易所变更: %d 条" % changes,
    ]
    return "\n".join(lines), {"balance": bal, "sent_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ")}


def main():
    text, receipt = build()
    ok = tg.send(text)
    receipt["delivered"] = ok
    tg.write_json("daily_last.json", receipt)
    if ok:
        tg.write_json("summary_receipt.json",
                      {"sent_utc": receipt["sent_utc"], "delivered": True})
    print("daily summary %s" % ("sent" if ok else "SEND FAILED"))


if __name__ == "__main__":
    main()
