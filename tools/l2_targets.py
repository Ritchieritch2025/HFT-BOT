#!/usr/bin/env python3
"""l2_targets — hourly targeted-L2 subscription selector (PIPE-W06 Stage 1).

Builds `work/live/l2_targets.csv`: the explicit market list the supervisor's
LAYER 1b (a second read-only ws_shadow, `orderbook_delta`) subscribes to.
Evolved from the Stage-0 generator (sandbox/l2_probe_targets_gen.py); field
semantics verified live 2026-07-11 (E4): status value is 'active'; the real
event time is `occurrence_datetime` (`close_time` is a padded fallback days
later); the activity proxy is `open_interest_fp` (volume is absent in list
responses). The `liquidity_tier` column carries the SPORT/GROUP so downstream
per-tier analysis (depth_probe --analyze) reads as per-sport rates — same
documented CSV contract as Stage 0.

Universe (spec §2, operator-approved): four majors Tennis / Baseball(MLB) /
Soccer / Basketball(WNBA) + controls (Esports / Golf / BTC15m), quotas
summing to the N=50 cap (Stage-0 quotas kept verbatim; controls ≤20% quota,
~15% realized). Pre-game window preference: markets whose occurrence falls
within --pregame-hours rank ABOVE the rest of the --window-hours horizon;
within a band open_interest desc, sooner first. CTRL_BTC15m instead picks
the soonest 15-minute windows (they roll continuously; OI is meaningless).

Fail-closed contract (spec §2):
  * The CSV is written ATOMICALLY (tmp + os.replace) and ONLY on a fully
    successful selection with >0 rows. A failed or empty refresh exits
    nonzero and leaves the previous file byte-untouched, so it ages out.
  * `--check --max-age-secs N` is the supervisor's start gate: it prints the
    comma-joined ticker list on stdout and exits 0 ONLY if the CSV exists,
    is younger than N seconds and carries >=1 well-formed ticker row.
    Stale / missing / empty / unreadable => nonzero => LAYER 1b must not
    start (the firehose is never affected either way).

Public read-only REST, no credentials, 0.25 s pacing + 429 exponential
backoff (the catalog_sync pacing lesson). stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://external-api.kalshi.com/trade-api/v2"
OUT_DEFAULT = os.path.join("work", "live", "l2_targets.csv")
FIELDNAMES = ["market_ticker", "liquidity_tier", "open_interest",
              "occurrence", "series", "generated_at"]
# Same shape gate as ingest's boundary validation (D3): a spliced/garbage
# ticker must never reach a subscribe command or an env var.
TICKER_RE = re.compile(r"^[A-Z0-9._-]{3,80}$")

# Per-group quotas sum to exactly the N=50 Stage-1 cap. Kept VERBATIM from
# the operator-approved Stage-0 list (PIPE-W06 spec §2 + approval receipt):
# majors 12/12/8/8 = 40, controls 4/3/3 = 10 (<=20% quota, ~15% realized —
# BTC15m rarely has 3 windows inside the horizon).
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
N_CAP_DEFAULT = 50
WINDOW_HOURS_DEFAULT = 36
PREGAME_HOURS_DEFAULT = 24

_last_call = [0.0]


def fetch_json(path, params):
    """Paced public GET with 429 backoff. Module-level so tests can patch it."""
    wait = _last_call[0] + 0.25 - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
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
    except (ValueError, TypeError):
        return None


def collect_group(series_list, now, window_h, fetch=None):
    """Active markets with a plausible occurrence inside the horizon.

    Returns (candidates, fetch_errors) where each candidate is
    (open_interest, occurrence_dt, ticker, series). Per-series fetch failures
    are counted, not fatal — the caller decides whether the overall run still
    produced a usable selection.
    """
    fetch = fetch or fetch_json
    cands, errors = [], 0
    for s in series_list:
        cursor, pages = None, 0
        while pages < 8:
            params = {"series_ticker": s, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            try:
                d = fetch("/markets", params)
            except Exception as e:
                print("WARN: %s fetch failed: %s" % (s, e), file=sys.stderr)
                errors += 1
                break
            for m in d.get("markets", []):
                if m.get("status") != "active":
                    continue
                tick = m.get("ticker", "")
                if not TICKER_RE.match(str(tick)) or "-" not in tick:
                    continue  # boundary validation (D3): never subscribe garbage
                occ = parse_ts(m.get("occurrence_datetime")) or \
                    parse_ts(m.get("expected_expiration_time"))
                if occ is None or not (now < occ <= now + timedelta(hours=window_h)):
                    continue
                try:
                    oi = float(m.get("open_interest_fp") or 0)
                except (ValueError, TypeError):
                    oi = 0.0
                cands.append((oi, occ, tick, s))
            cursor = d.get("cursor")
            pages += 1
            if not cursor or not d.get("markets"):
                break
    return cands, errors


def select_targets(now, plan=PLAN, window_h=WINDOW_HOURS_DEFAULT,
                   pregame_h=PREGAME_HOURS_DEFAULT, cap=N_CAP_DEFAULT,
                   fetch=None):
    """Rank + pick per-group quotas, dedupe, enforce the global N cap.

    Returns (rows, report_lines, fetch_errors). rows use FIELDNAMES minus
    generated_at (stamped at write time).
    """
    rows, report, seen, total_errors = [], [], set(), 0
    pregame_edge = now + timedelta(hours=pregame_h)
    for group, quota, series_list in plan:
        cands, errors = collect_group(series_list, now, window_h, fetch=fetch)
        total_errors += errors
        if group == "CTRL_BTC15m":
            cands.sort(key=lambda c: c[1])            # soonest 15m windows
        else:
            # pre-game preference first, then OI desc, then sooner first
            cands.sort(key=lambda c: (0 if c[1] <= pregame_edge else 1,
                                      -c[0], c[1]))
        picked = 0
        for oi, occ, tick, s in cands:
            if picked >= quota or len(rows) >= cap:
                break
            if tick in seen:
                continue
            seen.add(tick)
            picked += 1
            rows.append({"market_ticker": tick, "liquidity_tier": group,
                         "open_interest": oi, "occurrence": occ.isoformat(),
                         "series": s})
        report.append("%-12s quota=%2d active_in_%dh=%4d picked=%2d"
                      % (group, quota, window_h, len(cands), picked))
    return rows, report, total_errors


def write_atomic(rows, out_path, now):
    """tmp + fsync + os.replace: the reader (supervisor gate / l2_shadow) can
    never observe a partial file, and a crash never destroys the previous
    good list."""
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    d = os.path.dirname(out_path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = out_path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow(dict(r, generated_at=stamp))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def check(out_path, max_age_secs, now_s=None):
    """Supervisor start gate. Returns the ticker list; raises on any refusal
    (missing/stale/empty/unreadable — all fail-closed)."""
    now_s = time.time() if now_s is None else now_s
    if not os.path.exists(out_path):
        raise RuntimeError("targets file missing: %s" % out_path)
    age = now_s - os.path.getmtime(out_path)
    if age > max_age_secs:
        raise RuntimeError("targets file stale: %s is %.0fs old (max %ds)"
                           % (out_path, age, max_age_secs))
    tickers, skipped = [], 0
    with open(out_path, newline="") as f:
        for row in csv.DictReader(f):
            t = (row.get("market_ticker") or "").strip()
            if TICKER_RE.match(t) and "-" in t:
                tickers.append(t)
            elif t or any((v or "").strip() for v in row.values()):
                skipped += 1
    if skipped:
        print("WARN: %d malformed row(s) skipped in %s" % (skipped, out_path),
              file=sys.stderr)
    if not tickers:
        raise RuntimeError("targets file has no well-formed tickers: %s" % out_path)
    return tickers


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default=os.path.join(ROOT, OUT_DEFAULT),
                   help="target CSV path (default work/live/l2_targets.csv)")
    p.add_argument("--n", type=int, default=N_CAP_DEFAULT,
                   help="global market cap (Stage 1 = 50)")
    p.add_argument("--window-hours", type=int, default=WINDOW_HOURS_DEFAULT,
                   help="candidate horizon: occurrence within this many hours")
    p.add_argument("--pregame-hours", type=int, default=PREGAME_HOURS_DEFAULT,
                   help="pre-game preference band (spec §2: prefer <=24h to start)")
    p.add_argument("--check", action="store_true",
                   help="gate mode: no REST; validate freshness and print the "
                        "comma-joined ticker list (nonzero exit = do NOT start L2)")
    p.add_argument("--max-age-secs", type=int, default=7200,
                   help="--check: maximum CSV age before it counts as stale")
    args = p.parse_args(argv)

    if args.check:
        try:
            tickers = check(args.out, args.max_age_secs)
        except (RuntimeError, OSError, csv.Error) as e:
            print("l2_targets CHECK FAIL: %s" % e, file=sys.stderr)
            return 3
        print(",".join(tickers))
        return 0

    now = datetime.now(timezone.utc)
    try:
        rows, report, errors = select_targets(
            now, window_h=args.window_hours, pregame_h=args.pregame_hours,
            cap=args.n)
    except Exception as e:  # total failure: previous file stays untouched
        print("l2_targets FAIL: selection aborted: %s" % e, file=sys.stderr)
        return 1
    print("\n".join(report))
    if not rows:
        print("l2_targets FAIL: 0 candidates selected (%d fetch error(s)); "
              "previous file left untouched (fail-closed)" % errors,
              file=sys.stderr)
        return 1
    write_atomic(rows, args.out, now)
    print("TOTAL=%d (cap %d, %d fetch error(s)) -> %s (generated %sZ)"
          % (len(rows), args.n, errors, args.out,
             now.strftime("%Y-%m-%d %H:%M")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
