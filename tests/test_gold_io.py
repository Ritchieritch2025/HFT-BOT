"""W2.4: gold writer/reader — merged records -> .bin + sidecars + manifest;
np.memmap reader; market_id stability (PLAN_GOLD_DATA_CONTRACT §2.1/§2.2
output spec). Contract encoded here, written BEFORE tools/gold_io.py exists
(TDD):

  - writer: W2.3 MergedRecord stream + per-day market dim mapping ->
    work/gold/date=<D>/gold_<D>.bin (512-B GOLD_DTYPE records) + sidecars
    markets_<D>.csv (WITH a date column + ticker/category/close_time +
    liquidity tier) and trade_ids_<D>.csv (stream_seq -> full UUID) +
    manifest_<D>.json (record count, gold md5, md5 of EVERY sidecar,
    builder version, source-day identifiers, V5/V7 safety-verdict
    placeholders);
  - market_id: dense per-day ints minted in first-appearance order;
    SAME-DAY 1:1 market_id<->ticker enforced — same ticker two ids OR same
    id two tickers = BUILD FAILURE, loud (never a warning);
  - reader: np.memmap zero-copy; verifies every manifest md5 on open
    (mismatch => refuse, loud); constructed per (root, date) so market_id
    access always carries a date; the cross-day helper REQUIRES a ticker —
    cross-day joins on market_id alone are FORBIDDEN (§2.1) and the API
    makes them impossible;
  - trade_id_hash: fnv1a64(full UUID) in the .bin reconciles against the
    trade_ids sidecar for EVERY trade (V8 shape); a tampered hash or UUID
    goes red even when the manifest md5s are made self-consistent.
"""
import csv
import hashlib
import json
import os
import random

import numpy as np
import pytest

from tools.gold_dtype import DEPTH, EVENT_TYPE, FLAGS, GOLD_DTYPE, fnv1a64
from tools.gold_load import BookMsg, Event, L1Top, Trade
from tools.gold_merge import merge
from tools import gold_io
from tools.gold_io import (
    BUILDER_VERSION, GoldDayReader, GoldIOError, cross_day,
    market_id_violations, mint_market_ids, reconcile_trade_hashes, write_day,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFECTS = os.path.join(ROOT, "tests", "fixtures", "gold_defects")
T0 = 1783305736000000
S = 1_000_000
DATE = "2026-07-06"

SNAP, DELTA = EVENT_TYPE["BOOK_SNAPSHOT"], EVENT_TYPE["BOOK_DELTA"]
TRADE, L1, HB = EVENT_TYPE["TRADE"], EVENT_TYPE["L1_TICKER"], EVENT_TYPE["HEARTBEAT"]

TAKER_CODE = {"yes": 1, "no": 2}


def _snap(yes, no, mt, ts):
    return Event(SNAP, ts, mt, BookMsg(None, None, None, tuple(yes), tuple(no)))


def _delta(side, px, d, mt, ts):
    return Event(DELTA, ts, mt, BookMsg(side, px, d, None, None))


def _trade(tid, mt, ts, yp=4500, ct=30000, side="yes"):
    return Event(TRADE, ts, mt, Trade(tid, yp, 10000 - yp, ct, side))


def _l1(bid, bq, ask, aq, mt, ts, snap=False):
    return Event(L1, ts, mt, L1Top(bid, bq, ask, aq, snap))


def _hb(mt, ts):
    return Event(HB, ts, mt, None)


def _dim(ticker, tier="high"):
    return {"category": "Sports", "close_time": "2026-07-06T23:00:00Z",
            "liquidity_tier": tier}


def _small_day():
    """Two covered markets, one L1-only, heartbeats, same-us race, trades."""
    full_a = [_snap([(4500, 100000), (4400, 50000)], [(5300, 70000)], "KXIO-A", T0),
              _delta("yes", 4500, -30000, "KXIO-A", T0 + S),
              _delta("no", 5300, -20000, "KXIO-A", T0 + 2 * S)]
    full_b = [_snap([(3000, 20000)], [(6500, 10000)], "KXIO-B", T0 + S // 2),
              _delta("yes", 3000, 5000, "KXIO-B", T0 + 3 * S)]
    trades = [_trade("a0be3c5a-8563-725e-5d8e-26026dda3934", "KXIO-A", T0 + S),
              _trade("b1cf4d6b-9674-836f-6e9f-37137eeb4a45", "KXIO-B", T0 + 3 * S, yp=3000, side="no"),
              _trade("c2d05e7c-a785-947a-7fa0-48248ffc5b56", "KXIO-D", T0 + 4 * S)]
    l1_c = [_l1(4500, 100000, 4700, 50000, "KXIO-C", T0 + S, snap=True)]
    hbs = [_hb("KXIO-A", T0 + 2 * S)]
    records, _ = merge([full_a, full_b, trades, l1_c, hbs])
    dim = {mt: _dim(mt) for mt in ("KXIO-A", "KXIO-B", "KXIO-C", "KXIO-D")}
    return records, dim


def _synthetic_10k(n_markets=20, n_deltas=400, n_trades=100):
    """>= 10k merged records across n_markets covered markets + trades."""
    full, trades = [], []
    for m in range(n_markets):
        mt = "KXIO-%02d" % m
        src = [_snap([(4500, 100000), (4400, 50000)], [(5300, 70000)], mt, T0 + m)]
        for k in range(n_deltas):
            src.append(_delta("yes" if k % 2 else "no",
                              4400 + (k % 50) if k % 2 else 5300 + (k % 40),
                              1000 + k, mt, T0 + m + (k + 1) * 1000))
        full.append(src)
        trades.append([_trade("%08d-aaaa-bbbb-cccc-%012d" % (m, j), mt,
                              T0 + m + j * 3777 + 501,
                              yp=1 + (j * 97) % 9999, ct=10000 + j,
                              side="yes" if j % 2 else "no")
                       for j in range(n_trades)])
    records, _ = merge(full + trades)
    dim = {"KXIO-%02d" % m: _dim("KXIO-%02d" % m, tier=("high", "mid", "low")[m % 3])
           for m in range(n_markets)}
    return records, dim


def _paths(root, date):
    d = os.path.join(root, "date=%s" % date)
    return {"dir": d,
            "bin": os.path.join(d, "gold_%s.bin" % date),
            "markets": os.path.join(d, "markets_%s.csv" % date),
            "trade_ids": os.path.join(d, "trade_ids_%s.csv" % date),
            "manifest": os.path.join(d, "manifest_%s.json" % date)}


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _tamper(path, offset=100, delta=1):
    with open(path, "r+b") as f:
        f.seek(offset)
        b = f.read(1)
        f.seek(offset)
        f.write(bytes([(b[0] + delta) % 256]))


def _rewrite_manifest_md5s(paths):
    """Simulate a consistent-but-wrong build: recompute every manifest md5
    so the reader's integrity gate passes and deeper checks must catch it."""
    with open(paths["manifest"]) as f:
        man = json.load(f)
    for name in man["files"]:
        man["files"][name] = _md5(os.path.join(paths["dir"], name))
    with open(paths["manifest"], "w") as f:
        json.dump(man, f)


def _fixture_pairs(name):
    with open(os.path.join(DEFECTS, "%s.csv" % name), newline="") as f:
        return [(int(r["market_id"]), r["market_ticker"])
                for r in csv.DictReader(f)]


# --------------------------------------------------------------- writer basics

def test_write_day_produces_bin_sidecars_manifest(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    man = write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    for key in ("bin", "markets", "trade_ids", "manifest"):
        assert os.path.isfile(p[key]), key
    assert os.path.getsize(p["bin"]) == len(records) * 512
    assert man["record_count"] == len(records)


def test_market_id_dense_first_appearance():
    records, _ = _small_day()
    ids = mint_market_ids(records)
    first_seen = []
    for r in records:
        if r.market_ticker not in first_seen:
            first_seen.append(r.market_ticker)
    assert list(ids) == first_seen                     # first-appearance order
    assert sorted(ids.values()) == list(range(len(ids)))  # dense per-day ints


def test_records_render_field_exact(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    arr = np.memmap(_paths(root, DATE)["bin"], dtype=GOLD_DTYPE, mode="r")
    ids = mint_market_ids(records)
    for i, r in enumerate(records):
        row = arr[i]
        assert int(row["ts_us"]) == r.ts_us
        assert int(row["stream_seq"]) == r.stream_seq
        assert int(row["market_id"]) == ids[r.market_ticker]
        assert int(row["event_type"]) == r.kind
        assert int(row["book_seq"]) == r.book_seq
        assert int(row["flags"]) == r.state.flags
        assert tuple(int(x) for x in row["bid_px_e4"]) == r.state.bid_px_e4
        assert tuple(int(x) for x in row["ask_px_e4"]) == r.state.ask_px_e4
        assert tuple(int(x) for x in row["bid_qty_e4"]) == r.state.bid_qty_e4
        assert tuple(int(x) for x in row["ask_qty_e4"]) == r.state.ask_qty_e4
        assert int(row["bid_rest_qty_e4"]) == r.state.bid_rest_qty_e4
        assert int(row["ask_rest_qty_e4"]) == r.state.ask_rest_qty_e4
        assert int(row["bid_nlevels"]) == r.state.bid_nlevels
        assert int(row["ask_nlevels"]) == r.state.ask_nlevels
        if r.kind == TRADE:
            t = r.event.payload
            assert int(row["trade_yes_price_e4"]) == t.yes_price_e4
            assert int(row["taker_side"]) == TAKER_CODE[t.taker_side]
            assert int(row["trade_qty_e4"]) == t.count_e4
            assert int(row["trade_id_hash"]) == fnv1a64(t.trade_id)
        else:                                          # zero unless TRADE (§2.1)
            assert int(row["trade_yes_price_e4"]) == 0
            assert int(row["taker_side"]) == 0
            assert int(row["trade_qty_e4"]) == 0
            assert int(row["trade_id_hash"]) == 0


# ------------------------------------- V11 runtime half: 10k mmap == streamed

def test_10k_write_mmap_random_access_equals_streamed_parse(tmp_path):
    records, dim = _synthetic_10k()
    assert len(records) >= 10000                       # the contract's floor
    root = str(tmp_path)
    man = write_day(records, dim, DATE, root)
    assert man["record_count"] == len(records)
    p = _paths(root, DATE)
    mm = np.memmap(p["bin"], dtype=GOLD_DTYPE, mode="r")
    with open(p["bin"], "rb") as f:
        streamed = np.frombuffer(f.read(), dtype=GOLD_DTYPE)
    assert len(mm) == len(streamed) == len(records)
    rng = random.Random(11)
    sample = rng.sample(range(len(records)), 1000)     # >= 1000 records (V11)
    ids = mint_market_ids(records)
    for i in sample:
        assert mm[i] == streamed[i]                    # random access == stream
        r = records[i]
        assert int(mm[i]["ts_us"]) == r.ts_us
        assert int(mm[i]["stream_seq"]) == r.stream_seq
        assert int(mm[i]["market_id"]) == ids[r.market_ticker]
        assert int(mm[i]["event_type"]) == r.kind
        if r.kind == TRADE:
            assert int(mm[i]["trade_id_hash"]) == fnv1a64(r.event.payload.trade_id)
    assert bool((mm[:] == streamed).all())             # and the whole file
    print("\nV11 runtime half: %d records written; mmap random access == "
          "streamed parse on %d sampled + all-rows equality" %
          (len(records), len(sample)))


# ------------------------------------ same-day 1:1 market_id <-> ticker (loud)

def test_bijection_checker_flags_both_defect_shapes():
    good = [(0, "KXIO-A"), (1, "KXIO-B"), (2, "KXIO-C")]
    assert market_id_violations(good) == []
    two_ids = good + [(3, "KXIO-A")]                   # same ticker, two ids
    v = market_id_violations(two_ids)
    assert v and any("KXIO-A" in s for s in v)
    two_tickers = good + [(2, "KXIO-D")]               # same id, two tickers
    v = market_id_violations(two_tickers)
    assert v and any("KXIO-D" in s or "KXIO-C" in s for s in v)


def test_fixture_duplicate_ticker_two_ids_detected():
    """RED fixture (1): one ticker minted under two market_ids — must be
    detected; a mutant dropping the ticker->ids check goes green here."""
    v = market_id_violations(_fixture_pairs("io_duplicate_ticker_two_ids"))
    assert v, "duplicate-ticker-two-ids fixture NOT detected"
    print("\nRED fixture io_duplicate_ticker_two_ids detected: %s" % v)


def test_fixture_one_id_two_tickers_detected():
    """RED fixture (2): one market_id claimed by two tickers — must be
    detected; a mutant dropping the id->tickers check goes green here."""
    v = market_id_violations(_fixture_pairs("io_one_id_two_tickers"))
    assert v, "one-id-two-tickers fixture NOT detected"
    print("\nRED fixture io_one_id_two_tickers detected: %s" % v)


def test_write_day_bijection_violation_is_loud_build_failure(tmp_path, monkeypatch):
    """The write path ENFORCES the bijection: a minting mutant producing a
    duplicate id (or duplicate ticker) must abort the build loudly."""
    records, dim = _small_day()

    def bad_mint(recs):
        ids = mint_market_ids(recs)
        first = next(iter(ids))
        ids[first] = 1                                 # collide with id 1
        return ids

    monkeypatch.setattr(gold_io, "mint_market_ids", bad_mint)
    with pytest.raises(GoldIOError, match="BUILD FAILURE"):
        write_day(records, dim, DATE, str(tmp_path))
    assert not os.path.isfile(_paths(str(tmp_path), DATE)["manifest"])


def test_reader_refuses_sidecar_bijection_violation(tmp_path):
    """Defense in depth: a doctored markets sidecar (md5s made consistent)
    still cannot be read — the reader re-checks the bijection on open."""
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    with open(p["markets"], newline="") as f:
        rows = list(csv.reader(f))
    rows.append([DATE, rows[-1][1], "KXIO-EVIL", "Sports", "", "high"])
    with open(p["markets"], "w", newline="") as f:
        csv.writer(f).writerows(rows)
    _rewrite_manifest_md5s(p)
    with pytest.raises(GoldIOError, match="BUILD FAILURE|1:1"):
        GoldDayReader(root, DATE)


# ------------------------------------------------------------------- sidecars

def test_markets_sidecar_has_date_column_and_dim_fields(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    with open(_paths(root, DATE)["markets"], newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows, "markets sidecar is empty"
    for col in ("date", "market_id", "market_ticker", "category",
                "close_time", "liquidity_tier"):
        assert col in rows[0], "markets sidecar missing column %r" % col
    assert all(r["date"] == DATE for r in rows)        # date column, every row
    assert [int(r["market_id"]) for r in rows] == list(range(len(rows)))
    a = next(r for r in rows if r["market_ticker"] == "KXIO-A")
    assert a["category"] == "Sports" and a["liquidity_tier"] == "high"
    assert a["close_time"] == "2026-07-06T23:00:00Z"


def test_trade_ids_sidecar_maps_stream_seq_to_full_uuid(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    with open(_paths(root, DATE)["trade_ids"], newline="") as f:
        rows = list(csv.DictReader(f))
    trades = [r for r in records if r.kind == TRADE]
    assert len(rows) == len(trades)
    by_seq = {int(r["stream_seq"]): r["trade_id"] for r in rows}
    for t in trades:
        assert by_seq[t.stream_seq] == t.event.payload.trade_id  # full UUID


def test_manifest_contents(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    src = {"warehouse_date": DATE, "staging": "warehouse/staging.duckdb"}
    man = write_day(records, dim, DATE, root, source_ids=src)
    p = _paths(root, DATE)
    with open(p["manifest"]) as f:
        on_disk = json.load(f)
    assert on_disk == man
    assert man["date"] == DATE
    assert man["builder_version"] == BUILDER_VERSION
    assert man["record_count"] == len(records)
    assert man["source_day"] == src
    files = man["files"]
    for key in ("bin", "markets", "trade_ids"):        # gold + EVERY sidecar
        name = os.path.basename(p[key])
        assert files[name] == _md5(p[key]), name
    for v in ("V5", "V7"):                             # day-level verdicts
        assert man["safety_verdicts"][v] == "pending"  # placeholders until W3.x
    assert man["safety_verdicts"]["unsafe_for_microstructure"] == []


# ------------------------------------------------------------------ the reader

def test_reader_roundtrip_zero_copy(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    rd = GoldDayReader(root, DATE)
    assert rd.date == DATE
    assert rd.record_count == len(records)
    assert isinstance(rd.records, np.memmap)           # zero-copy mmap
    assert rd.records.dtype == GOLD_DTYPE
    ids = mint_market_ids(records)
    for mt, mid in ids.items():
        assert rd.market_id(mt) == mid
        assert rd.ticker(mid) == mt
    assert rd.market_id("KXIO-NOPE") is None
    tr = next(r for r in records if r.kind == TRADE)
    assert rd.trade_uuid(tr.stream_seq) == tr.event.payload.trade_id
    assert reconcile_trade_hashes(rd) == []


def test_reader_verifies_md5_refuses_tampered_sidecar(tmp_path):
    """RED fixture (3): tamper ONE byte of a sidecar after the manifest is
    written — the reader must refuse, loudly, on open."""
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    _tamper(p["markets"], offset=40)
    with pytest.raises(GoldIOError, match="md5"):
        GoldDayReader(root, DATE)
    print("\nRED tamper (3): sidecar byte flipped after manifest write -> "
          "reader refused on open")


def test_reader_refuses_tampered_gold_bin_and_trade_ids(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    _tamper(p["bin"], offset=17)
    with pytest.raises(GoldIOError, match="md5"):
        GoldDayReader(root, DATE)
    # restore via rewrite, then tamper the other sidecar
    write_day(records, dim, DATE, root)
    _tamper(p["trade_ids"], offset=25)
    with pytest.raises(GoldIOError, match="md5"):
        GoldDayReader(root, DATE)


def test_reader_refuses_missing_manifest_or_sidecar(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    os.rename(p["trade_ids"], p["trade_ids"] + ".gone")
    with pytest.raises(GoldIOError, match="missing"):
        GoldDayReader(root, DATE)
    os.rename(p["trade_ids"] + ".gone", p["trade_ids"])
    os.remove(p["manifest"])
    with pytest.raises(GoldIOError, match="manifest"):
        GoldDayReader(root, DATE)


def test_reader_refuses_record_count_mismatch(tmp_path):
    """Truncation with a doctored manifest md5 must still be refused: the
    record count is cross-checked against the actual file size (D2)."""
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    with open(p["bin"], "r+b") as f:
        f.truncate((len(records) - 1) * 512)
    _rewrite_manifest_md5s(p)
    with pytest.raises(GoldIOError, match="record count|size"):
        GoldDayReader(root, DATE)


# --------------------------- market_id is DAY-SCOPED; cross-day needs a ticker

def test_reader_requires_a_date():
    with pytest.raises(TypeError):
        GoldDayReader("work/gold")                     # no date: impossible
    with pytest.raises(GoldIOError, match="date"):
        GoldDayReader("work/gold", "")
    with pytest.raises(GoldIOError, match="date"):
        GoldDayReader("work/gold", "not-a-date")


def test_cross_day_requires_ticker_market_id_forbidden(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    with pytest.raises(TypeError):                     # no such parameter
        cross_day(root, [DATE], market_id=0)
    with pytest.raises(TypeError):                     # ticker is keyword-only
        cross_day(root, [DATE], "KXIO-A")
    with pytest.raises(GoldIOError, match="FORBIDDEN"):
        cross_day(root, [DATE], market_ticker=0)       # an id sneaking through
    with pytest.raises(GoldIOError, match="FORBIDDEN"):
        cross_day(root, [DATE], market_ticker="")


def test_cross_day_by_ticker_and_ids_differ_across_days(tmp_path):
    """The reason for the rule, demonstrated: the same ticker mints DIFFERENT
    market_ids on different days (first-appearance order differs), so a
    market_id-only cross-day join would silently splice different markets."""
    root = str(tmp_path)
    d1, d2 = "2026-07-06", "2026-07-07"
    day1, _ = merge([[_snap([(4500, 1)], [(5300, 1)], "KXIO-A", T0),
                      _snap([(3000, 1)], [(6500, 1)], "KXIO-B", T0 + S)]])
    day2, _ = merge([[_snap([(3000, 1)], [(6500, 1)], "KXIO-B", T0),
                      _snap([(4500, 1)], [(5300, 1)], "KXIO-A", T0 + S)]])
    dim = {"KXIO-A": _dim("KXIO-A"), "KXIO-B": _dim("KXIO-B")}
    write_day(day1, dim, d1, root)
    write_day(day2, dim, d2, root)
    out = cross_day(root, [d1, d2], market_ticker="KXIO-A")
    assert [(d, mid) for d, _, mid in out] == [(d1, 0), (d2, 1)]
    assert GoldDayReader(root, d1).market_id("KXIO-A") == 0
    assert GoldDayReader(root, d2).market_id("KXIO-A") == 1


# --------------------------------- V8 shape: hash <-> sidecar reconciliation

def test_reconcile_trade_hashes_green_on_clean_day(tmp_path):
    records, dim = _synthetic_10k(n_markets=4, n_deltas=50, n_trades=40)
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    rd = GoldDayReader(root, DATE)
    assert reconcile_trade_hashes(rd) == []            # every trade reconciles
    n_trades = int((rd.records["event_type"] == TRADE).sum())
    assert n_trades == 160                             # and it checked them all


def test_reconcile_detects_tampered_sidecar_uuid(tmp_path):
    """Tampered-hash RED: swap one sidecar UUID and make the manifest md5s
    self-consistent — the md5 gate passes, reconciliation must go red."""
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    with open(p["trade_ids"], newline="") as f:
        rows = list(csv.reader(f))
    rows[1][1] = "ffffffff-ffff-ffff-ffff-ffffffffffff"   # not the hashed UUID
    with open(p["trade_ids"], "w", newline="") as f:
        csv.writer(f).writerows(rows)
    _rewrite_manifest_md5s(p)
    rd = GoldDayReader(root, DATE)                     # md5s pass by design
    v = reconcile_trade_hashes(rd)
    assert v and any("fnv1a64" in s or "hash" in s for s in v)
    print("\nRED tampered-hash: sidecar UUID swapped (manifest md5s made "
          "consistent) -> reconciliation red: %s" % v)


def test_reconcile_detects_tampered_bin_hash(tmp_path):
    """Same shape from the other side: flip the trade_id_hash IN the .bin
    (manifest md5s made consistent) — reconciliation must go red."""
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    arr = np.fromfile(p["bin"], dtype=GOLD_DTYPE)
    i = int(np.nonzero(arr["event_type"] == TRADE)[0][0])
    arr["trade_id_hash"][i] ^= 0xDEAD
    arr.tofile(p["bin"])
    _rewrite_manifest_md5s(p)
    rd = GoldDayReader(root, DATE)
    assert reconcile_trade_hashes(rd)


def test_reconcile_detects_missing_and_orphan_sidecar_rows(tmp_path):
    records, dim = _small_day()
    root = str(tmp_path)
    write_day(records, dim, DATE, root)
    p = _paths(root, DATE)
    with open(p["trade_ids"], newline="") as f:
        rows = list(csv.reader(f))
    dropped = rows.pop(1)                              # a trade loses its UUID
    rows.append(["999999", dropped[1]])                # and an orphan appears
    with open(p["trade_ids"], "w", newline="") as f:
        csv.writer(f).writerows(rows)
    _rewrite_manifest_md5s(p)
    v = reconcile_trade_hashes(GoldDayReader(root, DATE))
    assert any("no sidecar" in s for s in v)
    assert any("no TRADE" in s for s in v)


# ----------------------------------------------------------------- guard rails

def test_write_day_rejects_bad_date_and_empty_records(tmp_path):
    records, dim = _small_day()
    with pytest.raises(GoldIOError, match="date"):
        write_day(records, dim, "20260706", str(tmp_path))
    man = write_day([], {}, DATE, str(tmp_path))       # empty day still valid
    assert man["record_count"] == 0
    rd = GoldDayReader(str(tmp_path), DATE)
    assert rd.record_count == 0 and reconcile_trade_hashes(rd) == []


def test_nlevels_overflow_is_loud_build_failure(tmp_path):
    """BACKLOG note owned by W2.4: bid_nlevels/ask_nlevels are uint16 in the
    frozen layout; a side with >65535 levels must ABORT the build loudly —
    silent truncation (or a bare numpy OverflowError) is forbidden (D2)."""
    records, dim = _small_day()
    doctored = list(records)
    i = next(k for k, r in enumerate(doctored) if r.state.bid_nlevels)
    doctored[i] = doctored[i]._replace(
        state=doctored[i].state._replace(bid_nlevels=70000))
    with pytest.raises(GoldIOError, match="nlevels"):
        write_day(doctored, dim, DATE, str(tmp_path))
    doctored[i] = doctored[i]._replace(
        state=doctored[i].state._replace(bid_nlevels=1, ask_nlevels=1 << 16))
    with pytest.raises(GoldIOError, match="nlevels"):
        write_day(doctored, dim, DATE, str(tmp_path))


def test_markets_missing_dim_are_kept_and_counted(tmp_path):
    """Coverage honesty (V10 spirit): a market absent from the dim mapping is
    NEVER dropped from the sidecar — fields empty, count surfaced."""
    records, _ = _small_day()
    root = str(tmp_path)
    man = write_day(records, {"KXIO-A": _dim("KXIO-A")}, DATE, root)
    with open(_paths(root, DATE)["markets"], newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4                              # all four markets present
    b = next(r for r in rows if r["market_ticker"] == "KXIO-B")
    assert b["category"] == "" and b["liquidity_tier"] == ""
    assert man["markets_missing_dim"] == 3
