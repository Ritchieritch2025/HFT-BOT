#!/usr/bin/env python3
"""WP-08 Daily Quality Check — the two-minute morning ritual as ONE command.

Prints YESTERDAY's health on one screen (R runs and READS it daily, H-2:
the habit is human; the fetching is code):
  [1] FRESHNESS  — consumed from WP-03 via `python3 tools/freshness.py
      --json` in a subprocess, NEVER reimplemented (one alarm, one owner).
      Test seam: --freshness-json <file> injects the producer's exact JSON
      (documented in tests/test_daily_check.py). `stale_reasons` are carried
      through verbatim so a consumer can distinguish "pipeline behind" from
      "cannot even read the pipeline" (WP-03 BACKLOG note) — never rebuilt
      from the exit code.
  [2] EXPORT     — yesterday's manifest.csv rows: per-table row/file counts,
      an md5 spot check of sampled archive files (a mismatch or missing file
      is a HARD failure — the archive is write-once truth, D1/S2), and
      category row-count DROP detection vs the prior day's manifest rows:
      a trades/orderbooks_l1 category whose rows fell more than --drop-pct
      (default 50%) is a WARNING — report-only, prints loudly, never exit 1
      (volume moves with the sports calendar; a drop is a human question,
      not a machine verdict). No prior-day rows => honestly "skipped".
  [3] GOLD       — day status if built: hard fail when the day sits in
      <gold-root>/quarantine/date=<D>[.N] OR its validation report verdict
      is QUARANTINE; GREEN passes; not built is reported, not failed
      (gold is derived + rebuildable; the raw/archive checks above own the
      data-loss alarm). A quarantine/ subdir INSIDE a green day dir is the
      loader's row-level forensic dump (known, quality_log'd) => note only.
  [4] SEQ GAPS   — tail of the live ws_shadow log: gap/resync marker lines
      plus the newest cumulative counters (reconnects/errors/overflow/drop;
      nonzero => WARNING). Counters are cumulative since daemon start and
      the log carries no timestamps, so per-day attribution is reported as
      not instrumented — honestly (D2), never dressed up as a per-day zero.
  [5] QUALITY LOG— entries from the last 24h, shown for human review.
  [6] APPEND     — one schema line {ts, wp: "daily-check", window: <date>,
      finding: GREEN | WARNINGS:n | RED:n, action: "reviewed", evidence:
      summary counts} to work/quality_log.ndjson (override: --quality-log /
      DAILY_CHECK_QUALITY_LOG env — tests never touch the real log).
      RED:n extends the plan's <GREEN|WARNINGS:n> enum: logging GREEN next
      to exit 1 would be exactly the green-lie D2 forbids.

Exit 0 when nothing red (warnings print loudly but do not fail); exit 1 on
any hard failure: freshness STALE, manifest missing for a completed day,
md5/spot-check integrity failure, gold day quarantined. --json for machines.
Registry pass token: "DAILY GREEN" (exit code authoritative, as freshness).
Read-only everywhere except the quality-log append. stdlib only.
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_MANIFEST = os.path.join(ROOT, "work", "warehouse", "manifest.csv")
DEFAULT_GOLD_ROOT = os.path.join(ROOT, "work", "gold")
DEFAULT_WS_LOG = os.path.join(ROOT, "work", "live", "ws_shadow.log")
DEFAULT_QUALITY_LOG = os.path.join(ROOT, "work", "quality_log.ndjson")
FRESHNESS_TOOL = os.path.join(ROOT, "tools", "freshness.py")

DROP_TABLES = ("trades", "orderbooks_l1")   # the drop detector's scope
MD5_SPOT_N = 5                              # archive files sampled per day
WS_TAIL_LINES = 400                         # "cheaply available" tail window
QL_SHOW_CAP = 12                            # 24h entries shown on screen

_WS_COUNTERS = ("reconnects", "errors", "overflow", "drop")
_WS_LINE = re.compile(r"\[ws_shadow\]\s+(.*)$")
_GAP_MARKER = re.compile(r"gap|resync", re.IGNORECASE)


def utc_now_iso(now_s):
    return datetime.datetime.fromtimestamp(
        now_s, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ql_ts(ts):
    """quality_log ts is ISO-8601 Zulu; 3.9-safe (no fromisoformat-with-Z)."""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.datetime.strptime(ts, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except (ValueError, TypeError):
            continue
    return None


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_fact_path(p):
    """Manifest file_path values are repo-root-relative in production
    ('work/warehouse/facts/...'); tests use absolute paths."""
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


# ------------------------------------------------------------ [1] freshness
def check_freshness(args, hard):
    if args.freshness_json:
        try:
            with open(args.freshness_json) as f:
                fresh = json.load(f)
        except (OSError, ValueError) as e:
            hard.append("freshness JSON unreadable (%s) — fail-closed" % e)
            return {"verdict": "UNREADABLE", "stale_reasons": [str(e)]}
    else:
        try:
            r = subprocess.run(
                [sys.executable, FRESHNESS_TOOL, "--json"],
                capture_output=True, text=True, cwd=ROOT, timeout=180)
            fresh = json.loads(r.stdout)
        except Exception as e:  # no JSON / timeout / missing tool: never green
            hard.append("freshness.py did not produce JSON (%r) — "
                        "fail-closed STALE" % (e,))
            return {"verdict": "UNREADABLE", "stale_reasons": [repr(e)]}
    if fresh.get("verdict") != "FRESH":
        hard.append("freshness STALE: %s"
                    % "; ".join(fresh.get("stale_reasons") or ["(no reason)"]))
    return fresh


# --------------------------------------------------------------- [2] export
def read_manifest(path):
    """rows keyed by (date, file_path), LAST occurrence wins (a --force
    re-export appends history; the newest row is the archive's truth)."""
    rows = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows[(r["date"], r["file_path"])] = r
    return list(rows.values())


def check_export(args, day, prior, hard, warns):
    out = {"tables": {}, "md5_spot_check": {}, "drops": [], "notes": []}
    if not os.path.isfile(args.manifest):
        hard.append("manifest missing: %s (no export record at all)"
                    % args.manifest)
        return out
    rows = read_manifest(args.manifest)
    day_rows = [r for r in rows if r["date"] == day]
    if not day_rows:
        hard.append("manifest has no rows for completed day %s — "
                    "yesterday was never exported" % day)
        return out
    out["manifest_rows"] = len(day_rows)
    for r in day_rows:
        t = out["tables"].setdefault(r["table"], {"rows": 0, "files": 0})
        t["rows"] += int(r["row_count"])
        t["files"] += 1

    # md5 spot check: deterministic sample (evenly spaced over sorted paths)
    sample_src = sorted(day_rows, key=lambda r: r["file_path"])
    n = min(MD5_SPOT_N, len(sample_src))
    step = max(1, len(sample_src) // n)
    checked = mismatched = 0
    for r in sample_src[::step][:n]:
        p = resolve_fact_path(r["file_path"])
        checked += 1
        if not os.path.isfile(p):
            mismatched += 1
            hard.append("md5 spot check: manifest-listed archive file "
                        "MISSING: %s" % r["file_path"])
        elif _md5(p) != r["file_md5"]:
            mismatched += 1
            hard.append("md5 spot check MISMATCH (write-once archive "
                        "modified?): %s" % r["file_path"])
    out["md5_spot_check"] = {"checked": checked, "mismatched": mismatched}

    # category row-count drop detection vs the prior day (report-only)
    prior_rows = [r for r in rows if r["date"] == prior]
    if not prior_rows:
        out["notes"].append("drop-check skipped: no prior-day manifest rows "
                            "for %s (first archived day?)" % prior)
        return out

    def by_cat(rws):
        agg = {}
        for r in rws:
            if r["table"] in DROP_TABLES:
                k = (r["table"], r["category"])
                agg[k] = agg.get(k, 0) + int(r["row_count"])
        return agg

    cur, prev = by_cat(day_rows), by_cat(prior_rows)
    thresh = args.drop_pct / 100.0
    for k in sorted(prev):
        was, now = prev[k], cur.get(k, 0)
        if was <= 0:
            continue
        fall = (was - now) / float(was)
        if fall > thresh:
            msg = ("%s[%s] rows fell %.0f%% vs prior day (%d -> %d)"
                   % (k[0], k[1], fall * 100, was, now))
            out["drops"].append(msg)
            warns.append(msg)
    if not out["drops"]:
        out["notes"].append("drop-check vs %s: no %s category fell more "
                            "than %.0f%%" % (prior, "/".join(DROP_TABLES),
                                             args.drop_pct))
    return out


# ----------------------------------------------------------------- [3] gold
def check_gold(args, day, hard, warns):
    out = {"status": "not_built", "verdict": None, "notes": []}
    qroot = os.path.join(args.gold_root, "quarantine")
    quarantined = []
    if os.path.isdir(qroot):
        pat = re.compile(r"^date=%s(\.\d+)?$" % re.escape(day))
        quarantined = [d for d in sorted(os.listdir(qroot)) if pat.match(d)]
    day_dir = os.path.join(args.gold_root, "date=%s" % day)
    if quarantined:
        out["status"] = "quarantined"
        hard.append("gold day %s QUARANTINED: %s" %
                    (day, ", ".join(os.path.join("quarantine", q)
                                    for q in quarantined)))
        return out
    if not os.path.isdir(day_dir):
        out["notes"].append("gold day %s not built (derived layer; "
                            "not a data-loss condition)" % day)
        return out
    out["status"] = "built"
    report_p = os.path.join(day_dir, "validation_report_%s.json" % day)
    if not os.path.isfile(report_p):
        warns.append("gold day %s built but has NO validation report — "
                     "unvalidated gold is not green" % day)
        return out
    try:
        with open(report_p) as f:
            rep = json.load(f)
    except ValueError as e:
        hard.append("gold validation report unparseable for %s: %s" % (day, e))
        return out
    out["verdict"] = rep.get("verdict")
    checks = rep.get("checks") or {}
    out["checks"] = {k: v.get("status") for k, v in sorted(checks.items())}
    if out["verdict"] != "GREEN":
        hard.append("gold day %s validation verdict %s (day still in the "
                    "green tree!) — treat as quarantined" %
                    (day, out["verdict"]))
    inday_q = os.path.join(day_dir, "quarantine")
    if os.path.isdir(inday_q) and os.listdir(inday_q):
        note = ("gold day %s carries row-level quarantine artifacts "
                "(loader forensic dumps: %d file(s)) — known, see "
                "quality_log" % (day, len(os.listdir(inday_q))))
        out["notes"].append(note)
    return out


# ------------------------------------------------------------- [4] seq gaps
def check_seq_gaps(args, warns):
    out = {"status": "not_instrumented", "source": args.ws_log,
           "note": "per-day gap attribution not instrumented"}
    if not os.path.isfile(args.ws_log):
        out["note"] = ("no ws_shadow/ingest log at %s — sequence-gap scan "
                       "not instrumented" % args.ws_log)
        return out
    try:
        with open(args.ws_log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError as e:
        out["note"] = "ws log unreadable: %s" % e
        return out
    tail = tail[-args.ws_tail:]
    markers = [ln for ln in tail if _GAP_MARKER.search(ln)]
    counters = None
    for ln in reversed(tail):
        m = _WS_LINE.search(ln)
        if m:
            counters = {k: int(v) for k, v in
                        re.findall(r"(\w+)=(\d+)", m.group(1))}
            break
    if counters is None and not markers:
        out["note"] = ("ws log tail has no stats lines or gap/resync "
                       "markers — not instrumented for this scan")
        return out
    out["status"] = "scanned"
    out["tail_lines"] = len(tail)
    out["gap_marker_lines"] = len(markers)
    out["gap_marker_sample"] = markers[-3:]
    out["counters"] = counters or {}
    out["note"] = ("counters cumulative since daemon start (log lines are "
                   "untimestamped); per-day attribution not instrumented")
    if markers:
        warns.append("%d gap/resync marker line(s) in ws log tail (last: %s)"
                     % (len(markers), markers[-1][:120]))
    bad = {k: v for k, v in (counters or {}).items()
           if k in _WS_COUNTERS and v}
    if bad:
        warns.append("ws_shadow nonzero counters in log tail: %s "
                     "(cumulative since daemon start)"
                     % ", ".join("%s=%d" % kv for kv in sorted(bad.items())))
    return out


# ------------------------------------------------------- [5] quality log 24h
def read_quality_log_24h(path, now_s, warns):
    entries, unparseable = [], 0
    if not os.path.isfile(path):
        return entries
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
                ts = parse_ql_ts(e.get("ts"))
            except ValueError:
                unparseable += 1
                continue
            if ts is None:
                unparseable += 1
                continue
            if now_s - ts <= 86400:
                entries.append(e)
    if unparseable:
        warns.append("quality_log has %d unparseable line(s)" % unparseable)
    return entries


# ----------------------------------------------------------- [6] append line
def append_quality_log(path, now_s, day, finding, evidence):
    entry = {"ts": utc_now_iso(now_s), "wp": "daily-check", "window": day,
             "finding": finding, "action": "reviewed", "evidence": evidence}
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


# ------------------------------------------------------------------- output
def human_report(res):
    L = []
    L.append("==== DAILY QUALITY CHECK -- %s (run %s) ====" %
             (res["date"], res["generated_utc"]))
    fr = res["freshness"]
    lag = (lambda v: "%.1fs" % v if isinstance(v, (int, float)) else "n/a")
    L.append("[1] FRESHNESS (now):  %s  staging_lag=%s capture_lag=%s"
             % (fr.get("verdict"), lag(fr.get("staging_lag_s")),
                lag(fr.get("capture_lag_s"))))
    for rzn in fr.get("stale_reasons") or []:
        L.append("      ! %s" % rzn)
    ex = res["export"]
    if ex.get("tables"):
        L.append("[2] EXPORT %s:  %d manifest rows" %
                 (res["date"], ex.get("manifest_rows", 0)))
        for t, v in sorted(ex["tables"].items()):
            L.append("      %-16s %12s rows / %3d files"
                     % (t, "{:,}".format(v["rows"]), v["files"]))
        sc = ex["md5_spot_check"]
        L.append("      md5 spot-check: %d/%d match"
                 % (sc["checked"] - sc["mismatched"], sc["checked"]))
        for d in ex["drops"]:
            L.append("      WARNING drop: %s" % d)
        for n in ex["notes"]:
            L.append("      %s" % n)
    else:
        L.append("[2] EXPORT %s:  NO DATA (see failures)" % res["date"])
    g = res["gold"]
    L.append("[3] GOLD %s:  %s%s" % (res["date"], g["status"],
             "  verdict=%s" % g["verdict"] if g.get("verdict") else ""))
    if g.get("checks"):
        L.append("      checks: %s" % " ".join(
            "%s=%s" % kv for kv in g["checks"].items()))
    for n in g.get("notes", []):
        L.append("      note: %s" % n)
    sq = res["seq_gaps"]
    if sq["status"] == "scanned":
        c = sq.get("counters", {})
        L.append("[4] SEQ GAPS:  ws tail(%d lines): markers=%d  %s"
                 % (sq.get("tail_lines", 0), sq.get("gap_marker_lines", 0),
                    " ".join("%s=%d" % (k, c[k]) for k in _WS_COUNTERS
                             if k in c)))
        L.append("      %s" % sq["note"])
    else:
        L.append("[4] SEQ GAPS:  not instrumented -- %s" % sq["note"])
    ql = res["quality_log_24h"]
    L.append("[5] QUALITY LOG (24h):  %d entr%s"
             % (len(ql), "y" if len(ql) == 1 else "ies"))
    for e in ql[:QL_SHOW_CAP]:
        L.append("      - %s %s: %s" % (e.get("ts"), e.get("wp"),
                                        str(e.get("finding"))[:100]))
    if len(ql) > QL_SHOW_CAP:
        L.append("      ... %d more in %s" % (len(ql) - QL_SHOW_CAP,
                                              res["quality_log_path"]))
    L.append("[6] LOGGED: %s -> %s" % (res["logged_entry"]["finding"],
                                       res["quality_log_path"]))
    if res["warnings"]:
        L.append("WARNINGS (%d):" % len(res["warnings"]))
        for w in res["warnings"]:
            L.append("  WARNING: %s" % w)
    if res["hard_failures"]:
        L.append("HARD FAILURES (%d):" % len(res["hard_failures"]))
        for h in res["hard_failures"]:
            L.append("  RED: %s" % h)
        L.append("VERDICT: DAILY RED -- fix before trusting the day")
    elif res["warnings"]:
        L.append("VERDICT: DAILY GREEN (with %d warning(s) -- read them)"
                 % len(res["warnings"]))
    else:
        L.append("VERDICT: DAILY GREEN")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="WP-08 daily quality check: yesterday's health on one "
                    "screen + one quality_log line. Exit 0 = nothing red.")
    ap.add_argument("--date", default=None,
                    help="day to check (default: yesterday UTC)")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output")
    ap.add_argument("--manifest", default=DEFAULT_MANIFEST)
    ap.add_argument("--gold-root", default=DEFAULT_GOLD_ROOT)
    ap.add_argument("--ws-log", default=DEFAULT_WS_LOG,
                    help="ws_shadow/ingest log to tail for gap markers")
    ap.add_argument("--ws-tail", type=int, default=WS_TAIL_LINES)
    ap.add_argument("--quality-log",
                    default=os.environ.get("DAILY_CHECK_QUALITY_LOG",
                                           DEFAULT_QUALITY_LOG),
                    help="append target (env DAILY_CHECK_QUALITY_LOG "
                         "overrides the default; tests point this at tmp)")
    ap.add_argument("--freshness-json", default=None,
                    help="TEST HOOK: read this file instead of running "
                         "tools/freshness.py --json")
    ap.add_argument("--drop-pct", type=float, default=50.0,
                    help="category row-count fall (%%) that flags a warning")
    ap.add_argument("--now", type=float, default=None,
                    help="epoch seconds (tests only)")
    args = ap.parse_args(argv)

    import time
    now_s = args.now if args.now is not None else time.time()
    today = datetime.datetime.fromtimestamp(
        now_s, tz=datetime.timezone.utc).date()
    day = args.date or (today - datetime.timedelta(days=1)).isoformat()
    prior = (datetime.date.fromisoformat(day)
             - datetime.timedelta(days=1)).isoformat()

    hard, warns = [], []
    fresh = check_freshness(args, hard)
    export = check_export(args, day, prior, hard, warns)
    gold = check_gold(args, day, hard, warns)
    seq = check_seq_gaps(args, warns)
    ql24 = read_quality_log_24h(args.quality_log, now_s, warns)

    if hard:
        finding = "RED:%d" % len(hard)
    elif warns:
        finding = "WARNINGS:%d" % len(warns)
    else:
        finding = "GREEN"
    tbl = export.get("tables", {})
    # staging lag recorded per day: WP-03's BACKLOG note wants a week of
    # observed lag distribution in the log before the threshold is retuned
    lag = fresh.get("staging_lag_s")
    fresh_ev = "%s(staging_lag=%ss)" % (
        fresh.get("verdict"), lag if lag is not None else "n/a")
    evidence = ("freshness=%s; manifest_rows=%s; %s; md5=%s/%s; gold=%s; "
                "seq=%s; warnings=%d; hard=%d" % (
                    fresh_ev, export.get("manifest_rows", 0),
                    " ".join("%s=%d" % (t, v["rows"])
                             for t, v in sorted(tbl.items())) or "no-tables",
                    export.get("md5_spot_check", {}).get("checked", 0)
                    - export.get("md5_spot_check", {}).get("mismatched", 0),
                    export.get("md5_spot_check", {}).get("checked", 0),
                    gold.get("verdict") or gold.get("status"),
                    seq.get("status"), len(warns), len(hard)))
    entry = append_quality_log(args.quality_log, now_s, day, finding, evidence)

    res = {"date": day, "prior_date": prior, "generated_utc": utc_now_iso(now_s),
           "freshness": fresh, "export": export, "gold": gold,
           "seq_gaps": seq, "quality_log_24h": ql24,
           "quality_log_path": args.quality_log, "logged_entry": entry,
           "warnings": warns, "hard_failures": hard,
           "verdict": ("RED" if hard else "WARNINGS" if warns else "GREEN")}
    if args.json:
        print(json.dumps(res, indent=1))
    else:
        print(human_report(res))
    return 1 if hard else 0


if __name__ == "__main__":
    sys.exit(main())
