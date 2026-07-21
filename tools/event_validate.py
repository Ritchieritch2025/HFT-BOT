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


_ALL_CHECKS = ["V-EP1", "V-EP3", "V-EP4", "V-EP6", "V-EP7", "V-EP10", "V-EP12", "V-EP15"]
# Checks that need a not-yet-built W — emitted as explicit `skip` (never omitted,
# never silent-pass — D2). Reason states the blocking dependency.
_DEFERRED = {
    "V-EP2": "subsumed by V-EP12 for crossed units; standalone check with W-E5",
    "V-EP5": "bracket/ME coverage needs the dim (W-E5)",
    "V-EP8": "idempotent-rebuild covered by W-E2 test; standalone with W-E5",
    "V-EP9": "LOCF reconstruct needs ffill compare (W-E3.1)",
    "V-EP11": "divergence is REPORT-only; surfaced from the index",
    "V-EP13": "lifecycle anchor needs Fork-A capture (W-LC)",
    "V-EP14": "CSV export fidelity needs the exporter (W-E7)",
}


def load_gaps(path):
    """Capture-gap intervals [(start_us, end_us)] from a start_us,end_us CSV.
    Returns (gaps, available): available=False when the file is absent, so the
    caller can fail-closed (absence of a gap record ≠ evidence of no gap — D2)."""
    if not os.path.exists(path):
        return [], False
    gaps = []
    for r in _read_csv(path):
        try:
            gaps.append((int(r["start_us"]), int(r["end_us"])))
        except (KeyError, ValueError):
            continue
    return gaps, True


def gaps_from_quality_log(path):
    """Best-effort capture-gap intervals from quality_log.ndjson: entries whose
    finding/action mention a capture gap / data loss / outage AND carry a parseable
    'YYYY-MM-DDThh:mm-hh:mm[Z]' window. Returns (gaps, available). Coarse (minute
    resolution, same-day) — a structured gap record is the durable source (BACKLOG)."""
    import re
    if not os.path.exists(path):
        return [], False
    import datetime as _dt
    import calendar as _cal
    gaps = []
    rex = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})\s*[-–]\s*(\d{2}):(\d{2})")
    for r in _read_csv_lines(path):
        blob = (r.get("finding", "") + " " + r.get("action", "") + " " + r.get("window", "")).lower()
        if not any(k in blob for k in ("gap", "data_loss", "data loss", "outage", "lockout", "lid-close")):
            continue
        m = rex.search(r.get("window", "") or "")
        if not m:
            continue
        d, h1, m1, h2, m2 = m.groups()
        base = _dt.datetime.strptime(d, "%Y-%m-%d")
        s = int(_cal.timegm((base.replace(hour=int(h1), minute=int(m1))).timetuple()) * 1_000_000)
        e = int(_cal.timegm((base.replace(hour=int(h2), minute=int(m2))).timetuple()) * 1_000_000)
        if e > s:
            gaps.append((s, e))
    return gaps, True


def _read_csv_lines(path):
    import json as _json
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(_json.loads(line))
                except ValueError:
                    continue
    return out


def validate_pack(manifest, data_dir, warehouse=None, gaps=None, gaps_available=True):
    """Run the §6 checks. Returns {verdict, checks:{id:{status, detail}}}.

    gaps_available=False (no capture-gap record) makes V-EP15 fail-closed: it
    cannot certify no interior hole, so the verdict can never be `pass` (D2)."""
    gaps = gaps or []
    markets = set(manifest["markets"])
    ws, we = manifest["win_start_us"], manifest["win_end_us"]
    cat = manifest.get("category")
    checks = {}

    def add(cid, status, detail=""):
        checks[cid] = {"status": status, "detail": detail}

    # fail-closed guards (Defect-4/5): a null window or empty market set is not
    # validatable — abort cleanly as `fail`, never crash.
    if ws is None or we is None:
        add("window", "fail", "null win_start_us/win_end_us")
        return {"verdict": "fail", "unit_key": manifest.get("unit_key"), "gaps": [], "checks": checks}
    if not markets:
        add("V-EP4", "fail", "empty market set")
        return {"verdict": "fail", "unit_key": manifest.get("unit_key"), "gaps": [], "checks": checks}

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

    # V-EP15 interior-gap completeness (AF-1). Fail-closed: with no gap record we
    # CANNOT certify no interior hole -> skip + block `pass` (absence of evidence
    # is not evidence of no gap, D2).
    overlaps = [(gs, ge) for (gs, ge) in gaps if gs < we and ge > ws]
    if not gaps_available:
        add("V-EP15", "skip", "capture-gap record unavailable — completeness uncertified")
    else:
        add("V-EP15", "pass" if not overlaps else "degraded",
            "gap overlap: %s" % [[gs, ge] for gs, ge in overlaps])

    # deferred checks: explicit skip with the blocking dependency — never omitted,
    # never silently counted as pass (D2).
    for cid, why in _DEFERRED.items():
        add(cid, "skip", why)

    # verdict: any blocking fail -> fail; interior-gap overlap OR an uncertified
    # gap record -> degraded (never pass); else pass.
    if any(checks[c]["status"] == "fail" for c in _BLOCKING):
        verdict = "fail"
    elif checks["V-EP15"]["status"] in ("degraded", "skip"):
        verdict = "degraded"
    else:
        verdict = "pass"
    return {"verdict": verdict, "unit_key": manifest.get("unit_key"),
            "gaps": [[gs, ge] for gs, ge in overlaps], "checks": checks}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True, help="path to manifests/<key>.json")
    ap.add_argument("--warehouse", help="warehouse root (default: config)")
    ap.add_argument("--gaps", help="structured start_us,end_us CSV (overrides quality-log)")
    ap.add_argument("--quality-log", default="work/quality_log.ndjson",
                    help="capture-gap source when --gaps is not given")
    args = ap.parse_args(argv[1:])

    with open(args.manifest) as f:
        manifest = json.load(f)
    data_dir = os.path.join(os.path.dirname(os.path.dirname(args.manifest)),
                            "data", "unit=%s" % manifest["unit_key"].replace("/", "_"))
    gaps, available = (load_gaps(args.gaps) if args.gaps
                       else gaps_from_quality_log(args.quality_log))
    res = validate_pack(manifest, data_dir, warehouse=args.warehouse,
                        gaps=gaps, gaps_available=available)
    print("verdict: %s  (%s)" % (res["verdict"], res["unit_key"]))
    for cid in sorted(res["checks"], key=lambda x: int(x[4:])):
        c = res["checks"][cid]
        print("  %-7s %-8s %s" % (cid, c["status"], c["detail"]))
    return 0 if res["verdict"] != "fail" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
