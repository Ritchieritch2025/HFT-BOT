#!/usr/bin/env python3
"""Build TODAY's L2 probe target CSV (PIPE-W06 Stage 0, operator-approved).

Public read-only endpoints only (same BASE + pacing lesson as catalog_sync).
Field semantics verified live 2026-07-11 (E4): status value is 'active';
real event time = occurrence_datetime (close_time is a padded fallback days
later); activity proxy = open_interest_fp (volume is absent in list responses).
liquidity_tier column carries the SPORT/GROUP so depth_probe's per-tier table
reads as per-sport rates (documented CSV contract, zero tool change).
"""
import csv, json, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://external-api.kalshi.com/trade-api/v2"
NOW = datetime.now(timezone.utc)
WINDOW_H = 36

PLAN = [
    ("Tennis",     12, ["KXITFMATCH", "KXITFWMATCH", "KXATPCHALLENGERMATCH",
                        "KXWTACHALLENGERMATCH", "KXATPMATCH", "KXWTAMATCH"]),
    ("Baseball",   12, ["KXMLBGAME", "KXMLBTOTAL", "KXMLBSPREAD"]),
    ("Soccer",      8, ["KXWCGOAL", "KXWCSCORE", "KXMENWORLDCUP", "KXWCADVANCE",
                        "KXUCLGAME", "KXBRASILEIROBGAME"]),
    ("Basketball",  8, ["KXWNBAGAME", "KXWNBASPREAD", "KXWNBATOTAL"]),
    ("CTRL_Esports", 4, ["KXCS2GAME", "KXLOLGAME", "KXVALORANTGAME"]),
    ("CTRL_Golf",    3, ["KXPGATOUR"]),
    ("CTRL_BTC15m",  3, ["KXBTC15M"]),
]

_last = [0.0]
def get(path, params):
    wait = _last[0] + 0.25 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.monotonic()
    url = "%s%s?%s" % (BASE, path, urllib.parse.urlencode(params))
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("429 retries exhausted: " + url)

def parse_ts(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None

rows, report = [], []
for group, quota, series_list in PLAN:
    cands = []
    for s in series_list:
        cursor, pages = None, 0
        while pages < 8:
            params = {"series_ticker": s, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                d = get("/markets", params)
            except Exception as e:
                print("WARN: %s fetch failed: %s" % (s, e), file=sys.stderr)
                break
            for m in d.get("markets", []):
                if m.get("status") != "active":
                    continue
                occ = parse_ts(m.get("occurrence_datetime")) or \
                      parse_ts(m.get("expected_expiration_time"))
                if occ is None or not (NOW < occ <= NOW + timedelta(hours=WINDOW_H)):
                    continue
                try:
                    oi = float(m.get("open_interest_fp") or 0)
                except ValueError:
                    oi = 0.0
                cands.append((oi, occ.isoformat(), m["ticker"], s))
            cursor = d.get("cursor")
            pages += 1
            if not cursor or not d.get("markets"):
                break
    if group == "CTRL_BTC15m":
        cands.sort(key=lambda x: x[1])          # soonest 15m windows
    else:
        cands.sort(key=lambda x: (-x[0], x[1]))  # OI desc, sooner first
    picked = cands[:quota]
    report.append("%-12s wanted=%2d active_in_%dh=%4d picked=%2d  top_oi=%s"
                  % (group, quota, WINDOW_H, len(cands), len(picked),
                     picked[0][0] if picked else "-"))
    for oi, occ, tick, s in picked:
        rows.append({"market_ticker": tick, "liquidity_tier": group,
                     "open_interest": oi, "occurrence": occ, "series": s})

out = sys.argv[1] if len(sys.argv) > 1 else "l2_probe_targets.csv"
with open(out, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["market_ticker", "liquidity_tier",
                                      "open_interest", "occurrence", "series"])
    w.writeheader()
    w.writerows(rows)
print("\n".join(report))
print("TOTAL=%d -> %s (generated %sZ)" % (len(rows), out,
                                          NOW.strftime("%Y-%m-%d %H:%M")))
