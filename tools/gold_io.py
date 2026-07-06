"""W2.4 gold writer/reader (PLAN_GOLD_DATA_CONTRACT §2.1/§2.2 output spec) —
W2.3 MergedRecord stream -> work/gold/date=<D>/gold_<D>.bin (512-B GOLD_DTYPE
records) + sidecars + manifest; np.memmap zero-copy reader; market_id
stability.

  - market_id: dense per-day ints (0-based, documented choice matching
    stream_seq) minted in first-appearance order over the merged stream.
    DAY-SCOPED per §2.1: the same ticker mints a different id on a different
    day. SAME-DAY 1:1 market_id<->ticker is ENFORCED — any violation (same
    ticker two ids, same id two tickers) is a BUILD FAILURE, loud, both at
    write time and again when the reader loads the sidecar (defense in depth).
  - sidecars: markets_<D>.csv market dim WITH a date column
    (date,market_id,market_ticker,category,close_time,liquidity_tier;
    markets missing from the dim mapping are KEPT with empty fields and
    counted — never dropped, V10 spirit); trade_ids_<D>.csv maps stream_seq
    -> full trade UUID (the .bin carries only fnv1a64(trade_id)).
  - manifest_<D>.json: record count, md5 of the gold file AND EVERY sidecar,
    builder version, source-day identifiers, day-level safety-verdict
    placeholders (V5/V7 land in W3.1/W3.2). Written LAST; the reader refuses
    to open a partition whose md5s do not match (mismatch = loud error).
  - cross-day joins on market_id alone are FORBIDDEN (§2.1): the reader is
    constructed per (root, date) so every access carries a date, and the
    cross-day helper requires a market_ticker — there is no market_id form.
  - reconcile_trade_hashes: V8 shape — for EVERY trade record,
    trade_id_hash in the .bin == fnv1a64(sidecar UUID at its stream_seq);
    missing/orphan sidecar rows are violations too.

Derived data only: work/gold/* is rebuildable and safe to delete (G8
GOLD_RETENTION_DAYS prunes ONLY these partitions, never raw/archive).
stdlib + numpy only.
"""
import csv
import hashlib
import json
import os
import re
import sys

import numpy as np

try:
    from tools.gold_dtype import (EVENT_TYPE, GOLD_DTYPE, GOLD_LAYOUT_VERSION,
                                  fnv1a64)
except ImportError:  # imported as a plain module from tools/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gold_dtype import EVENT_TYPE, GOLD_DTYPE, GOLD_LAYOUT_VERSION, fnv1a64

BUILDER_VERSION = "gold_io/W2.4-1 layout=%d" % GOLD_LAYOUT_VERSION
MARKET_ID_BASE = 0                 # documented choice: dense, 0-based per day
TAKER_SIDE = {"yes": 1, "no": 2}   # GoldRecord.taker_side; 0 = none (non-trade)
DIM_FIELDS = ("category", "close_time", "liquidity_tier")
MARKETS_COLS = ("date", "market_id", "market_ticker") + DIM_FIELDS
_TRADE = EVENT_TYPE["TRADE"]
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class GoldIOError(Exception):
    """Contract violation surfaced to callers — fail closed, never repaired."""


# ------------------------------------------------------------- pure helpers

def mint_market_ids(records):
    """Merged records -> {market_ticker: market_id}, dense ints minted in
    first-appearance order (§2.1: per-day ids; sidecar carries the dim)."""
    ids = {}
    for r in records:
        if r.market_ticker not in ids:
            ids[r.market_ticker] = MARKET_ID_BASE + len(ids)
    return ids


def market_id_violations(pairs):
    """Same-day 1:1 market_id<->ticker check over (market_id, ticker) pairs.
    Returns violation strings; empty list = pass. Both directions checked:
    one ticker under two ids AND one id claimed by two tickers."""
    by_ticker, by_id, out = {}, {}, []
    for mid, mt in pairs:
        if mt in by_ticker and by_ticker[mt] != mid:
            out.append("ticker %s minted under two market_ids: %d and %d"
                       % (mt, by_ticker[mt], mid))
        by_ticker.setdefault(mt, mid)
        if mid in by_id and by_id[mid] != mt:
            out.append("market_id %d claimed by two tickers: %s and %s"
                       % (mid, by_id[mid], mt))
        by_id.setdefault(mid, mt)
    return out


def _assert_bijection(pairs, where):
    v = market_id_violations(pairs)
    if v:
        raise GoldIOError(
            "BUILD FAILURE (%s): same-day 1:1 market_id<->ticker violated "
            "(§2.1): %s" % (where, "; ".join(v)))


def records_to_array(records, ids):
    """MergedRecord list + ticker->id mapping -> GOLD_DTYPE ndarray. Trade
    payload zero unless TRADE (§2.1); book arrays are the FSM state as-of
    each record, already yes-space/DEPTH-aggregated by W2.2."""
    arr = np.zeros(len(records), dtype=GOLD_DTYPE)
    for i, r in enumerate(records):
        row = arr[i]
        row["ts_us"] = r.ts_us
        row["stream_seq"] = r.stream_seq
        row["market_id"] = ids[r.market_ticker]
        row["event_type"] = r.kind
        row["flags"] = r.state.flags
        row["book_seq"] = r.book_seq
        if r.kind == _TRADE:
            t = r.event.payload
            row["trade_yes_price_e4"] = t.yes_price_e4
            row["taker_side"] = TAKER_SIDE[t.taker_side]
            row["trade_qty_e4"] = t.count_e4
            row["trade_id_hash"] = fnv1a64(t.trade_id)
        s = r.state
        if s.bid_nlevels > 0xFFFF or s.ask_nlevels > 0xFFFF:
            raise GoldIOError(
                "BUILD FAILURE: nlevels overflow at stream_seq %d (%s): "
                "bid_nlevels=%d ask_nlevels=%d exceed uint16 — refusing to "
                "truncate (D2)" % (r.stream_seq, r.market_ticker,
                                   s.bid_nlevels, s.ask_nlevels))
        row["bid_px_e4"], row["ask_px_e4"] = s.bid_px_e4, s.ask_px_e4
        row["bid_qty_e4"], row["ask_qty_e4"] = s.bid_qty_e4, s.ask_qty_e4
        row["bid_rest_qty_e4"] = s.bid_rest_qty_e4
        row["ask_rest_qty_e4"] = s.ask_rest_qty_e4
        row["bid_nlevels"], row["ask_nlevels"] = s.bid_nlevels, s.ask_nlevels
    return arr


def day_paths(root, date):
    d = os.path.join(root, "date=%s" % date)
    return {"dir": d,
            "bin": os.path.join(d, "gold_%s.bin" % date),
            "markets": os.path.join(d, "markets_%s.csv" % date),
            "trade_ids": os.path.join(d, "trade_ids_%s.csv" % date),
            "manifest": os.path.join(d, "manifest_%s.json" % date)}


def _check_date(date):
    if not isinstance(date, str) or not _DATE_RE.fullmatch(date):
        raise GoldIOError("date must be a YYYY-MM-DD string, got %r" % (date,))


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- the writer

def write_day(records, dim, date, root, source_ids=None):
    """Write one day partition: gold .bin + markets/trade_ids sidecars +
    manifest (written LAST). dim maps ticker -> {category, close_time,
    liquidity_tier}; markets missing from it are kept with empty fields and
    counted in the manifest (markets_missing_dim — surfaced, never dropped).
    Returns the manifest dict. Bijection violation = BUILD FAILURE before
    any file is written."""
    _check_date(date)
    ids = mint_market_ids(records)
    market_rows = [(mid, mt) for mt, mid in sorted(ids.items(),
                                                   key=lambda kv: kv[1])]
    _assert_bijection(market_rows, "write_day %s" % date)
    arr = records_to_array(records, ids)
    p = day_paths(root, date)
    os.makedirs(p["dir"], exist_ok=True)

    arr.tofile(p["bin"])
    missing_dim = 0
    with open(p["markets"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(MARKETS_COLS)
        for mid, mt in market_rows:
            d = dim.get(mt)
            if d is None:
                missing_dim += 1
                d = {}
            w.writerow([date, mid, mt] + [d.get(k, "") for k in DIM_FIELDS])
    n_trades = 0
    with open(p["trade_ids"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["stream_seq", "trade_id"])
        for r in records:
            if r.kind == _TRADE:
                w.writerow([r.stream_seq, r.event.payload.trade_id])
                n_trades += 1

    manifest = {
        "date": date,
        "builder_version": BUILDER_VERSION,
        "gold_layout_version": GOLD_LAYOUT_VERSION,
        "record_count": len(records),
        "trade_count": n_trades,
        "markets": len(ids),
        "markets_missing_dim": missing_dim,
        # md5 of the gold file and EVERY sidecar (§2.2 output spec):
        "files": {os.path.basename(p[k]): _md5(p[k])
                  for k in ("bin", "markets", "trade_ids")},
        "source_day": dict(source_ids or {}),
        # day-level safety verdicts: placeholders until W3.1 (V5) / W3.2 (V7)
        "safety_verdicts": {"V5": "pending", "V7": "pending",
                            "unsafe_for_microstructure": []},
    }
    with open(p["manifest"], "w") as f:                # manifest written LAST
        json.dump(manifest, f, indent=1, sort_keys=True)
    return manifest


# ---------------------------------------------------------------- the reader

class GoldDayReader(object):
    """One (root, date) partition, mmap-backed. Constructed PER DATE — §2.1:
    market_id is day-scoped, so the API carries the date on every access.
    On open: verifies the md5 of every manifest-listed file (gold + all
    sidecars), the record-count/file-size agreement, and the same-day 1:1
    market_id<->ticker bijection. Any mismatch => refuse, loud."""

    def __init__(self, root, date):
        _check_date(date)
        self.root, self.date = root, date
        p = self._paths = day_paths(root, date)
        if not os.path.isfile(p["manifest"]):
            raise GoldIOError("gold manifest missing: %s" % p["manifest"])
        with open(p["manifest"]) as f:
            self.manifest = json.load(f)
        expected = {os.path.basename(p[k]) for k in ("bin", "markets",
                                                     "trade_ids")}
        listed = set(self.manifest.get("files", {}))
        if expected - listed:
            raise GoldIOError("manifest %s does not list required files: %s"
                              % (p["manifest"], sorted(expected - listed)))
        for name, want in self.manifest["files"].items():
            path = os.path.join(p["dir"], name)
            if not os.path.isfile(path):
                raise GoldIOError("manifest-listed file missing: %s" % path)
            got = _md5(path)
            if got != want:
                raise GoldIOError(
                    "md5 mismatch for %s: manifest %s != file %s — refusing "
                    "to read a tampered/partial gold partition" %
                    (path, want, got))
        n = self.manifest["record_count"]
        size = os.path.getsize(p["bin"])
        if size != n * GOLD_DTYPE.itemsize:
            raise GoldIOError(
                "record count/size mismatch in %s: manifest says %d records "
                "(%d bytes), file is %d bytes" %
                (p["bin"], n, n * GOLD_DTYPE.itemsize, size))
        # zero-copy view (np.memmap cannot map a 0-byte file):
        self.records = (np.memmap(p["bin"], dtype=GOLD_DTYPE, mode="r")
                        if size else np.zeros(0, dtype=GOLD_DTYPE))
        self._by_ticker, self._by_id, pairs = {}, {}, []
        with open(p["markets"], newline="") as f:
            for row in csv.DictReader(f):
                mid = int(row["market_id"])
                pairs.append((mid, row["market_ticker"]))
                self._by_ticker[row["market_ticker"]] = mid
                self._by_id[mid] = row["market_ticker"]
        _assert_bijection(pairs, "read %s" % p["markets"])
        with open(p["trade_ids"], newline="") as f:
            self._trade_uuids = {int(r["stream_seq"]): r["trade_id"]
                                 for r in csv.DictReader(f)}

    @property
    def record_count(self):
        return len(self.records)

    def market_id(self, market_ticker):
        """Ticker -> this day's market_id (None if absent this day)."""
        return self._by_ticker.get(market_ticker)

    def ticker(self, market_id):
        """This day's market_id -> ticker (None if absent this day)."""
        return self._by_id.get(market_id)

    def trade_uuid(self, stream_seq):
        """stream_seq of a TRADE record -> full trade UUID from the sidecar."""
        return self._trade_uuids.get(stream_seq)


def cross_day(root, dates, *, market_ticker):
    """The ONLY cross-day helper — and it requires a ticker. §2.1: market_id
    is day-scoped; cross-day joins on market_id alone are FORBIDDEN, so no
    market_id form exists. Returns [(date, GoldDayReader, that day's
    market_id-or-None)] so callers join per (date, market_id)."""
    if not isinstance(market_ticker, str) or not market_ticker:
        raise GoldIOError(
            "cross-day access requires a market_ticker string; cross-day "
            "joins on market_id alone are FORBIDDEN (§2.1) — got %r"
            % (market_ticker,))
    out = []
    for d in dates:
        rd = GoldDayReader(root, d)
        out.append((d, rd, rd.market_id(market_ticker)))
    return out


def reconcile_trade_hashes(reader):
    """V8 shape: EVERY trade record's trade_id_hash in the .bin must equal
    fnv1a64(sidecar UUID at its stream_seq); every sidecar row must have its
    TRADE record. Returns violation strings; empty list = pass."""
    out, seen = [], set()
    recs = reader.records
    for i in np.nonzero(recs["event_type"] == _TRADE)[0]:
        ss = int(recs["stream_seq"][i])
        seen.add(ss)
        uuid = reader.trade_uuid(ss)
        if uuid is None:
            out.append("trade at stream_seq %d has no sidecar UUID" % ss)
            continue
        want, got = fnv1a64(uuid), int(recs["trade_id_hash"][i])
        if want != got:
            out.append("trade at stream_seq %d: bin hash %d != fnv1a64(%s)=%d"
                       % (ss, got, uuid, want))
    for ss in sorted(set(reader._trade_uuids) - seen):
        out.append("sidecar UUID at stream_seq %d has no TRADE record" % ss)
    return out
