#!/usr/bin/env python3
"""W-TL1 jitter / pacing-residual report over RAW capture envelopes.

DATA SOURCE IS PINNED: this tool reads raw NDJSON envelope files (or local
fixtures shaped like them) ONLY. It must never compute from warehouse tables —
stream_epoch / connection boundaries exist ONLY in the raw envelope, and a
reconnect-aware residual computed from the warehouse is unverifiable
(acceptance FAIL by W-TL1 definition).

Metrics (all microseconds):
  lag_us       = local_recv_ts_us - exchange_ts_us. DIAGNOSTIC observed-lag
                 PROXY only, never absolute network latency (Kalshi ts
                 semantics are undocumented; the two clocks are not perfectly
                 calibrated). A NEGATIVE lag has exactly one correct reading:
                 the exchange clock runs ahead of ours / ours behind — never
                 faster-than-light transport.
  jitter_us    = lag_p99 - lag_p50, per hourly window (the report's row grain
                 is (capture_host, category, table, hour_utc)).
  pacing_residual_us[i] = (recv_mono_ns[i]-recv_mono_ns[i-1])/1000
                          - (exchange_ts_us[i]-exchange_ts_us[i-1])
                 over CONSECUTIVE-BY-ARRIVAL records within one
                 (file, stream_epoch, market_ticker, table) chain. Never
                 differenced across stream_epoch/reconnects; file (rotation
                 shard) boundaries also break chains — conservative: a few
                 pairs are lost, none are wrong. A record missing mono or
                 exchange breaks the chain (arrival adjacency would be
                 violated by bridging over it). delta_exchange < 0 records
                 enter no residual and are counted in
                 exchange_out_of_order_count. Batch bursts (positive spike
                 then a run of negatives) are annotated in batch_pattern_runs
                 (heuristic: residual > +1000us followed by >=2 consecutive
                 < -100us).

capture_host is DECLARED PROVENANCE (--capture-host, required): the caller
states what the file paths / deployment metadata prove. It is NEVER inferred
from timestamps (forbidden). Rules:
  ec2             provable EC2-box capture, EC2-exclusive era
                  (>= 2026-07-09T23:09:07Z). The ONLY host class whose rows
                  may feed formal calibration / latency / toxicity / shadow
                  gate / go-no-go.
  mac-precutover  provable pre-cutover Mac capture (development/sanity only;
                  the Mac clock carried hundreds-of-ms skew).
  unknown_overlap cannot prove which host (overlap window) — excluded from
                  go/no-go by definition.
  fixture         synthetic/local test data.

Scheduled-heartbeat records (envelope "scheduled_heartbeat": true or
channel == "heartbeat"; production raw never contains them — ingest
synthesizes heartbeats downstream) are excluded from every statistic and
counted in heartbeat_count_excluded.

W-TL1 acceptance runs on local synthetic/raw fixtures only; the production
EC2 report is generated after deployment (not part of this W).

Usage:
  python3 work/research/jitter_report.py --capture-host fixture \
      [--out work/research/jitter_report.csv] FILE.ndjson [...]
"""
import argparse
import csv
import datetime
import json
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ingest  # noqa: E402  (single source of truth for ts parsing rules)
import warehouse_common as wc  # noqa: E402

FOOTNOTE = ("Kalshi exchange timestamp is millisecond-granular. Sub-ms "
            "conclusions are not supported. Sub-1ms variation in the EC2 era "
            "is primarily timestamp quantization / upstream batching "
            "behavior, not transport jitter.")
GONOGO_NOTE = ("Only capture_host=ec2 rows (EC2-exclusive era, from "
               "2026-07-09T23:09:07Z) may feed formal calibration/latency/"
               "toxicity/shadow-gate/go-no-go; all other host classes are "
               "development/fixture/sanity only.")

COLUMNS = ["capture_host", "category", "table", "hour_utc", "sample_count",
           "lag_p50", "lag_p90", "lag_p99", "lag_min", "lag_max", "jitter_us",
           "residual_sample_count", "residual_p50", "residual_p90",
           "residual_p99", "residual_min", "residual_max",
           "missing_exchange_pct", "missing_recv_pct", "negative_lag_pct",
           "exchange_out_of_order_count", "heartbeat_count_excluded",
           "batch_pattern_runs"]

TABLE_OF_TYPE = {"ticker": "orderbooks_l1", "trade": "trades",
                 "orderbook_snapshot": "orderbooks_full",
                 "orderbook_delta": "orderbooks_full"}

BATCH_SPIKE_US = 1000     # heuristic annotation thresholds (documented above)
BATCH_NEG_US = -100
BATCH_MIN_RUN = 2


def nearest_rank(sorted_vals, p):
    if not sorted_vals:
        return None
    return sorted_vals[max(0, math.ceil(p / 100.0 * len(sorted_vals)) - 1)]


def load_categories():
    """series_ticker -> category from the pinned classification dim (a catalog
    LABEL lookup, not a warehouse-facts computation). Missing -> {} and every
    series labels _unclassified (fixtures)."""
    pq = os.path.join(wc.load_config()["warehouse_root"], "catalog",
                      "series_classified", "part-00000.parquet")
    if not os.path.exists(pq):
        return {}
    import duckdb
    return {s: c for s, c in duckdb.sql(
        "SELECT series_ticker, category FROM read_parquet('%s')"
        % pq.replace("'", "''")).fetchall()}


class Bucket:
    __slots__ = ("samples", "lags", "residuals", "miss_exch", "miss_recv",
                 "neg_lag", "ooo", "heartbeats", "batch_runs")

    def __init__(self):
        self.samples = 0
        self.lags = []
        self.residuals = []
        self.miss_exch = self.miss_recv = self.neg_lag = 0
        self.ooo = self.heartbeats = 0
        self.batch_runs = 0


def is_heartbeat(rec):
    return rec.get("scheduled_heartbeat") is True or rec.get("channel") == "heartbeat"


def build_report(files, capture_host, categories):
    """-> (rows sorted, n_unparseable). One row per (host, category, table,
    hour of local recv — exchange hour if recv missing, 'unknown' if both)."""
    buckets = {}
    chains = {}      # (file, stream_epoch, market_ticker, table) -> chain state
    bad = 0

    def bkt(key):
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = Bucket()
        return b

    for path in files:
        with open(path, "rb") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    frame = json.loads(rec["raw"]) if "raw" in rec else rec
                    msg = frame.get("msg", {})
                except (ValueError, KeyError, TypeError):
                    bad += 1
                    continue
                typ = frame.get("type") or rec.get("channel")
                mt = (msg.get("market_ticker") or msg.get("ticker")
                      or rec.get("source_ticker"))
                table = TABLE_OF_TYPE.get(typ)
                exch = ingest.exchange_ts_us(msg)
                _, mono, local = ingest.recv_ladder(rec)
                cat = categories.get(str(mt).split("-", 1)[0], None) \
                    if mt else None
                cat = cat or "_unclassified"
                t_ref = local if local is not None else exch
                hour = (datetime.datetime.fromtimestamp(
                    t_ref / 1e6, tz=datetime.timezone.utc)
                    .strftime("%Y-%m-%dT%H")) if t_ref is not None else "unknown"
                key = (capture_host, cat, table or "_other", hour)
                b = bkt(key)

                if is_heartbeat(rec):
                    b.heartbeats += 1     # excluded from every statistic
                    continue
                if table is None or not mt:
                    bad += 1
                    continue
                b.samples += 1
                if exch is None:
                    b.miss_exch += 1
                if local is None:
                    b.miss_recv += 1
                if exch is not None and local is not None:
                    lag = local - exch
                    b.lags.append(lag)
                    if lag < 0:
                        b.neg_lag += 1

                # pacing residual: consecutive-by-arrival within one chain
                ck = (path, rec.get("stream_epoch"), mt, table)
                if mono is None or exch is None:
                    chains.pop(ck, None)   # adjacency broken — reset
                    continue
                prev = chains.get(ck)
                chains[ck] = {"mono": mono, "exch": exch,
                              "spike": prev.get("spike", False) if prev else False,
                              "negrun": prev.get("negrun", 0) if prev else 0}
                if prev is None:
                    continue
                d_exch = exch - prev["exch"]
                if d_exch < 0:
                    b.ooo += 1             # out-of-order: no residual
                    chains[ck]["spike"], chains[ck]["negrun"] = False, 0
                    continue
                r = (mono - prev["mono"]) // 1000 - d_exch
                b.residuals.append(r)
                # batch-burst annotation (heuristic, see module docstring)
                st = chains[ck]
                if r > BATCH_SPIKE_US:
                    st["spike"], st["negrun"] = True, 0
                elif st["spike"] and r < BATCH_NEG_US:
                    st["negrun"] += 1
                    if st["negrun"] == BATCH_MIN_RUN:
                        b.batch_runs += 1
                else:
                    st["spike"], st["negrun"] = False, 0

    rows = []
    for (host, cat, table, hour), b in sorted(buckets.items()):
        lags = sorted(b.lags)
        res = sorted(b.residuals)
        p50, p99 = nearest_rank(lags, 50), nearest_rank(lags, 99)
        n_lag = len(lags)
        rows.append({
            "capture_host": host, "category": cat, "table": table,
            "hour_utc": hour, "sample_count": b.samples,
            "lag_p50": p50, "lag_p90": nearest_rank(lags, 90), "lag_p99": p99,
            "lag_min": lags[0] if lags else None,
            "lag_max": lags[-1] if lags else None,
            "jitter_us": (p99 - p50) if lags else None,
            "residual_sample_count": len(res),
            "residual_p50": nearest_rank(res, 50),
            "residual_p90": nearest_rank(res, 90),
            "residual_p99": nearest_rank(res, 99),
            "residual_min": res[0] if res else None,
            "residual_max": res[-1] if res else None,
            "missing_exchange_pct": round(100.0 * b.miss_exch / b.samples, 2)
            if b.samples else None,
            "missing_recv_pct": round(100.0 * b.miss_recv / b.samples, 2)
            if b.samples else None,
            "negative_lag_pct": round(100.0 * b.neg_lag / n_lag, 2)
            if n_lag else None,
            "exchange_out_of_order_count": b.ooo,
            "heartbeat_count_excluded": b.heartbeats,
            "batch_pattern_runs": b.batch_runs,
        })
    return rows, bad


def write_csv(rows, out_path):
    with open(out_path, "w", newline="") as f:
        f.write("# %s\n" % FOOTNOTE)
        f.write("# %s\n" % GONOGO_NOTE)
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r[k] is None else r[k]) for k in COLUMNS})


def main(argv):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture-host", required=True,
                    choices=("ec2", "mac-precutover", "unknown_overlap", "fixture"),
                    help="DECLARED provenance of ALL input files (see module "
                         "docstring; never inferred from timestamps)")
    ap.add_argument("--out", default=None, help="CSV output path "
                    "(default work/research/jitter_report_<host>.csv)")
    ap.add_argument("files", nargs="+", help="raw NDJSON envelope files / fixtures")
    args = ap.parse_args(argv[1:])

    rows, bad = build_report(args.files, args.capture_host, load_categories())
    out = args.out or os.path.join(ROOT, "work", "research",
                                   "jitter_report_%s.csv" % args.capture_host)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    write_csv(rows, out)

    print("FOOTNOTE: %s" % FOOTNOTE)
    print("NOTE: %s" % GONOGO_NOTE)
    if args.capture_host != "ec2":
        print("NOTE: capture_host=%s -> this report is development/sanity "
              "only, NOT usable for go/no-go." % args.capture_host)
    if bad:
        print("WARN: %d record(s) unparseable/unattributable — counted, "
              "never silently dropped (D2)" % bad)
    print("rows: %d bucket(s) -> %s" % (len(rows), out))
    for r in rows:
        print("  %-16s %-20s %-15s %s n=%-6d lag_p50=%s jitter=%s resid_n=%d"
              % (r["capture_host"], r["category"], r["table"], r["hour_utc"],
                 r["sample_count"], r["lag_p50"], r["jitter_us"],
                 r["residual_sample_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
