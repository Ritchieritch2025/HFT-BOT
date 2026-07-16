#!/usr/bin/env python3
"""W-TELEGRAM-01 §4-ter upstream change intelligence (EDC defense line).

Two sources (operator-specified, live-verified 2026-07-16):
  1. docs.kalshi.com/changelog  — TECHNICAL, primary watch. Daily snapshot
     diff; NEW entries -> YELLOW push + appended to
     docs/EXCHANGE_CHANGE_CALENDAR.md (effective date + impact-surface note).
  2. news.kalshi.com/t/announcements — BUSINESS, filtered. Keyword hits
     (config ANNOUNCE_KEYWORDS) -> INFO push; everything else ignored to
     protect signal-to-noise.

Runs daily via kalshi-tg-intel.timer. READ-ONLY over the web; writes only its
own snapshot state + the change calendar doc. stdlib only.
"""
import html
import os
import re
import urllib.request

import tg_common as tg

CONF = tg.load_conf()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _text_lines(page):
    """Crude HTML->text: drop scripts/styles/tags, unescape, keep substantial
    lines. Good enough for set-diffing a changelog."""
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", "\n", page)
    page = html.unescape(page)
    lines = []
    for ln in page.splitlines():
        ln = ln.strip()
        if len(ln) >= 12 and not ln.startswith("{") and "function(" not in ln:
            lines.append(ln)
    return lines


def check_changelog():
    url = CONF.get("CHANGELOG_URL", "https://docs.kalshi.com/changelog")
    try:
        lines = _text_lines(_fetch(url))
    except Exception as e:
        tg.log("changelog fetch error: %s" % str(e)[:80])
        return
    snap = tg.read_json("changelog_snapshot.json", None)
    cur = set(lines)
    if snap is None:
        tg.write_json("changelog_snapshot.json", {"lines": sorted(cur)})
        tg.log("changelog baseline stored (%d lines)" % len(cur))
        return
    prev = set(snap.get("lines", []))
    new = [ln for ln in lines if ln not in prev]
    # keep only entry-like new lines (contain a date or a version/word signal)
    interesting = [ln for ln in new
                   if re.search(r"\d{4}|v\d|deprecat|add|remov|change|new|updat|fee|field|endpoint",
                                ln, re.I)][:8]
    if interesting:
        body = "\n".join("• " + ln[:160] for ln in interesting)
        tg.send("%s 交易所 changelog 新条目(docs.kalshi.com,技术源,重点盯防):\n%s\n"
                "→ 已记入 EXCHANGE_CHANGE_CALENDAR.md,请评估是否碰我们的采集频道/字段/费率"
                % (tg.YELLOW, body))
        _append_calendar(interesting)
    tg.write_json("changelog_snapshot.json", {"lines": sorted(cur)})


def _append_calendar(entries):
    cal = os.path.join(tg.ROOT, "docs", "EXCHANGE_CHANGE_CALENDAR.md")
    now = tg._now()
    header_needed = not os.path.exists(cal)
    with open(cal, "a") as f:
        if header_needed:
            f.write("# EXCHANGE CHANGE CALENDAR (W-TELEGRAM-01 §4-ter)\n\n"
                    "自动追加:docs.kalshi.com/changelog 新条目。列生效日期 + 影响面初评。\n"
                    "影响面初评需人工填(碰不碰我们的采集频道/字段/费率)。\n\n")
        f.write("## 抓取于 %s\n" % now)
        for e in entries:
            m = re.search(r"(\d{4}-\d{2}-\d{2})", e)
            eff = m.group(1) if m else "(生效日期待定)"
            f.write("- [ ] %s — %s  · 影响面:待评估\n" % (eff, e[:200]))
        f.write("\n")


def check_announcements():
    url = CONF.get("ANNOUNCE_URL", "https://news.kalshi.com/t/announcements")
    kws = [k.strip().lower() for k in CONF.get("ANNOUNCE_KEYWORDS", "").split(",") if k.strip()]
    try:
        lines = _text_lines(_fetch(url))
    except Exception as e:
        tg.log("announcements fetch error: %s" % str(e)[:80])
        return
    seen = set((tg.read_json("announce_seen.json", {}) or {}).get("hashes", []))
    hits = []
    for ln in lines:
        low = ln.lower()
        if any(k in low for k in kws):
            h = str(hash(ln))
            if h not in seen:
                hits.append(ln)
                seen.add(h)
    for ln in hits[:6]:
        tg.send("%s 交易所公告命中关键词(news.kalshi.com):\n%s" % (tg.INFO, ln[:220]))
    tg.write_json("announce_seen.json", {"hashes": sorted(seen)[-500:]})


def main():
    check_changelog()
    check_announcements()
    print("intel sweep done")


if __name__ == "__main__":
    main()
