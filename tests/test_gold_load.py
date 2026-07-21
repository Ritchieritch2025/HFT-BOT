"""W2.1: typed loaders — l1/full/trades warehouse rows -> typed events
(PLAN_GOLD_DATA_CONTRACT §2.2 dynamic validation gates, V1, V17 subset).

Contract encoded here, written BEFORE tools/gold_load.py exists (TDD):
  - V1 byte-exact E4 round-trip on real golden rows (string -> int ->
    re-rendered string identical), integer digit accumulation only;
  - grep gate: no float parse anywhere in tools/gold_load.py code tokens;
  - every seeded-defect fixture in tests/fixtures/gold_defects/ IS detected
    (must-fail assertions via pytest.raises on assert_clean) while valid rows
    in the same file keep flowing;
  - no silent repair: quarantined rows preserved verbatim, non-monotonic
    timestamps reported but never reordered/"fixed";
  - trades stable-sorted (ts_us, trade_id) + deduped on trade_id (V8-style);
  - loader report + quarantine + malformed_record_sample outputs exist and
    use the operator vocabulary verbatim.
"""
import io
import json
import os
import tokenize

import pytest

from tools.gold_load import (
    Event, GoldLoadError, LoaderReport, assert_clean, iter_ndjson,
    load_full, load_l1, load_trades, parse_e4, render_e4, write_outputs,
)
from tools.gold_dtype import EVENT_TYPE

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "tests", "fixtures", "gold_golden_rows")
DEFECTS = os.path.join(ROOT, "tests", "fixtures", "gold_defects")
NOW_US = 1783382400000000  # 2026-07-07 00:00 UTC — freeze "now" for the ts gate


def _golden(name):
    with open(os.path.join(GOLDEN, "%s.json" % name)) as f:
        return json.load(f)["rows"]


def _defect(name):
    rows = []
    with open(os.path.join(DEFECTS, "%s.ndjson" % name)) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))  # plain loads: JSON floats stay float
    return rows


# ---------------------------------------------------------------- V1 round-trip

def _frac_digits(s):
    return len(s.partition(".")[2])


def test_v1_trades_byte_exact_roundtrip():
    for r in _golden("trades"):
        for key in ("yes_price_e4", "no_price_e4", "count_e4"):
            s, want = r["input"][key], r["expect_e4"][key]
            v = parse_e4(s)
            assert v == want, "%s: %r -> %d != warehouse %d" % (key, s, v, want)
            assert render_e4(v, _frac_digits(s)) == s  # byte-exact re-render


def test_v1_l1_byte_exact_roundtrip():
    for r in _golden("l1"):
        for key in ("yes_bid_e4", "yes_bid_qty_e4", "yes_ask_e4", "yes_ask_qty_e4"):
            s, want = r["input"][key], r["expect_e4"][key]
            v = parse_e4(s)
            assert v == want
            assert render_e4(v, _frac_digits(s)) == s


def test_parse_e4_integer_accumulation():
    assert parse_e4("0.0090") == 90            # sub-penny (D5)
    assert parse_e4("5119.00") == 51190000     # fractional qty
    assert parse_e4("1.0000") == 10000
    assert parse_e4("0.0000") == 0
    assert parse_e4("-1.25") == -12500
    assert parse_e4("42") == 420000
    assert parse_e4("0.00900000") == 90        # excess zeros are lossless
    assert parse_e4(4500) == 4500              # already-E4 int passes through


@pytest.mark.parametrize("bad", ["", ".", "1e3", "0x10", "1.2.3", "1,5",
                                 " 1.0", "1.0 ", "--1", "1.", ".5",
                                 "0.00901", "٥.٠"])
def test_parse_e4_rejects_malformed(bad):
    with pytest.raises(GoldLoadError):
        parse_e4(bad)


def test_parse_e4_rejects_float_value():
    with pytest.raises(GoldLoadError):
        parse_e4(0.45)  # float input is a gate violation, never coerced


def test_render_e4_refuses_lossy_render():
    assert render_e4(90, 4) == "0.0090"
    assert render_e4(51190000, 2) == "5119.00"
    assert render_e4(-12500, 2) == "-1.25"
    assert render_e4(420000, 0) == "42"
    with pytest.raises(GoldLoadError):
        render_e4(90, 2)  # 0.0090 has no exact 2-digit form


# ------------------------------------------------------------ float grep gate

def test_no_float_parse_in_loader_source():
    """V1 grep gate: `float` never appears as a code token (calls, casts,
    annotations) in tools/gold_load.py — comments/strings excluded by
    tokenizing rather than grepping raw text."""
    path = os.path.join(ROOT, "tools", "gold_load.py")
    with open(path, "rb") as f:
        toks = list(tokenize.tokenize(f.readline))
    hits = [t for t in toks
            if t.type == tokenize.NAME and t.string == "float"]
    assert not hits, "float used in code at lines %s" % [t.start[0] for t in hits]


# ---------------------------------------------------------- golden clean loads

def test_golden_trades_load_clean_and_typed():
    rows = [dict(r["input"]) for r in _golden("trades")]
    events, rep = load_trades(rows, now_us=NOW_US)
    assert_clean(rep)  # zero rejects on known-good real rows
    assert rep.rows_in == len(rows) and rep.events_out == len(rows)
    by_id = {e.payload.trade_id: e for e in events}
    for r in _golden("trades"):
        e = by_id[r["input"]["trade_id"]]
        assert isinstance(e, Event) and e.kind == EVENT_TYPE["TRADE"]
        assert e.ts_us == r["input"]["ts_utc"]
        assert e.market_ticker == r["input"]["market_ticker"]
        assert e.payload.yes_price_e4 == r["expect_e4"]["yes_price_e4"]
        assert e.payload.no_price_e4 == r["expect_e4"]["no_price_e4"]
        assert e.payload.count_e4 == r["expect_e4"]["count_e4"]
        assert e.payload.taker_side == r["input"]["taker_side"]
    # sorted by (ts_us, trade_id)
    keys = [(e.ts_us, e.payload.trade_id) for e in events]
    assert keys == sorted(keys)


def test_golden_l1_load_clean_and_typed():
    rows = [dict(r["input"]) for r in _golden("l1")]
    events, rep = load_l1(rows, now_us=NOW_US)
    assert_clean(rep)
    assert len(events) == len(rows)
    for e, r in zip(events, _golden("l1")):  # input order preserved
        assert e.kind == EVENT_TYPE["L1_TICKER"]
        assert e.ts_us == r["input"]["ts_utc"]
        assert e.payload.yes_bid_e4 == r["expect_e4"]["yes_bid_e4"]
        assert e.payload.yes_bid_qty_e4 == r["expect_e4"]["yes_bid_qty_e4"]
        assert e.payload.yes_ask_e4 == r["expect_e4"]["yes_ask_e4"]
        assert e.payload.yes_ask_qty_e4 == r["expect_e4"]["yes_ask_qty_e4"]
        assert e.payload.is_snapshot == r["input"]["is_snapshot"]


def test_golden_full_load_clean_and_typed():
    rows = [dict(r["input"]) for r in _golden("full")]
    events, rep = load_full(rows, now_us=NOW_US)
    assert_clean(rep)
    assert len(events) == len(rows)
    kinds = {e.kind for e in events}
    assert kinds == {EVENT_TYPE["BOOK_SNAPSHOT"], EVENT_TYPE["BOOK_DELTA"]}
    for e, r in zip(events, rows):
        if e.kind == EVENT_TYPE["BOOK_SNAPSHOT"]:
            want = json.loads(r["yes_levels"])
            assert e.payload.yes_levels == tuple((p, q) for p, q in want)
            assert all(isinstance(p, int) and isinstance(q, int)
                       for p, q in e.payload.yes_levels)
            assert e.payload.no_levels  # parsed too
        else:
            assert e.payload.side in ("yes", "no")
            assert e.payload.price_e4 == r["price_e4"]
            assert e.payload.delta_e4 == r["delta_e4"]
            assert isinstance(e.payload.delta_e4, int)


# -------------------------------------------- seeded defects MUST be detected

TRADE_DEFECTS = [
    ("trades_duplicate_trade_id", "duplicate_trade_id", 1),
    ("trades_float_price", "float_dtype", 1),
    ("trades_price_out_of_range", "price_out_of_range", 2),
    ("trades_inconsistent_price_pair", "inconsistent_price_pair", 1),
    ("trades_negative_qty", "bad_quantity", 2),
    ("trades_bad_timestamp", "bad_timestamp", 2),
    ("trades_bad_taker_side", "invalid_taker_side", 1),
    ("trades_missing_field", "missing_field", 2),
    ("trades_bad_ticker", "invalid_ticker", 2),
]


@pytest.mark.parametrize("fixture,reason,nbad", TRADE_DEFECTS,
                         ids=[d[0] for d in TRADE_DEFECTS])
def test_trade_defect_detected(fixture, reason, nbad):
    rows = _defect(fixture)
    events, rep = load_trades(rows, now_us=NOW_US)
    got = sum(n for r, n in rep.reasons.items() if r.startswith(reason))
    assert got == nbad, "expected %d x %s, got %s" % (nbad, reason, dict(rep.reasons))
    assert rep.malformed_record_count == nbad
    assert len(events) == len(rows) - nbad  # good rows keep flowing
    assert len(events) >= 1
    with pytest.raises(GoldLoadError):
        assert_clean(rep)  # a defective batch must never pass as clean


FULL_DEFECTS = [
    ("full_malformed_json", "malformed_json", 1),
    ("full_malformed_level_array", "malformed_level_array", 2),
    ("full_float_in_levels", "float_dtype", 1),
    ("full_bad_delta", "", 3),  # bad_delta + invalid_side + bad_msg_type
]


@pytest.mark.parametrize("fixture,reason,nbad", FULL_DEFECTS,
                         ids=[d[0] for d in FULL_DEFECTS])
def test_full_defect_detected(fixture, reason, nbad):
    rows = _defect(fixture)
    events, rep = load_full(rows, now_us=NOW_US)
    assert rep.malformed_record_count == nbad, dict(rep.reasons)
    if reason:
        assert any(r.startswith(reason) for r in rep.reasons), dict(rep.reasons)
    assert len(events) == len(rows) - nbad and len(events) >= 1
    with pytest.raises(GoldLoadError):
        assert_clean(rep)


L1_DEFECTS = [
    ("l1_float_dtype", "float_dtype", 1),
    ("l1_empty_side_mismatch", "empty_side_qty_mismatch", 2),
]


@pytest.mark.parametrize("fixture,reason,nbad", L1_DEFECTS,
                         ids=[d[0] for d in L1_DEFECTS])
def test_l1_defect_detected(fixture, reason, nbad):
    rows = _defect(fixture)
    events, rep = load_l1(rows, now_us=NOW_US)
    assert any(r.startswith(reason) for r in rep.reasons), dict(rep.reasons)
    assert rep.malformed_record_count == nbad
    assert len(events) == len(rows) - nbad and len(events) >= 1
    with pytest.raises(GoldLoadError):
        assert_clean(rep)


def test_duplicate_trade_dedupe_keeps_first_by_sort_order():
    """V8-style: stable sort (ts_us, trade_id) then dedupe keeps the EARLIEST
    print; the later duplicate is quarantined, never silently dropped."""
    rows = _defect("trades_duplicate_trade_id")
    events, rep = load_trades(rows, now_us=NOW_US)
    dups = [e for e in events
            if e.payload.trade_id == "bbbb2222-e117-5290-0b2d-40f8241da6ad"]
    assert len(dups) == 1 and dups[0].ts_us == 1783305736000000
    q = [q for q in rep.quarantined if q["reason"] == "duplicate_trade_id"]
    assert len(q) == 1 and q[0]["row"]["ts_utc"] == 1783305737000000


# --------------------------------------------------- report-only, no repairs

def test_nonmonotonic_ts_reported_never_fixed():
    rows = _defect("l1_nonmonotonic_ts")
    events, rep = load_l1(rows, now_us=NOW_US)
    assert rep.ts_regressions >= 1          # detected AND reported
    assert rep.malformed_record_count == 0  # ...but never rejected
    assert_clean(rep)                       # report-only gate stays clean
    assert [e.ts_us for e in events] == [r["ts_utc"] for r in rows]  # no reorder


def test_quarantined_rows_preserved_verbatim():
    rows = _defect("trades_price_out_of_range")
    originals = [dict(r) for r in rows]
    _, rep = load_trades(rows, now_us=NOW_US)
    assert rows == originals  # inputs never mutated
    for q in rep.quarantined:
        assert q["row"] in originals  # original row kept byte-for-byte
        assert q["reason"]


def test_corrupt_ndjson_line_quarantined():
    path = os.path.join(DEFECTS, "corrupt_ndjson_line.ndjson")
    rep = LoaderReport("corrupt_ndjson_line")
    with open(path) as f:
        rows = list(iter_ndjson(f, rep))
    assert len(rows) == 1  # the good line survives
    assert rep.reasons.get("malformed_json_line") == 1
    assert rep.quarantined[0]["row"].startswith('{"ts_utc": 178330573')
    with pytest.raises(GoldLoadError):
        assert_clean(rep)


def test_float_via_ndjson_guard_never_constructs_float():
    """iter_ndjson parses NDJSON with a float guard: a float token becomes a
    reject at the gate, not a Python float silently coerced downstream."""
    rep = LoaderReport("g")
    line = ('{"ts_utc": 1783305736000000, "market_ticker": "KXT-1", '
            '"trade_id": "x-1", "yes_price_e4": 3880.0, "no_price_e4": 6120, '
            '"count_e4": 100000, "taker_side": "no"}')
    rows = list(iter_ndjson([line], rep))
    assert len(rows) == 1
    events, rep2 = load_trades(rows, now_us=NOW_US)
    assert not events and rep2.reasons and \
        any(r.startswith("float_dtype") for r in rep2.reasons)


# ----------------------------------------------------------- operator outputs

def test_write_outputs_report_quarantine_sample(tmp_path):
    _, rep_bad = load_trades(_defect("trades_negative_qty"), now_us=NOW_US)
    _, rep_ok = load_l1([dict(r["input"]) for r in _golden("l1")], now_us=NOW_US)
    out = write_outputs([rep_bad, rep_ok], str(tmp_path), tag="testtag")
    report = json.load(open(out["report"]))
    src = {s["source"]: s for s in report["sources"]}
    bad = src[rep_bad.source]
    # operator vocabulary, verbatim (PLAN §3)
    assert bad["Malformed Records"] == 2
    assert bad["Rejected Rows"] == {"bad_quantity:count_e4": 2}
    assert bad["Quarantined Input Rows"] == 2
    assert src[rep_ok.source]["Malformed Records"] == 0
    qfile = os.path.join(str(tmp_path), "quarantine",
                         "%s_testtag.ndjson" % rep_bad.source)
    with open(qfile) as f:
        qrows = [json.loads(l) for l in f if l.strip()]
    assert len(qrows) == 2 and all(q["reason"].startswith("bad_quantity")
                                   for q in qrows)
    assert qrows[0]["row"]["count_e4"] == -156300  # original bytes preserved
    assert os.path.exists(out["sample_csv"])  # malformed_record_sample CSV
    with open(out["sample_csv"]) as f:
        assert "bad_quantity" in f.read()


def test_trades_stable_sort_on_shuffled_input():
    rows = [dict(r["input"]) for r in _golden("trades")]
    rows = rows[::-1]  # reverse = worst case unsorted
    events, rep = load_trades(rows, now_us=NOW_US)
    assert_clean(rep)
    keys = [(e.ts_us, e.payload.trade_id) for e in events]
    assert keys == sorted(keys) and len(events) == len(rows)
