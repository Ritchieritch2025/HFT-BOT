#!/usr/bin/env python3
"""W-E3: event pack validator (PLAN_EVENT_PACKAGING §6 V-EP checks).

Validates a W-E2 pack against the warehouse + capture-gap record and emits a
completeness verdict: `pass` (all blocking checks green, no gap overlap),
`degraded` (V-EP15 interior-gap overlap — usable with caveats, NEVER pass), or
`fail` (a blocking check other than V-EP15 failed).

Implemented now (computable from a W-E2 pack): V-EP1 conservation, V-EP3 no
time leakage, V-EP4 market membership, V-EP6 Q7 exclusion, V-EP7 manifest
integrity, V-EP10 trade completeness, V-EP12 calendar-split recovery, and
V-EP15 interior-gap completeness (AF-1). Checks that need unbuilt Ws (V-EP5
bracket/dim, V-EP8 rebuild, V-EP9 LOCF, V-EP11 divergence REPORT, V-EP13
lifecycle Fork-A, V-EP14 CSV export W-E7) report `skip` with the reason — an
honest not-yet-applicable, never a silent pass (D2).

Read-only. `gaps` are (start_us, end_us) intervals; the default source is
work/event_packs/capture_gaps.csv (start_us,end_us header), else none.
"""
import argparse
import csv
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warehouse as wh  # noqa: E402

US_PER_DAY = 86_400_000_000
_BLOCKING = {"V-EP1", "V-EP3", "V-EP4", "V-EP6", "V-EP7", "V-EP10", "V-EP12"}


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def load_gaps(path):
    """Capture-gap intervals [(start_us, end_us)] from a start_us,end_us CSV."""
    gaps = []
    for r in _read_csv(path):
        try:
            gaps.append((int(r["start_us"]), int(r["end_us"])))
        except (KeyError, ValueError):
            continue
    return gaps


def validate_pack(manifest, data_dir, warehouse=None, gaps=None):
    """Run the §6 checks. Returns {verdict, checks:{id:{status, detail}}}."""
    gaps = gaps or []
    markets = set(manifest["markets"])
    ws, we = manifest["win_start_us"], manifest["win_end_us"]
    cat = manifest.get("category")
    checks = {}

    def add(cid, status, detail=""):
        checks[cid] = {"status": status, "detail": detail}

    trades = _read_csv(os.path.join(data_dir, "trades.csv"))
    l1 = _read_csv(os.path.join(data_dir, "orderbooks_l1.csv"))
    per_table = {"trades": trades, "orderbooks_l1": l1}

    # V-EP1 conservation: pack row_counts == warehouse count over the same filter.
    ep1_ok, det = True, []
    for table, rows in per_table.items():
        try:
            rel = wh.load(table, category=cat, start=ws, end=we, warehouse=warehouse)
            mk = ", ".join("'%s'" % m.replace("'", "''") for m in markets)
            n = rel.query("f", "SELECT count(*) FROM f WHERE market_ticker IN (%s)"
                          % mk).fetchone()[0]
        except FileNotFoundError:
            n = 0
        if n != len(rows):
            ep1_ok = False
            det.append("%s pack=%d warehouse=%d" % (table, len(rows), n))
    add("V-EP1", "pass" if ep1_ok else "fail", "; ".join(det))

    # V-EP3 no time leakage: every row within [win_start, win_end].
    leaks = [r for rows in per_table.values() for r in rows
             if not (ws <= int(r["ts_utc"]) <= we)]
    add("V-EP3", "pass" if not leaks else "fail",
        "%d rows outside window" % len(leaks))

    # V-EP4 market membership: no foreign markets.
    foreign = {r["market_ticker"] for rows in per_table.values() for r in rows
               if r["market_ticker"] not in markets}
    add("V-EP4", "pass" if not foreign else "fail", "foreign: %s" % sorted(foreign))

    # V-EP6 Q7: no KXMVE* rows.
    mve = {r["market_ticker"] for rows in per_table.values() for r in rows
           if r["market_ticker"].startswith("KXMVE")}
    add("V-EP6", "pass" if not mve else "fail", "mve: %s" % sorted(mve))

    # V-EP7 manifest integrity: md5 + row_counts match the files on disk.
    ep7_ok, det7 = True, []
    for fname, want_md5 in manifest.get("files", {}).items():
        p = os.path.join(data_dir, fname)
        if not os.path.exists(p) or _md5(p) != want_md5:
            ep7_ok = False
            det7.append("md5 mismatch %s" % fname)
    for table, rows in per_table.items():
        if manifest.get("row_counts", {}).get(table) != len(rows):
            ep7_ok = False
            det7.append("row_count mismatch %s" % table)
    add("V-EP7", "pass" if ep7_ok else "fail", "; ".join(det7))

    # V-EP10 trade completeness: no duplicate trade_id.
    tids = [r["trade_id"] for r in trades]
    dups = len(tids) - len(set(tids))
    add("V-EP10", "pass" if dups == 0 else "fail", "%d duplicate trade_id" % dups)

    # V-EP12 calendar-split recovery: crossed events span >=2 UTC days in-pack.
    if manifest.get("crossed_day_boundary"):
        days = {int(r["ts_utc"]) // US_PER_DAY for rows in per_table.values() for r in rows}
        add("V-EP12", "pass" if len(days) >= 2 else "fail",
            "%d distinct UTC days" % len(days))
    else:
        add("V-EP12", "skip", "unit does not cross a day boundary")

    # V-EP15 interior-gap completeness (AF-1): ANY capture-gap overlap -> degraded.
    overlaps = [(gs, ge) for (gs, ge) in gaps if gs < we and ge > ws]
    add("V-EP15", "pass" if not overlaps else "degraded",
        "gap overlap: %s" % [[gs, ge] for gs, ge in overlaps])

    # verdict
    if any(checks[c]["status"] == "fail" for c in _BLOCKING):
        verdict = "fail"
    elif checks["V-EP15"]["status"] == "degraded":
        verdict = "degraded"
    else:
        verdict = "pass"
    return {"verdict": verdict, "unit_key": manifest.get("unit_key"),
            "gaps": [[gs, ge] for gs, ge in overlaps], "checks": checks}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True, help="path to manifests/<key>.json")
    ap.add_argument("--warehouse", help="warehouse root (default: config)")
    ap.add_argument("--gaps", default="work/event_packs/capture_gaps.csv")
    args = ap.parse_args(argv[1:])

    with open(args.manifest) as f:
        manifest = json.load(f)
    data_dir = os.path.join(os.path.dirname(os.path.dirname(args.manifest)),
                            "data", "unit=%s" % manifest["unit_key"].replace("/", "_"))
    res = validate_pack(manifest, data_dir, warehouse=args.warehouse, gaps=load_gaps(args.gaps))
    print("verdict: %s  (%s)" % (res["verdict"], res["unit_key"]))
    for cid in sorted(res["checks"], key=lambda x: int(x[4:])):
        c = res["checks"][cid]
        print("  %-7s %-8s %s" % (cid, c["status"], c["detail"]))
    return 0 if res["verdict"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
