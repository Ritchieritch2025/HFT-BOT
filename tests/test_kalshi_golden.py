"""W3.3: golden Kalshi frames — V13 field semantics pinned against REAL
captured frames (PLAN_GOLD_DATA_CONTRACT §2.3 V13, §W3.3).

Contract encoded here, written BEFORE the fixtures were extracted (TDD):
  (a) per-sid seq scoping: within the sampled orderbook_delta frames, `seq`
      increases strictly monotonically per (sid, stream_epoch) ACROSS ALL
      markets on the sid (protocol invariant I1) — observed semantics are
      documented, never forced;
  (b) trade_id is the dedupe key: all 50 real trade frames carry distinct,
      UUID-shaped trade_ids;
  (c) fixed-point strings: every price/qty field is a STRING-encoded decimal
      that round-trips byte-exact through tools/gold_load.py integer digit
      accumulation (parse_e4 -> render_e4, V1 machinery reused, never
      reimplemented);
  (d) taker_side is exactly 'yes' or 'no';
  (e) ts consistency: trade `ts` (epoch seconds int) agrees with `ts_ms`
      (epoch ms int) to within 1 s; orderbook_delta `ts` is an ISO-8601
      STRING (real semantics — not epoch seconds) and agrees with its
      `ts_ms` to within 1 s; orderbook_snapshot msgs carry NO ts fields at
      all (real semantics, asserted);
  (f) V12 wiring: this module SKIPS loudly when the SAVED
      work/kalshi_spec_alignment.json is missing/red/older than 7 days —
      mirroring tools/gold_build.py::spec_gate (never runs the network sync).

Anti-fake-green (plan §3 binding rule): doctored-frame fixtures in
tests/fixtures/gold_defects/ (duplicated trade_id, per-sid seq regression,
float-typed price) are proven RED through the same checkers the real-frame
tests use.

Observed REAL semantics pinned here that contradict/extend
docs/kalshi_ws_protocol.md (doc drift filed in docs/BACKLOG.md, not fixed
here — W3.3 may not write that doc):
  - trade frames DO carry sid+seq on their own sid (doc I9 says no seq);
  - orderbook_delta `ts` is ISO-8601 microsecond string, trade `ts` is
    epoch-seconds int — same field name, two encodings;
  - orderbook_snapshot msg has NO ts/ts_ms;
  - in-stream get_snapshot stamps the NEXT sid seq (observed seq 2025 mid
    delta stream), answering the protocol doc's open question #2.
"""
import datetime
import json
import os
import re
import time

import pytest

from tools.gold_load import parse_e4, render_e4

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "tests", "fixtures", "kalshi_golden")
DEFECTS = os.path.join(ROOT, "tests", "fixtures", "gold_defects")
SPEC_PATH = os.path.join(ROOT, "work", "kalshi_spec_alignment.json")
SPEC_MAX_AGE_DAYS = 7  # keep in lockstep with tools/gold_build.py

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}"
                     r"-[0-9a-f]{12}$")
# resting/trade prices live strictly inside (0, 1) dollars == (1, 9999) E4
PRICE_MIN_E4, PRICE_MAX_E4 = 1, 9999


# ------------------------------------------------------------ (f) V12 gate

def _spec_gate(path=SPEC_PATH, max_age_days=SPEC_MAX_AGE_DAYS):
    """Mirror of tools/gold_build.py::spec_gate on the SAVED sync result.
    Deliberately duplicated (not imported): importing gold_build drags in
    the warehouse/duckdb stack, and the WP forbids importing anything that
    could touch the network path. Returns (ok, message)."""
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        return False, "saved spec result unreadable (%s): %s" % (path, e)
    status = d.get("status")
    age_days = (time.time() * 1000.0 - d.get("generated_at_ms", 0)) / 86400000.0
    if status != "pass":
        return False, "saved status=%r (need 'pass')" % (status,)
    if age_days > max_age_days:
        return False, "saved result is %.1f days old (max %d)" % (age_days,
                                                                  max_age_days)
    return True, "status=pass, %.2f days old" % age_days


_SPEC_OK, _SPEC_MSG = _spec_gate()
if not _SPEC_OK:
    pytest.skip(
        "V12 SPEC-DRIFT GATE RED — golden-frame semantics cannot be trusted "
        "against a stale/failed spec alignment (%s). Operator: rerun "
        "./tools/kalshi_spec_sync.py --allow-network, then re-run this suite."
        % _SPEC_MSG,
        allow_module_level=True)


def test_v12_gate_logic_goes_red(tmp_path):
    """The skip above must be able to fire: missing / red / stale saved
    results are each refused by the mirrored gate (anti-fake-green for the
    module-level skip)."""
    ok, msg = _spec_gate(str(tmp_path / "missing.json"))
    assert not ok and "unreadable" in msg

    red = tmp_path / "red.json"
    red.write_text(json.dumps(
        {"status": "fail", "generated_at_ms": time.time() * 1000.0}))
    ok, msg = _spec_gate(str(red))
    assert not ok and "status" in msg

    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps(
        {"status": "pass",
         "generated_at_ms": time.time() * 1000.0 - 8 * 86400000.0}))
    ok, msg = _spec_gate(str(stale))
    assert not ok and "days old" in msg

    fresh = tmp_path / "fresh.json"
    fresh.write_text(json.dumps(
        {"status": "pass", "generated_at_ms": time.time() * 1000.0}))
    ok, _ = _spec_gate(str(fresh))
    assert ok


# ------------------------------------------------------------ fixture I/O

def _records(path):
    """RawRecord lines (capture envelope, verbatim) -> list of dicts with
    the inner Kalshi frame decoded under 'frame'."""
    out = []
    with open(path) as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            rec["frame"] = json.loads(rec["raw"])
            out.append(rec)
    return out


def _golden(name):
    return _records(os.path.join(GOLDEN, name))


def _defect(name):
    return _records(os.path.join(DEFECTS, name))


# ------------------------------------------------------------ checkers
# The SAME functions run on real fixtures (must pass) and on doctored
# defect fixtures (must raise) — a checker that has never been red is
# unproven (plan §3 / D2).

def check_seq_per_sid(records):
    """(a) `seq` strictly increases per (sid, stream_epoch), across ALL
    markets on the sid. Envelope sid must agree with the frame's own sid."""
    last = {}
    for rec in records:
        fr = rec["frame"]
        assert fr.get("sid") == rec.get("sid"), \
            "envelope sid %r != frame sid %r" % (rec.get("sid"), fr.get("sid"))
        key = (fr["sid"], rec.get("stream_epoch"))
        seq = fr["seq"]
        assert isinstance(seq, int) and not isinstance(seq, bool)
        prev = last.get(key)
        assert prev is None or seq > prev, \
            "per-sid seq regression on sid=%r epoch=%r: %r after %r" % (
                fr["sid"], rec.get("stream_epoch"), seq, prev)
        last[key] = seq


def check_trade_ids(records):
    """(b) trade_id: present, UUID-shaped, distinct across all frames."""
    seen = {}
    for rec in records:
        tid = rec["frame"]["msg"]["trade_id"]
        assert isinstance(tid, str) and UUID_RE.fullmatch(tid), \
            "trade_id not UUID-shaped: %r" % (tid,)
        assert tid not in seen, "duplicate trade_id (dedupe key): %r" % (tid,)
        seen[tid] = True
    return len(seen)


def check_fp_string(s, price=False):
    """(c) one fixed-point field: STRING-encoded decimal that byte-exact
    round-trips through gold_load integer digit accumulation (E4)."""
    assert isinstance(s, str), \
        "fixed-point field is %s, not a string: %r" % (type(s).__name__, s)
    v = parse_e4(s)  # raises _RowError on floats/malformed — that's a fail
    frac = len(s.partition(".")[2])
    assert render_e4(v, frac) == s, \
        "not byte-exact through E4: %r -> %d -> %r" % (s, v, render_e4(v, frac))
    if price:
        assert PRICE_MIN_E4 <= v <= PRICE_MAX_E4, \
            "price outside (0,1) dollars: %r" % (s,)
    return v


def check_trade_fp(records):
    for rec in records:
        msg = rec["frame"]["msg"]
        yes = check_fp_string(msg["yes_price_dollars"], price=True)
        no = check_fp_string(msg["no_price_dollars"], price=True)
        assert yes + no == 10000, \
            "yes+no price != $1: %r + %r" % (msg["yes_price_dollars"],
                                             msg["no_price_dollars"])
        count = check_fp_string(msg["count_fp"])
        assert count > 0, "non-positive trade count: %r" % (msg["count_fp"],)


def check_delta_fp(records):
    for rec in records:
        msg = rec["frame"]["msg"]
        check_fp_string(msg["price_dollars"], price=True)
        delta = check_fp_string(msg["delta_fp"])  # negative is legitimate
        assert delta != 0, "zero delta_fp: %r" % (msg["delta_fp"],)


def check_snapshot_fp(msg):
    n_levels = 0
    for side in ("yes_dollars_fp", "no_dollars_fp"):
        for level in msg[side]:
            price_s, qty_s = level
            check_fp_string(price_s, price=True)
            qty = check_fp_string(qty_s)
            assert qty > 0, "non-positive snapshot level qty: %r" % (qty_s,)
            n_levels += 1
    return n_levels


def check_taker_side(records):
    for rec in records:
        ts = rec["frame"]["msg"]["taker_side"]
        assert ts in ("yes", "no"), "taker_side not exactly yes/no: %r" % (ts,)


_ISO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$")


def _iso_to_epoch(s):
    """Kalshi delta ts, e.g. "2026-07-06T03:14:05.791454Z". REAL semantics:
    the fractional part is VARIABLE length (trailing zeros trimmed, e.g.
    ".52251Z") — datetime.fromisoformat rejects that on this interpreter,
    so parse the fraction by hand. UTC 'Z' suffix only, as observed."""
    m = _ISO_RE.fullmatch(s)
    assert m, "delta ts not ISO-8601 Zulu: %r" % (s,)
    base = datetime.datetime.strptime(
        m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=datetime.timezone.utc).timestamp()
    frac = m.group(2) or "0"
    return base + int(frac) / 10.0 ** len(frac)


def check_ts_consistency(records):
    """(e) ts vs ts_ms within 1 s where both present. Trade ts is an
    epoch-seconds int; delta ts is an ISO-8601 string (REAL semantics)."""
    for rec in records:
        msg = rec["frame"]["msg"]
        if "ts" not in msg or "ts_ms" not in msg:
            continue
        ts, ts_ms = msg["ts"], msg["ts_ms"]
        assert isinstance(ts_ms, int) and not isinstance(ts_ms, bool)
        if isinstance(ts, str):
            epoch = _iso_to_epoch(ts)
        else:
            assert isinstance(ts, int) and not isinstance(ts, bool)
            epoch = float(ts)
        assert abs(epoch - ts_ms / 1000.0) <= 1.0, \
            "ts/ts_ms disagree by >1s: %r vs %r" % (ts, ts_ms)


# ------------------------------------------------------------ the fixtures

def test_fixture_shape_and_provenance():
    """Exactly 1 real snapshot + 50 real deltas + 50 real trades, channels
    as labeled, and the README documenting source + window exists."""
    snaps = _golden("orderbook_snapshot.ndjson")
    deltas = _golden("orderbook_deltas.ndjson")
    trades = _golden("trades.ndjson")
    assert len(snaps) == 1
    assert len(deltas) == 50
    assert len(trades) == 50
    assert all(r["frame"]["type"] == "orderbook_snapshot" for r in snaps)
    assert all(r["frame"]["type"] == "orderbook_delta" for r in deltas)
    assert all(r["frame"]["type"] == "trade" for r in trades)
    # capture envelope channel agrees with the frame's own type
    assert all(r["channel"] == r["frame"]["type"]
               for r in snaps + deltas + trades)
    with open(os.path.join(GOLDEN, "README.md")) as f:
        readme = f.read()
    for token in ("live_capture.ndjson", "firehose", "window"):
        assert token in readme, "README missing provenance token %r" % token


# ------------------------------------------------------------ (a) seq scope

def test_a_delta_seq_monotonic_per_sid():
    deltas = _golden("orderbook_deltas.ndjson")
    check_seq_per_sid(deltas)
    # Observed epoch semantics, documented not forced: this sample is one
    # connection epoch, so no seq reset appears; a reset would come with a
    # new stream_epoch (protocol I8) and a NEW (sid, epoch) key above.
    assert {r.get("stream_epoch") for r in deltas} == {1}


def test_a_trade_frames_also_carry_per_sid_seq():
    """REAL semantics: trade frames carry sid+seq too (doc I9 says they do
    not — drift filed in BACKLOG). Pin what the wire actually does."""
    trades = _golden("trades.ndjson")
    assert all("seq" in r["frame"] and "sid" in r["frame"] for r in trades)
    check_seq_per_sid(trades)


def test_a_snapshot_seq_shares_the_sid_counter():
    """The snapshot's seq is a normal sequenced observation on its sid
    (in-stream get_snapshot stamps the next sid seq — protocol doc open
    question #2, answered empirically)."""
    (snap,) = _golden("orderbook_snapshot.ndjson")
    assert isinstance(snap["frame"]["seq"], int)
    assert snap["frame"]["sid"] == snap["sid"]


# ------------------------------------------------------------ (b) trade_id

def test_b_trade_ids_distinct_and_uuid_shaped():
    assert check_trade_ids(_golden("trades.ndjson")) == 50


# ------------------------------------------------------------ (c) fixed point

def test_c_trade_fixed_point_strings():
    check_trade_fp(_golden("trades.ndjson"))


def test_c_delta_fixed_point_strings():
    check_delta_fp(_golden("orderbook_deltas.ndjson"))


def test_c_snapshot_fixed_point_strings():
    (snap,) = _golden("orderbook_snapshot.ndjson")
    assert check_snapshot_fp(snap["frame"]["msg"]) > 0


# ------------------------------------------------------------ (d) taker_side

def test_d_taker_side_values():
    check_taker_side(_golden("trades.ndjson"))


# ------------------------------------------------------------ (e) timestamps

def test_e_trade_ts_vs_ts_ms():
    trades = _golden("trades.ndjson")
    # every trade has both fields, ts an epoch-seconds int
    assert all(isinstance(r["frame"]["msg"].get("ts"), int) and
               isinstance(r["frame"]["msg"].get("ts_ms"), int)
               for r in trades)
    check_ts_consistency(trades)


def test_e_delta_ts_is_iso_string_and_agrees_with_ts_ms():
    deltas = _golden("orderbook_deltas.ndjson")
    assert all(isinstance(r["frame"]["msg"].get("ts"), str) and
               isinstance(r["frame"]["msg"].get("ts_ms"), int)
               for r in deltas)
    check_ts_consistency(deltas)


def test_e_snapshot_has_no_ts_fields():
    (snap,) = _golden("orderbook_snapshot.ndjson")
    msg = snap["frame"]["msg"]
    assert "ts" not in msg and "ts_ms" not in msg
    assert set(msg) == {"market_ticker", "market_id",
                        "yes_dollars_fp", "no_dollars_fp"}


# ------------------------------------------------ doctored-frame red proofs

def test_red_doctored_duplicate_trade_id_is_caught():
    with pytest.raises(AssertionError, match="duplicate trade_id"):
        check_trade_ids(_defect("golden_trade_dup_trade_id.ndjson"))


def test_red_doctored_seq_regression_is_caught():
    with pytest.raises(AssertionError, match="seq regression"):
        check_seq_per_sid(_defect("golden_delta_seq_regression.ndjson"))


def test_red_doctored_float_price_is_caught():
    # yes_price_dollars tampered into a JSON number -> json.loads makes a
    # float -> the string gate must refuse it (never coerced, D5/V1)
    with pytest.raises(AssertionError, match="not a string"):
        check_trade_fp(_defect("golden_trade_float_price.ndjson"))
