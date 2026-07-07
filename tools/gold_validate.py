"""W2.5 validator harness (PLAN_GOLD_DATA_CONTRACT §2.3 + W2.5) — runs V2,
V3, V4, V6, V9, V10 over one written gold day partition, INDEPENDENTLY, plus
the V8-shape trade-hash reconciliation (gold_io.reconcile_trade_hashes, wired
per the W2.4 audit finding: never dormant). ALL pass => GREEN (exit 0); any
fail => QUARANTINE (exit 1). Design decisions (E2):
  - Independence: each check reports its own violations; an exception inside
    one check is caught as THAT check's failure, never masking others. All
    record access goes through RawDay, an md5-BLIND reader, so a V2 md5/
    manifest failure cannot stop V3-V10. V2 verifies explicitly (md5 of every
    manifest-listed file; record/trade/market counts vs parsed rows) AND
    instantiates the strict GoldDayReader so its refusals are REPORTED.
  - V3/V6 REUSE gold_merge.v3_violations/v6_violations verbatim on shims
    built from .bin rows; "minted" is inferred as "book_seq exceeded this
    market's previous value" — sound, not circular: stream_seq holes, ts
    regressions, book_seq gaps/dups/decreases, and trades referencing a
    not-yet-emitted mint all still violate (V9 owns heartbeats explicitly).
  - Quarantine choice (per W2.5): MOVE the whole day dir to <root>/
    quarantine/date=<D>/ (collision => date=<D>.N; nothing deleted or
    overwritten, P6) + validation_report_<D>.json written inside. Moving
    beats a marker: the green tree can no longer resolve the day by path
    (S2 fail-closed) and the partition stays byte-intact for forensics.
  - Reports cap examples at VIOL_SAMPLE per check but always carry the FULL
    violation count (D2: output bounded, never hidden). stdlib + numpy only.
"""
import argparse
import json
import csv
import os
import shutil
import sys
from collections import namedtuple

import numpy as np

try:
    from tools.gold_dtype import EVENT_TYPE, FLAGS, GOLD_DTYPE
    from tools.gold_fsm import APPLIED, NEUTRAL
    from tools.gold_io import (GoldDayReader, GoldIOError, _check_date, _md5,
                               day_paths, reconcile_trade_hashes)
    from tools.gold_merge import v3_violations, v6_violations
except ImportError:  # imported as a plain module from tools/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gold_dtype import EVENT_TYPE, FLAGS, GOLD_DTYPE
    from gold_fsm import APPLIED, NEUTRAL
    from gold_io import (GoldDayReader, GoldIOError, _check_date, _md5,
                         day_paths, reconcile_trade_hashes)
    from gold_merge import v3_violations, v6_violations

CHECKS = ("V2", "V3", "V4", "V6", "V8", "V9", "V10")
VIOL_SAMPLE = 12                     # examples shown per check; total always kept
_TRADE, _HB = EVENT_TYPE["TRADE"], EVENT_TYPE["HEARTBEAT"]
_SNAP = EVENT_TYPE["BOOK_SNAPSHOT"]
_STATE_FIELDS = ("flags", "bid_px_e4", "ask_px_e4", "bid_qty_e4", "ask_qty_e4",
                 "bid_rest_qty_e4", "ask_rest_qty_e4", "bid_nlevels", "ask_nlevels")

_Shim = namedtuple("_Shim", "stream_seq ts_us kind market_ticker book_seq fsm_result")


class RawDay(object):
    """md5-blind partition access, so one integrity failure (V2's job to
    report) never prevents the other checks from inspecting the records."""

    def __init__(self, root, date):
        _check_date(date)
        self.root, self.date = root, date
        self.paths = p = day_paths(root, date)
        self.errors, self.bin_errors = [], []   # errors: V2; bin_errors: V3-V10
        self.manifest = None
        if os.path.isfile(p["manifest"]):
            try:
                with open(p["manifest"]) as f:
                    self.manifest = json.load(f)
            except ValueError as e:
                self.errors.append("manifest unparseable: %s" % e)
        else:
            self.errors.append("manifest missing: %s" % p["manifest"])
        self.records = np.zeros(0, dtype=GOLD_DTYPE)
        if os.path.isfile(p["bin"]):
            n, tail = divmod(os.path.getsize(p["bin"]), GOLD_DTYPE.itemsize)
            if tail:
                self.bin_errors.append(
                    "gold .bin has %d trailing bytes (torn record)" % tail)
            if n:
                self.records = np.memmap(p["bin"], dtype=GOLD_DTYPE, mode="r",
                                         shape=(int(n),))
        else:
            self.bin_errors.append("gold .bin missing: %s" % p["bin"])
        self.errors += self.bin_errors
        self._by_id, self._trade_uuids = {}, {}
        for key, sink, cols in (("markets", self._by_id,
                                 ("market_id", "market_ticker")),
                                ("trade_ids", self._trade_uuids,
                                 ("stream_seq", "trade_id"))):
            if not os.path.isfile(p[key]):
                self.errors.append("%s sidecar missing: %s" % (key, p[key]))
                continue
            with open(p[key], newline="") as f:
                for row in csv.DictReader(f):
                    sink[int(row[cols[0]])] = row[cols[1]]

    def ticker(self, market_id):
        return self._by_id.get(market_id, "market_id=%d" % market_id)

    def trade_uuid(self, stream_seq):
        return self._trade_uuids.get(stream_seq)


def bin_shims(raw):
    """.bin rows -> shims for the reused gold_merge checkers (see docstring:
    mint inference is sound for V3/V6, and V9 owns heartbeats explicitly)."""
    out, last, r = [], {}, raw.records
    for i in range(len(r)):
        mid, bs = int(r["market_id"][i]), int(r["book_seq"][i])
        res = APPLIED if bs > last.get(mid, 0) else NEUTRAL
        last[mid] = bs
        out.append(_Shim(int(r["stream_seq"][i]), int(r["ts_us"][i]),
                         int(r["event_type"][i]), raw.ticker(mid), bs, res))
    return out


# ------------------------------------------------------------------ the checks

def check_v2(raw):
    """Manifest reconciliation: explicit md5 + count verification, PLUS the
    strict reader's own open-time gate surfaced as reported violations."""
    v, m, p = list(raw.errors), raw.manifest, raw.paths
    if m is not None:
        listed = m.get("files", {})
        for k in ("bin", "markets", "trade_ids"):
            if os.path.basename(p[k]) not in listed:
                v.append("manifest does not list %s" % os.path.basename(p[k]))
        for name, want in sorted(listed.items()):
            path = os.path.join(p["dir"], name)
            if not os.path.isfile(path):
                v.append("manifest-listed file missing: %s" % name)
            elif _md5(path) != want:
                v.append("md5 mismatch for %s: manifest %s != file %s"
                         % (name, want, _md5(path)))
        n = len(raw.records)
        n_tr = int((raw.records["event_type"] == _TRADE).sum())
        for field, got in (("record_count", n), ("trade_count", n_tr),
                           ("trade_count", len(raw._trade_uuids)),
                           ("markets", len(raw._by_id))):
            if m.get(field) != got:
                v.append("manifest %s=%s != %d parsed" % (field, m.get(field), got))
    try:
        GoldDayReader(raw.root, raw.date)
    except GoldIOError as e:
        v.append("GoldDayReader refused: %s" % e)
    return v


def check_v3(raw):
    return raw.bin_errors + v3_violations(bin_shims(raw))


def check_v6(raw):
    return raw.bin_errors + v6_violations(bin_shims(raw))


def check_v4(raw):
    """Book integrity on emitted records: zero negative levels; invalid =>
    zeroed arrays; crossed flagged, never repaired."""
    v, r = list(raw.bin_errors), raw.records
    if not len(r):
        return v, {}
    neg = (r["bid_rest_qty_e4"] < 0) | (r["ask_rest_qty_e4"] < 0)
    for f in ("bid_px_e4", "ask_px_e4", "bid_qty_e4", "ask_qty_e4"):
        neg |= (r[f] < 0).any(axis=1)
    for i in np.nonzero(neg)[0]:
        v.append("negative book level emitted at stream_seq %d" % r["stream_seq"][i])
    invalid = (r["flags"] & FLAGS["F_BOOK_VALID"]) == 0
    dirty = (r["bid_nlevels"] != 0) | (r["ask_nlevels"] != 0) | \
            (r["bid_rest_qty_e4"] != 0) | (r["ask_rest_qty_e4"] != 0)
    for f in ("bid_px_e4", "ask_px_e4", "bid_qty_e4", "ask_qty_e4"):
        dirty |= r[f].any(axis=1)
    for i in np.nonzero(invalid & dirty)[0]:
        v.append("Invalid Book State at stream_seq %d serves non-zero book "
                 "data (invalid must mean zeroed arrays)" % r["stream_seq"][i])
    both = (r["bid_nlevels"] > 0) & (r["ask_nlevels"] > 0) & ~invalid
    crossed = both & (r["bid_px_e4"][:, 0] >= r["ask_px_e4"][:, 0])
    flagged = (r["flags"] & FLAGS["F_CROSSED"]) != 0
    for i in np.nonzero(crossed & ~flagged)[0]:
        v.append("crossed book NOT flagged F_CROSSED at stream_seq %d "
                 "(silent repair?)" % r["stream_seq"][i])
    for i in np.nonzero(flagged & ~crossed)[0]:
        v.append("F_CROSSED at stream_seq %d but book is not crossed "
                 "(flagged books stay unrepaired)" % r["stream_seq"][i])
    return v, {"records": len(r), "invalid_records": int(invalid.sum()),
               "crossed_flagged": int(flagged.sum())}


def check_v8(raw):
    v = list(raw.bin_errors)
    if not os.path.isfile(raw.paths["trade_ids"]):
        v.append("trade_ids sidecar missing — V8 reconciliation impossible")
    return v + reconcile_trade_hashes(raw)


def check_v9(raw):
    """Heartbeat neutrality: no book_seq advance, no state diff vs the
    previous record of the same market (zeros if none)."""
    v, r, prev, n_hb = list(raw.bin_errors), raw.records, {}, 0
    for i in range(len(r)):
        mid = int(r["market_id"][i])
        if int(r["event_type"][i]) == _HB:
            n_hb += 1
            j = prev.get(mid)
            want_bs = int(r["book_seq"][j]) if j is not None else 0
            if int(r["book_seq"][i]) != want_bs:
                v.append("heartbeat at stream_seq %d advances book_seq "
                         "%d -> %d (%s)" % (r["stream_seq"][i], want_bs,
                                            r["book_seq"][i], raw.ticker(mid)))
            if j is None:
                if any(np.any(r[f][i]) for f in _STATE_FIELDS):
                    v.append("heartbeat at stream_seq %d has non-zero state "
                             "with no prior record (%s)"
                             % (r["stream_seq"][i], raw.ticker(mid)))
            elif any(not np.array_equal(r[f][i], r[f][j]) for f in _STATE_FIELDS):
                v.append("heartbeat at stream_seq %d diffs book state vs "
                         "previous record of %s (stream_seq %d)"
                         % (r["stream_seq"][i], raw.ticker(mid),
                            r["stream_seq"][j]))
        prev[mid] = i
    return v, {"heartbeats": n_hb}


def check_v10(raw):
    """Coverage honesty: traded-but-uncovered count REPORTED (not a failure
    by itself); such records carry F_BOOK_COVERED=0; covered markets keep the
    flag from first snapshot on; .bin ids reconcile 1:1 with the sidecar
    (no silent inner-join shrinkage)."""
    v, r = list(raw.bin_errors), raw.records
    mid, et = r["market_id"], r["event_type"]
    covered_flag = (r["flags"] & FLAGS["F_BOOK_COVERED"]) != 0
    ids_bin = set(int(x) for x in np.unique(mid))
    traded = set(int(x) for x in np.unique(mid[et == _TRADE]))
    snap_first = {}
    for i in np.nonzero(et == _SNAP)[0]:
        snap_first.setdefault(int(mid[i]), int(i))
    uncovered = sorted(ids_bin - set(snap_first))
    for i in np.nonzero(covered_flag & np.isin(mid, uncovered))[0]:
        v.append("record at stream_seq %d flags F_BOOK_COVERED but market %s "
                 "had no snapshot this day (coverage lie)"
                 % (r["stream_seq"][i], raw.ticker(int(mid[i]))))
    for m, i0 in sorted(snap_first.items()):
        idx = np.nonzero((mid == m) & ~covered_flag)[0]
        for i in idx[idx >= i0]:
            v.append("covered market %s record at stream_seq %d lost "
                     "F_BOOK_COVERED (coverage shrinkage)"
                     % (raw.ticker(m), r["stream_seq"][i]))
    ids_side = set(raw._by_id)
    for m in sorted(ids_bin - ids_side):
        v.append(".bin market_id %d (%s) absent from markets sidecar "
                 "(inner-join shrinkage)" % (m, raw.ticker(m)))
    for m in sorted(ids_side - ids_bin):
        v.append("sidecar market %s (id %d) has no .bin records"
                 % (raw.ticker(m), m))
    return v, {"markets_bin": len(ids_bin), "markets_sidecar": len(ids_side),
               "traded_markets": len(traded),
               "traded_but_uncovered": len(traded - set(snap_first))}


_CHECK_FNS = (("V2", check_v2), ("V3", check_v3), ("V4", check_v4),
              ("V6", check_v6), ("V8", check_v8), ("V9", check_v9),
              ("V10", check_v10))


# ----------------------------------------------------------------- the harness

def validate_day(root, date):
    """Run every check INDEPENDENTLY; report all; never drop anything."""
    raw = RawDay(root, date)
    checks = {}
    for name, fn in _CHECK_FNS:
        try:
            res = fn(raw)
        except Exception as e:      # independence: a crash is THIS check's fail
            res = ["check crashed: %r" % (e,)]
        viol, counts = res if isinstance(res, tuple) else (res, {})
        checks[name] = {"status": "PASS" if not viol else "FAIL",
                        "violations_total": len(viol),
                        "violations": [str(x) for x in viol[:VIOL_SAMPLE]],
                        "counts": counts}
    verdict = ("GREEN" if all(c["status"] == "PASS" for c in checks.values())
               else "QUARANTINE")
    return {"date": date, "root": root, "verdict": verdict, "checks": checks}


def quarantine_day(root, date, report):
    """MOVE the whole partition to <root>/quarantine/date=<D>[.N] + write the
    report inside it (module docstring: move, never delete/overwrite)."""
    src = day_paths(root, date)["dir"]
    qroot = os.path.join(root, "quarantine")
    dst, n = os.path.join(qroot, "date=%s" % date), 0
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(qroot, "date=%s.%d" % (date, n))
    os.makedirs(qroot, exist_ok=True)
    if os.path.isdir(src):
        shutil.move(src, dst)
    else:                            # nothing on disk: still record the verdict
        os.makedirs(dst)
    with open(os.path.join(dst, "validation_report_%s.json" % date), "w") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    return dst


def format_report(report):
    lines = ["gold_validate %s (root=%s)" % (report["date"], report["root"]),
             "%-5s %-6s %-10s counts" % ("check", "status", "violations")]
    for name in CHECKS:
        c = report["checks"][name]
        lines.append("%-5s %-6s %-10d %s" % (name, c["status"],
                                             c["violations_total"],
                                             c["counts"] or ""))
        for s in c["violations"]:
            lines.append("      - " + s)
        if c["violations_total"] > len(c["violations"]):
            lines.append("      ... %d more (sample capped at %d)"
                         % (c["violations_total"] - len(c["violations"]),
                            VIOL_SAMPLE))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="W2.5 gold-day validator: V2,V3,V4,V6,V8,V9,V10 "
                    "independent checks; any failure quarantines the day.")
    ap.add_argument("--root", default="work/gold")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD partition date")
    ap.add_argument("--no-quarantine", action="store_true",
                    help="report + nonzero exit only; leave the day in place")
    a = ap.parse_args(argv)
    report = validate_day(a.root, a.date)
    print(format_report(report))
    if report["verdict"] == "GREEN":
        print("DAY GREEN: all checks passed for %s" % a.date)
        return 0
    if a.no_quarantine:
        print("DAY FAILED validation for %s (left in place: --no-quarantine)"
              % a.date)
        return 1
    dst = quarantine_day(a.root, a.date, report)
    print("DAY QUARANTINED -> %s" % dst)
    return 1


if __name__ == "__main__":
    sys.exit(main())
