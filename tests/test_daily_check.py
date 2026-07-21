#!/usr/bin/env python3
"""WP-08 Daily Quality Check — contract tests for tools/daily_check.py.

The two-minute morning ritual as ONE command: yesterday's health on one
screen. Contract under test:
  (1) freshness is CONSUMED from WP-03 (tools/freshness.py --json), never
      reimplemented. Test seam (documented): --freshness-json <file> injects
      the exact JSON object freshness.py would print, so these tests exercise
      daily_check's handling of FRESH/STALE without building a live staging
      DB (tests/test_freshness.py already proves the producer end-to-end
      against a real Ingester-built staging; duplicating that here would
      double-run one infrastructure, A1). Without the flag the tool MUST
      subprocess the real freshness.py.
  (2) yesterday's export from manifest.csv: per-table row counts, md5 spot
      check (mismatch = hard fail, fail-closed S2), category row-count DROP
      detection vs the prior day's manifest rows (>X% fall on trades/
      orderbooks_l1 = WARNING, report-only — prints loudly, exit stays 0).
  (3) gold day status: quarantined day (root/quarantine/date=<D>) or a
      QUARANTINE validation verdict = hard fail; GREEN passes.
  (4) sequence gaps: ws_shadow log tail counters when the log exists;
      "not instrumented" HONESTLY when it does not (D2 — never silent).
  (5) quality_log entries from the last 24h are shown (--now injects time).
  (6) ONE schema-conformant line {ts,wp,window,finding,action,evidence} is
      APPENDED to the quality log — tests ALWAYS pass --quality-log (or the
      DAILY_CHECK_QUALITY_LOG env var) pointing at a tmp file so the real
      work/quality_log.ndjson is never touched from tests.
Exit code: 0 when nothing red (warnings print loudly but do not fail);
1 on any hard failure (freshness STALE, manifest missing for a completed
day, gold day quarantined, md5 mismatch). pytest-native; stdlib only.
"""
import csv
import hashlib
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "daily_check.py")

# Deterministic clock: 2026-07-07T08:00:00Z => "yesterday" = 2026-07-06.
NOW_S = 1783382400 + 8 * 3600
DAY = "2026-07-06"
PRIOR = "2026-07-05"

MANIFEST_HEADER = ["date", "table", "category", "subcategory", "row_count",
                   "file_path", "file_md5", "created_ts"]
QL_KEYS = {"ts", "wp", "window", "finding", "action", "evidence"}


# ---------------------------------------------------------------- fixtures
def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def make_fact_file(tmp, name, content=b"row1\nrow2\n"):
    p = os.path.join(tmp, "facts", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(content)
    return p


def write_manifest(tmp, rows):
    """rows: list of (date, table, category, subcategory, row_count,
    file_path, file_md5). Absolute file paths (tests); production uses
    repo-root-relative paths."""
    p = os.path.join(tmp, "manifest.csv")
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(MANIFEST_HEADER)
        for r in rows:
            w.writerow(list(r) + ["2026-07-07T02:00:00Z"])
    return p


def write_freshness(tmp, verdict="FRESH", reasons=None):
    """The documented --freshness-json test seam: the exact object
    tools/freshness.py --json prints."""
    p = os.path.join(tmp, "freshness.json")
    with open(p, "w") as f:
        json.dump({"verdict": verdict, "threshold_s": 600.0,
                   "staging_lag_s": 48.2 if verdict == "FRESH" else 3600.0,
                   "capture_lag_s": 3.1,
                   "staging_newest_table": "trades",
                   "stale_reasons": reasons or []}, f)
    return p


def make_gold(tmp, day=DAY, state="green"):
    """state: green | report_quarantine | day_quarantined | none."""
    root = os.path.join(tmp, "gold")
    os.makedirs(root, exist_ok=True)
    if state == "none":
        return root
    if state == "day_quarantined":
        os.makedirs(os.path.join(root, "quarantine", "date=%s" % day))
        return root
    d = os.path.join(root, "date=%s" % day)
    os.makedirs(d)
    verdict = "GREEN" if state == "green" else "QUARANTINE"
    with open(os.path.join(d, "validation_report_%s.json" % day), "w") as f:
        json.dump({"date": day, "verdict": verdict,
                   "checks": {"V2": {"status": "PASS", "violations_total": 0}}}, f)
    return root


def base_env(tmp, drop=False, gold="green", fresh="FRESH", prior=True):
    """A complete healthy fixture; knobs poison one dimension at a time.
    Returns (args, paths) for run_tool."""
    f1 = make_fact_file(tmp, "trades_sports.csv.gz")
    f2 = make_fact_file(tmp, "l1_sports.parquet", b"x" * 64)
    rows = [
        (DAY, "trades", "Sports", "Soccer",
         100 if drop else 1000, f1, _md5(f1)),
        (DAY, "orderbooks_l1", "Sports", "Soccer", 2000, f2, _md5(f2)),
    ]
    if prior:
        # distinct file_paths: the manifest is keyed by (date, file_path) —
        # last row wins per file so a --force re-export's appended history
        # never double-counts (prior-day files need not exist on disk; only
        # yesterday's rows are md5 spot-checked)
        rows += [
            (PRIOR, "trades", "Sports", "Soccer", 1000,
             "prior_trades_sports.csv.gz", "0" * 32),
            (PRIOR, "orderbooks_l1", "Sports", "Soccer", 2000,
             "prior_l1_sports.parquet", "0" * 32),
        ]
    manifest = write_manifest(tmp, rows)
    gold_root = make_gold(tmp, state=gold)
    fresh_p = write_freshness(tmp, verdict=fresh,
                              reasons=[] if fresh == "FRESH" else
                              ["staging lag 3600.0s > threshold 600s"])
    ql = os.path.join(tmp, "quality_log.ndjson")
    args = ["--date", DAY, "--now", str(NOW_S),
            "--manifest", manifest, "--gold-root", gold_root,
            "--freshness-json", fresh_p, "--quality-log", ql,
            "--ws-log", os.path.join(tmp, "no_such_ws_shadow.log")]
    return args, {"manifest": manifest, "gold_root": gold_root,
                  "quality_log": ql, "f1": f1}


def run_tool(*args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run([sys.executable, TOOL] + list(args),
                          capture_output=True, text=True, cwd=ROOT, env=e)


def run_json(*args, env=None):
    r = run_tool("--json", *args, env=env)
    try:
        return r, json.loads(r.stdout)
    except json.JSONDecodeError:
        pytest.fail("--json did not print valid JSON. rc=%d stdout=%r "
                    "stderr=%r" % (r.returncode, r.stdout, r.stderr))


def last_ql_line(path):
    with open(path) as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert lines, "quality log has no entries"
    return json.loads(lines[-1])


# ------------------------------------------------------------ green path
def test_green_day_exits_0_with_token(tmp_path):
    args, paths = base_env(str(tmp_path))
    r = run_tool(*args)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DAILY GREEN" in r.stdout
    assert DAY in r.stdout


def test_json_output_is_machine_readable(tmp_path):
    args, _ = base_env(str(tmp_path))
    r, out = run_json(*args)
    assert r.returncode == 0
    assert out["date"] == DAY
    assert out["verdict"] == "GREEN"
    assert out["freshness"]["verdict"] == "FRESH"
    assert out["export"]["tables"]["trades"]["rows"] == 1000
    assert out["export"]["tables"]["orderbooks_l1"]["rows"] == 2000
    assert out["export"]["md5_spot_check"]["checked"] >= 1
    assert out["export"]["md5_spot_check"]["mismatched"] == 0
    assert out["gold"]["verdict"] == "GREEN"
    assert out["hard_failures"] == []


# --------------------------------------------------- (2) export + drops
def test_category_row_count_drop_warns_but_does_not_fail(tmp_path):
    args, _ = base_env(str(tmp_path), drop=True)  # trades 1000 -> 100 (-90%)
    r, out = run_json(*args)
    assert r.returncode == 0, "row-count drops are report-only, never exit 1"
    assert out["verdict"] == "WARNINGS"
    drops = [w for w in out["warnings"]
             if "trades" in w and "Sports" in w and "90" in w]
    assert drops, "expected a trades/Sports 90%% drop warning, got %r" % (
        out["warnings"],)
    # prints loudly on the human screen too
    r2 = run_tool(*args)
    assert "WARNING" in r2.stdout and "Sports" in r2.stdout


def test_drop_threshold_is_configurable(tmp_path):
    args, _ = base_env(str(tmp_path), drop=True)
    r, out = run_json(*args, "--drop-pct", "95")  # -90% no longer flags
    assert r.returncode == 0
    assert not [w for w in out["warnings"] if "fell" in w]


def test_no_prior_day_history_skips_drop_check_honestly(tmp_path):
    args, _ = base_env(str(tmp_path), prior=False)
    r, out = run_json(*args)
    assert r.returncode == 0
    assert "no prior-day manifest rows" in " ".join(
        out["export"]["notes"]).lower() or \
        "skipped" in " ".join(out["export"]["notes"]).lower()


def test_manifest_missing_rows_for_completed_day_exits_1(tmp_path):
    tmp = str(tmp_path)
    # manifest exists but carries ONLY the prior day: yesterday never exported
    manifest = write_manifest(tmp, [
        (PRIOR, "trades", "Sports", "Soccer", 1000, "unused", "0" * 32)])
    args = ["--date", DAY, "--now", str(NOW_S), "--manifest", manifest,
            "--gold-root", make_gold(tmp, state="green"),
            "--freshness-json", write_freshness(tmp),
            "--quality-log", os.path.join(tmp, "ql.ndjson"),
            "--ws-log", os.path.join(tmp, "none.log")]
    r, out = run_json(*args)
    assert r.returncode == 1
    assert any("manifest" in h and DAY in h for h in out["hard_failures"])


def test_manifest_file_missing_exits_1(tmp_path):
    tmp = str(tmp_path)
    args = ["--date", DAY, "--now", str(NOW_S),
            "--manifest", os.path.join(tmp, "no_manifest.csv"),
            "--gold-root", make_gold(tmp, state="green"),
            "--freshness-json", write_freshness(tmp),
            "--quality-log", os.path.join(tmp, "ql.ndjson"),
            "--ws-log", os.path.join(tmp, "none.log")]
    r = run_tool(*args)
    assert r.returncode == 1


def test_md5_spot_check_mismatch_exits_1(tmp_path):
    args, paths = base_env(str(tmp_path))
    with open(paths["f1"], "ab") as f:
        f.write(b"tampered\n")  # archive file no longer matches its manifest md5
    r, out = run_json(*args)
    assert r.returncode == 1
    assert any("md5" in h.lower() for h in out["hard_failures"])


# ------------------------------------------------------- (1) freshness
def test_freshness_stale_exits_1(tmp_path):
    args, _ = base_env(str(tmp_path), fresh="STALE")
    r, out = run_json(*args)
    assert r.returncode == 1
    assert out["freshness"]["verdict"] == "STALE"
    assert any("STALE" in h for h in out["hard_failures"])
    # stale_reasons carried through verbatim (BACKLOG WP-03 note: consumers
    # branch on reasons, not exit codes)
    assert out["freshness"]["stale_reasons"]


def test_freshness_json_hook_feeds_verdict_through(tmp_path):
    args, _ = base_env(str(tmp_path))
    r, out = run_json(*args)
    assert out["freshness"]["staging_lag_s"] == 48.2
    assert out["freshness"]["capture_lag_s"] == 3.1


# ------------------------------------------------------------ (3) gold
def test_gold_day_quarantined_exits_1(tmp_path):
    args, _ = base_env(str(tmp_path), gold="day_quarantined")
    r, out = run_json(*args)
    assert r.returncode == 1
    assert any("quarantin" in h.lower() for h in out["hard_failures"])


def test_gold_quarantine_verdict_in_report_exits_1(tmp_path):
    args, _ = base_env(str(tmp_path), gold="report_quarantine")
    r, out = run_json(*args)
    assert r.returncode == 1
    assert any("quarantin" in h.lower() or "QUARANTINE" in h
               for h in out["hard_failures"])


def test_gold_not_built_is_reported_not_failed(tmp_path):
    args, _ = base_env(str(tmp_path), gold="none")
    r, out = run_json(*args)
    assert r.returncode == 0
    assert out["gold"]["status"] == "not_built"


# -------------------------------------------------- (4) sequence gaps
def test_seq_gaps_not_instrumented_is_honest(tmp_path):
    args, _ = base_env(str(tmp_path))  # ws-log path does not exist
    r, out = run_json(*args)
    assert r.returncode == 0
    assert out["seq_gaps"]["status"] == "not_instrumented"
    r2 = run_tool(*args)
    assert "not instrumented" in r2.stdout


def test_seq_gap_counters_from_ws_log_tail_warn_when_nonzero(tmp_path):
    tmp = str(tmp_path)
    args, _ = base_env(tmp)
    ws = os.path.join(tmp, "ws_shadow.log")
    with open(ws, "w") as f:
        f.write("[ws_shadow] events=100 deltas=0 reconnects=0 errors=0 "
                "overflow=0 epoch=1 rec=100 drop=0\n")
        f.write("[ws_shadow] events=200 deltas=0 reconnects=2 errors=1 "
                "overflow=0 epoch=1 rec=200 drop=3\n")
    args = [a if a != os.path.join(tmp, "no_such_ws_shadow.log") else ws
            for a in args]
    r, out = run_json(*args)
    assert r.returncode == 0, "gap counters warn, never hard-fail"
    assert out["seq_gaps"]["counters"]["reconnects"] == 2
    assert out["seq_gaps"]["counters"]["drop"] == 3
    assert any("reconnects" in w or "drop" in w for w in out["warnings"])
    # honesty: counters are cumulative, not per-day
    assert "cumulative" in out["seq_gaps"]["note"]


# ------------------------------------------- (5)+(6) quality log I/O
def test_quality_log_append_is_schema_conformant(tmp_path):
    args, paths = base_env(str(tmp_path))
    r = run_tool(*args)
    assert r.returncode == 0
    entry = last_ql_line(paths["quality_log"])
    assert set(entry.keys()) == QL_KEYS
    assert entry["wp"] == "daily-check"
    assert entry["window"] == DAY
    assert entry["finding"] == "GREEN"
    assert entry["action"] == "reviewed"
    assert entry["evidence"]
    # the observed staging lag is recorded per day (WP-03 BACKLOG note:
    # a week of lag distribution before anyone retunes the 600s threshold)
    assert "staging_lag=48.2" in entry["evidence"]


def test_quality_log_finding_counts_warnings(tmp_path):
    args, paths = base_env(str(tmp_path), drop=True)
    run_tool(*args)
    entry = last_ql_line(paths["quality_log"])
    assert entry["finding"].startswith("WARNINGS:")
    assert int(entry["finding"].split(":")[1]) >= 1


def test_quality_log_finding_red_on_hard_failure(tmp_path):
    """finding is never GREEN/WARNINGS while exiting 1 (D2: a log line that
    says green next to a red exit would be the lie this system exists to
    kill). RED:<n> extends the spec's <GREEN|WARNINGS:n> enum honestly."""
    args, paths = base_env(str(tmp_path), fresh="STALE")
    r = run_tool(*args)
    assert r.returncode == 1
    entry = last_ql_line(paths["quality_log"])
    assert entry["finding"].startswith("RED:")


def test_quality_log_24h_window_filters_old_entries(tmp_path):
    args, paths = base_env(str(tmp_path))
    with open(paths["quality_log"], "w") as f:
        f.write(json.dumps({"ts": "2026-07-07T02:00:00Z", "wp": "recent-wp",
                            "window": "w", "finding": "f", "action": "a",
                            "evidence": "e"}) + "\n")
        f.write(json.dumps({"ts": "2026-07-05T00:00:00Z", "wp": "ancient-wp",
                            "window": "w", "finding": "f", "action": "a",
                            "evidence": "e"}) + "\n")
    r, out = run_json(*args)
    shown = [e["wp"] for e in out["quality_log_24h"]]
    assert "recent-wp" in shown
    assert "ancient-wp" not in shown
    r2 = run_tool(*args)
    assert "recent-wp" in r2.stdout and "ancient-wp" not in r2.stdout


def test_quality_log_env_var_override(tmp_path):
    tmp = str(tmp_path)
    args, _ = base_env(tmp)
    args = [a for i, a in enumerate(args)
            if args[i - 1] != "--quality-log" and a != "--quality-log"]
    ql_env = os.path.join(tmp, "env_quality_log.ndjson")
    r = run_tool(*args, env={"DAILY_CHECK_QUALITY_LOG": ql_env})
    assert r.returncode == 0
    entry = last_ql_line(ql_env)
    assert entry["wp"] == "daily-check"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
