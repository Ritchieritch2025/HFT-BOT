"""W2.5: validator harness — tools/gold_validate.py (PLAN_GOLD_DATA_CONTRACT
§2.3 + W2.5). Contract encoded here, written BEFORE tools/gold_validate.py
exists (TDD):

  - validate_day(root, date) runs V2, V3, V4, V6, V9, V10 INDEPENDENTLY —
    plus the V8-shape trade-hash reconciliation (gold_io.reconcile_trade_
    hashes), wired per the W2.4 finding that it must not stay dormant. One
    failing check never masks another; every check reports its own
    violations. Verdict: ALL pass => GREEN; any fail => QUARANTINE.
  - quarantine: the WHOLE day partition is MOVED to <root>/quarantine/
    date=<D>/ (collision => .N suffix; nothing deleted, P6) with a
    validation_report_<D>.json written inside; the CLI exits nonzero.
  - EACH check has one committed seeded-defect gold day under
    tests/fixtures/gold_defects/<name>/date=<DATE>/ that turns EXACTLY its
    own check red. Defect days are built with the W2.4 writer + targeted
    post-write tampering; manifest md5s are made self-consistent so the
    md5 gate is NOT what fails (except v2_count_mismatch, where the
    manifest defect IS the check). Regenerate deterministically with:
        python3 tests/test_gold_validate.py
"""
import csv
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:                      # __main__ fixture regeneration
    sys.path.insert(0, ROOT)

from tools.gold_dtype import EVENT_TYPE, FLAGS, GOLD_DTYPE, fnv1a64
from tools.gold_load import BookMsg, Event, L1Top, Trade
from tools.gold_merge import merge
from tools.gold_io import day_paths, write_day
from tools.gold_validate import (CHECKS, main, quarantine_day, validate_day)

DEFECTS = os.path.join(ROOT, "tests", "fixtures", "gold_defects")
DATE = "2026-07-06"
T0 = 1783300000000000
S = 1_000_000

SNAP, DELTA = EVENT_TYPE["BOOK_SNAPSHOT"], EVENT_TYPE["BOOK_DELTA"]
TRADE, L1, HB = EVENT_TYPE["TRADE"], EVENT_TYPE["L1_TICKER"], EVENT_TYPE["HEARTBEAT"]
A, B, C, D = "KXGV-A", "KXGV-B", "KXGV-C", "KXGV-D"
UUID = {A: "a0be3c5a-8563-725e-5d8e-26026dda3934",
        B: "b1cf4d6b-9674-836f-6e9f-37137eeb4a45",
        D: "c2d05e7c-a785-947a-7fa0-48248ffc5b56"}

# One committed seeded-defect gold day PER CHECK -> the check it must red.
DEFECT_DAYS = {"v2_count_mismatch": "V2",
               "v3_stream_seq_gap": "V3",
               "v4_negative_level": "V4",
               "v6_trade_lookahead": "V6",
               "v9_heartbeat_diff": "V9",
               "v10_coverage_lie": "V10"}


# ------------------------------------------------------------ day construction

def _snap(yes, no, mt, ts):
    return Event(SNAP, ts, mt, BookMsg(None, None, None, tuple(yes), tuple(no)))


def _delta(side, px, d, mt, ts):
    return Event(DELTA, ts, mt, BookMsg(side, px, d, None, None))


def _trade(mt, ts, yp=4500, ct=30000, side="yes"):
    return Event(TRADE, ts, mt, Trade(UUID[mt], yp, 10000 - yp, ct, side))


def base_day():
    """A: covered + trade + heartbeat; B: covered + trade (same-us delta);
    C: L1-only; D: traded-but-uncovered. Last record is a BOOK_DELTA (so the
    v3 fixture can tamper stream_seq without touching the trade sidecar)."""
    full_a = [_snap([(4500, 100000), (4400, 50000)], [(5300, 70000)], A, T0),
              _delta("yes", 4500, -30000, A, T0 + S),
              _delta("no", 5300, -20000, A, T0 + 2 * S),
              _delta("yes", 4400, -10000, A, T0 + 5 * S)]
    full_b = [_snap([(3000, 20000)], [(6500, 10000)], B, T0 + S // 2),
              _delta("yes", 3000, 5000, B, T0 + 3 * S)]
    trades = [_trade(A, T0 + S),
              _trade(B, T0 + 3 * S, yp=3000, side="no"),
              _trade(D, T0 + 4 * S)]
    l1_c = [Event(L1, T0 + S, C, L1Top(4500, 100000, 4700, 50000, True))]
    hbs = [Event(HB, T0 + 2 * S + 1, A, None)]
    records, _ = merge([full_a, full_b, trades, l1_c, hbs])
    dim = {mt: {"category": "Sports", "close_time": "2026-07-06T23:00:00Z",
                "liquidity_tier": "high"} for mt in (A, B, C, D)}
    return records, dim


def _write_base_day(root):
    records, dim = base_day()
    write_day(records, dim, DATE, root)
    return day_paths(root, DATE)


def _md5(path):
    import hashlib
    h = hashlib.md5()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _rewrite_manifest_md5s(p):
    """Make the manifest self-consistent AFTER tampering, so the md5 gate is
    not what fails — the targeted CHECK must fail (W2.5 fixture rule)."""
    with open(p["manifest"]) as f:
        man = json.load(f)
    for name in man["files"]:
        man["files"][name] = _md5(os.path.join(p["dir"], name))
    with open(p["manifest"], "w") as f:
        json.dump(man, f, indent=1, sort_keys=True)


def _tamper_bin(p, fn):
    arr = np.fromfile(p["bin"], dtype=GOLD_DTYPE)
    fn(arr)
    arr.tofile(p["bin"])
    _rewrite_manifest_md5s(p)


def _market_ids(p):
    with open(p["markets"], newline="") as f:
        return {r["market_ticker"]: int(r["market_id"]) for r in csv.DictReader(f)}


def _trade_row(arr, mt):
    return int(np.nonzero((arr["event_type"] == TRADE) &
                          (arr["trade_id_hash"] == fnv1a64(UUID[mt])))[0][0])


# --------------------------------------------------- seeded-defect day builders

def build_defect_days(defects_root=DEFECTS):
    """Regenerate ALL committed defect fixture days (deterministic bytes)."""
    for name in DEFECT_DAYS:
        root = os.path.join(defects_root, name)
        shutil.rmtree(os.path.join(root, "date=%s" % DATE), ignore_errors=True)
        p = _write_base_day(root)
        ids = _market_ids(p)
        if name == "v2_count_mismatch":       # manifest lies about row count
            with open(p["manifest"]) as f:
                man = json.load(f)
            man["record_count"] += 1
            with open(p["manifest"], "w") as f:
                json.dump(man, f, indent=1, sort_keys=True)
        elif name == "v3_stream_seq_gap":     # last record (a delta) skips one

            def gap(arr):
                arr["stream_seq"][-1] += 1
            _tamper_bin(p, gap)
        elif name == "v4_negative_level":     # negative qty emitted on B's snap

            def neg(arr):
                i = int(np.nonzero((arr["event_type"] == SNAP) &
                                   (arr["market_id"] == ids[B]))[0][0])
                arr["bid_qty_e4"][i][0] = -1
            _tamper_bin(p, neg)
        elif name == "v6_trade_lookahead":    # trade references the NEXT mint

            def ahead(arr):
                arr["book_seq"][_trade_row(arr, B)] += 1
            _tamper_bin(p, ahead)
        elif name == "v9_heartbeat_diff":     # heartbeat diffs book state

            def diff(arr):
                i = int(np.nonzero(arr["event_type"] == HB)[0][0])
                arr["bid_qty_e4"][i][0] += 777
            _tamper_bin(p, diff)
        elif name == "v10_coverage_lie":      # uncovered traded market flagged

            def lie(arr):
                i = _trade_row(arr, D)
                arr["flags"][i] |= FLAGS["F_BOOK_COVERED"]
            _tamper_bin(p, lie)
    return sorted(DEFECT_DAYS)


def _matrix_line(label, rep):
    return "%-22s %s  -> %s" % (
        label,
        " ".join("%s=%s" % (c, rep["checks"][c]["status"]) for c in CHECKS),
        rep["verdict"])


# ------------------------------------------------------------------ the checks

def test_good_day_all_checks_green(tmp_path):
    root = str(tmp_path)
    _write_base_day(root)
    rep = validate_day(root, DATE)
    for c in CHECKS:
        assert rep["checks"][c]["status"] == "PASS", (c, rep["checks"][c])
        assert rep["checks"][c]["violations_total"] == 0
    assert rep["verdict"] == "GREEN"
    # coverage honesty is REPORTED even when green (V10 counts, never drops):
    counts = rep["checks"]["V10"]["counts"]
    assert counts["traded_but_uncovered"] == 1          # D traded, no snapshot
    assert counts["markets_bin"] == counts["markets_sidecar"] == 4
    print("\n" + _matrix_line("good_day", rep))


def test_defect_fixture_days_exist_and_are_git_tracked():
    """Fresh-clone safety (G2 precedent): every fixture day dir is complete
    on disk AND git-tracked (gitignore negation must cover the .bin files)."""
    tracked = set(subprocess.run(
        ["git", "ls-files", "tests/fixtures/gold_defects"], cwd=ROOT,
        capture_output=True, text=True).stdout.splitlines())
    for name in DEFECT_DAYS:
        p = day_paths(os.path.join(DEFECTS, name), DATE)
        for key in ("bin", "markets", "trade_ids", "manifest"):
            assert os.path.isfile(p[key]), (name, key)
            rel = os.path.relpath(p[key], ROOT)
            assert rel in tracked, "fixture NOT git-tracked: %s" % rel


def test_matrix_each_defect_day_reds_exactly_its_own_check():
    """The W2.5 acceptance matrix: every seeded-defect day turns EXACTLY its
    target check red — one check red, all six others green, day quarantined."""
    lines = []
    for name, target in sorted(DEFECT_DAYS.items()):
        rep = validate_day(os.path.join(DEFECTS, name), DATE)
        lines.append(_matrix_line(name, rep))
        assert rep["checks"][target]["status"] == "FAIL", \
            "%s did NOT red its target %s: %s" % (name, target, rep["checks"][target])
        assert rep["checks"][target]["violations_total"] > 0
        for other in CHECKS:
            if other != target:
                assert rep["checks"][other]["status"] == "PASS", \
                    "%s bled into %s: %s" % (name, other, rep["checks"][other])
        assert rep["verdict"] == "QUARANTINE"
    print("\nW2.5 per-check green/red matrix (committed defect fixture days):")
    print("\n".join(lines))


def test_v8_reconciliation_wired_not_dormant(tmp_path):
    """W2.4 audit finding: reconcile_trade_hashes must run in the harness.
    A tampered sidecar UUID (manifest md5s made self-consistent, so V2 stays
    green) must turn V8 red and quarantine the day."""
    root = str(tmp_path)
    p = _write_base_day(root)
    with open(p["trade_ids"], newline="") as f:
        rows = list(csv.reader(f))
    rows[1][1] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    with open(p["trade_ids"], "w", newline="") as f:
        csv.writer(f).writerows(rows)
    _rewrite_manifest_md5s(p)
    rep = validate_day(root, DATE)
    assert rep["checks"]["V8"]["status"] == "FAIL"
    assert rep["checks"]["V2"]["status"] == "PASS"      # md5 gate NOT the catch
    assert rep["verdict"] == "QUARANTINE"
    print("\n" + _matrix_line("v8_uuid_tamper", rep))


def test_v9_catches_book_seq_advance_too(tmp_path):
    """V9's other half: a heartbeat that ADVANCES book_seq (state untouched)
    is caught — and only V9 goes red."""
    root = str(tmp_path)
    p = _write_base_day(root)

    def advance(arr):
        i = int(np.nonzero(arr["event_type"] == HB)[0][0])
        arr["book_seq"][i] += 1
    _tamper_bin(p, advance)
    rep = validate_day(root, DATE)
    assert rep["checks"]["V9"]["status"] == "FAIL"
    for other in CHECKS:
        if other != "V9":
            assert rep["checks"][other]["status"] == "PASS", other


def test_v10_sidecar_shrinkage_reported_never_dropped(tmp_path):
    """Silent inner-join shrinkage: a market's sidecar row disappears (md5s
    made consistent). V10 must red with the id named; V2 independently reds
    on the manifest count — BOTH are reported (independence, D2)."""
    root = str(tmp_path)
    p = _write_base_day(root)
    with open(p["markets"], newline="") as f:
        rows = list(csv.reader(f))
    dropped = rows.pop()                                # market D vanishes
    with open(p["markets"], "w", newline="") as f:
        csv.writer(f).writerows(rows)
    _rewrite_manifest_md5s(p)
    rep = validate_day(root, DATE)
    assert rep["checks"]["V10"]["status"] == "FAIL"
    assert any(dropped[1] in v for v in rep["checks"]["V10"]["violations"])
    assert rep["checks"]["V2"]["status"] == "FAIL"      # counts disagree too
    assert rep["checks"]["V3"]["status"] == "PASS"      # others unaffected


def test_independence_multiple_defects_all_reported(tmp_path):
    """One failing check never masks another: seed a V2 defect AND a V4
    defect in the same day — both must be reported FAIL in one run."""
    root = str(tmp_path)
    p = _write_base_day(root)

    def neg(arr):
        arr["ask_qty_e4"][0][0] = -5
    _tamper_bin(p, neg)                                 # V4 defect
    with open(p["manifest"]) as f:
        man = json.load(f)
    man["record_count"] += 2                            # V2 defect
    with open(p["manifest"], "w") as f:
        json.dump(man, f, indent=1, sort_keys=True)
    rep = validate_day(root, DATE)
    assert rep["checks"]["V2"]["status"] == "FAIL"
    assert rep["checks"]["V4"]["status"] == "FAIL"
    assert rep["verdict"] == "QUARANTINE"


def test_missing_day_fails_every_check_no_lying_green(tmp_path):
    """A day with no files must not show ANY green row (D2: a green check
    that inspected nothing is a lie)."""
    rep = validate_day(str(tmp_path), DATE)
    for c in CHECKS:
        assert rep["checks"][c]["status"] == "FAIL", c
    assert rep["verdict"] == "QUARANTINE"


# ------------------------------------------------------------------ quarantine

def test_quarantine_moves_partition_writes_report_never_overwrites(tmp_path):
    root = str(tmp_path)
    src = day_paths(root, DATE)["dir"]
    qdir = os.path.join(root, "quarantine", "date=%s" % DATE)
    _write_base_day(root)
    rep = validate_day(root, DATE)
    dst = quarantine_day(root, DATE, rep)
    assert dst == qdir and os.path.isdir(qdir)
    assert not os.path.exists(src)                      # MOVED, whole partition
    for key in ("bin", "markets", "trade_ids", "manifest"):
        name = os.path.basename(day_paths(root, DATE)[key])
        assert os.path.isfile(os.path.join(qdir, name)), name
    rp = os.path.join(qdir, "validation_report_%s.json" % DATE)
    with open(rp) as f:
        assert json.load(f)["date"] == DATE
    # collision: quarantining the same date again must suffix, not overwrite
    _write_base_day(root)
    dst2 = quarantine_day(root, DATE, rep)
    assert dst2 != qdir and os.path.isdir(dst2) and os.path.isdir(qdir)


# ------------------------------------------------------------------------- CLI

def test_cli_green_day_exit_zero_stays_in_place(tmp_path, capsys):
    root = str(tmp_path)
    _write_base_day(root)
    rc = main(["--root", root, "--date", DATE])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DAY GREEN" in out
    assert os.path.isdir(day_paths(root, DATE)["dir"])  # untouched


def test_cli_failed_day_quarantined_nonzero_exit(tmp_path, capsys):
    root = str(tmp_path)
    shutil.copytree(os.path.join(DEFECTS, "v4_negative_level", "date=%s" % DATE),
                    day_paths(root, DATE)["dir"])
    rc = main(["--root", root, "--date", DATE])
    out = capsys.readouterr().out
    assert rc != 0
    assert "QUARANTINED" in out and "V4" in out and "FAIL" in out
    assert not os.path.exists(day_paths(root, DATE)["dir"])
    qdir = os.path.join(root, "quarantine", "date=%s" % DATE)
    assert os.path.isdir(qdir)
    assert os.path.isfile(os.path.join(qdir, "validation_report_%s.json" % DATE))


def test_cli_no_quarantine_reports_but_leaves_day(tmp_path, capsys):
    root = str(tmp_path)
    shutil.copytree(os.path.join(DEFECTS, "v6_trade_lookahead", "date=%s" % DATE),
                    day_paths(root, DATE)["dir"])
    rc = main(["--root", root, "--date", DATE, "--no-quarantine"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "FAIL" in out
    assert os.path.isdir(day_paths(root, DATE)["dir"])  # left in place
    assert not os.path.exists(os.path.join(root, "quarantine"))


if __name__ == "__main__":
    print("regenerated defect fixture days: %s" % build_defect_days())
