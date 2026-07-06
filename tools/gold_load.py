"""W2.1 typed loaders (PLAN_GOLD_DATA_CONTRACT §2.2) — warehouse rows from
tools/warehouse.py load() -> typed events for the W2.2 book FSM and W2.3 merge.

Pure functions, date-blind: every §2.2 dynamic validation gate inspects the
record, never the calendar. Gates detect AND report; allowed actions are
reject / quarantine / report / continue — silently coercing floats, clamping
prices, fabricating state, or hiding rows is forbidden (no silent repair).

E4 fixed-point everywhere (D5): prices are int dollars*10000, quantities int
size*10000. Any string-encoded number is parsed by integer digit accumulation;
constructing a Python float from input is a gate violation, not a code path
(tests/test_gold_load.py enforces a no-float-token grep gate on this file).
Ints are passed through as already-E4 (staging DuckDB is typed E4).

Empty-side encodings measured on real 2026-07-06 staging: no bid => price 0 +
qty 0; no ask => price 10000 + qty 0. Trades: yes+no price == 10000 exactly.

Events: Event(kind, ts_us, market_ticker, payload); kind is the frozen W1
EVENT_TYPE code from tools/gold_dtype.py. Trades are stable-sorted on
(ts_us, trade_id) and deduped on trade_id (V8-style; duplicates quarantined).
stdlib only.
"""
import csv
import json
import os
import re
import sys
import time
from collections import Counter, namedtuple

try:
    from tools.gold_dtype import EVENT_TYPE  # frozen after W1 — reused, never redefined
except ImportError:  # imported as a plain module from tools/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gold_dtype import EVENT_TYPE

Event = namedtuple("Event", "kind ts_us market_ticker payload")
L1Top = namedtuple("L1Top", "yes_bid_e4 yes_bid_qty_e4 yes_ask_e4 yes_ask_qty_e4 is_snapshot")
Trade = namedtuple("Trade", "trade_id yes_price_e4 no_price_e4 count_e4 taker_side")
BookMsg = namedtuple("BookMsg", "side price_e4 delta_e4 yes_levels no_levels")

TS_MIN_US = 1_704_067_200_000_000      # 2024-01-01 UTC — before any capture era
TS_SLACK_US = 48 * 3600 * 1_000_000    # plausibility headroom past "now"
PRICE_MIN_E4, PRICE_MAX_E4 = 1, 9999   # resting/trade prices live strictly inside (0,1)
_DEC_RE = re.compile(r"(-)?([0-9]+)(?:\.([0-9]+))?")
_TICKER_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")


class GoldLoadError(Exception):
    """Any W2.1 gate failure surfaced to callers."""


class _RowError(GoldLoadError):
    """Internal: one row failed one gate; .reason is a bounded Counter key."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _FloatToken(str):
    """Sentinel for JSON numeric tokens that would otherwise become floats.
    Keeps the raw text so no Python float is ever constructed; any field gate
    seeing one rejects the row as float_dtype (never silently coerced)."""


def parse_e4(v):
    """String dollars/size -> E4 int by integer digit accumulation ("0.0090"
    -> 90, "5119.00" -> 51190000, "-1.25" -> -12500). Ints (non-bool) pass
    through as already-E4. Floats, float tokens, exponent/hex forms, and any
    fraction digits beyond E4 precision are rejected — never coerced."""
    if isinstance(v, bool):
        raise _RowError("bad_type")
    if isinstance(v, int):
        return v
    if "float" in type(v).__name__.lower():   # float, numpy float64, _FloatToken
        raise _RowError("float_dtype")
    if not isinstance(v, str):
        raise _RowError("bad_type")
    m = _DEC_RE.fullmatch(v)
    if not m:
        raise _RowError("malformed_number")
    sign, ip, fp = m.group(1), m.group(2), m.group(3) or ""
    n = 0
    for c in ip:
        n = n * 10 + (ord(c) - 48)
    n *= 10000
    mul = 1000
    for c in fp[:4]:
        n += (ord(c) - 48) * mul
        mul //= 10
    if any(c != "0" for c in fp[4:]):
        raise _RowError("malformed_number")   # sub-E4 precision would be lost
    return -n if sign else n


def render_e4(v, frac_digits):
    """E4 int -> decimal string with exactly frac_digits fraction digits.
    Integer math only; raises instead of rounding when the render is lossy,
    so parse_e4/render_e4 form a byte-exact round trip (V1)."""
    if isinstance(v, bool) or not isinstance(v, int):
        raise GoldLoadError("render_e4 needs an int, got %s" % type(v).__name__)
    if not isinstance(frac_digits, int) or not 0 <= frac_digits <= 16:
        raise GoldLoadError("frac_digits out of range: %r" % (frac_digits,))
    sign = "-" if v < 0 else ""
    ip, fr = divmod(-v if v < 0 else v, 10000)
    if frac_digits <= 4:
        scale = 10 ** (4 - frac_digits)
        if fr % scale:
            raise GoldLoadError("lossy render: %d E4 needs more than %d "
                                "fraction digits" % (v, frac_digits))
        fr //= scale
    else:
        fr *= 10 ** (frac_digits - 4)
    if frac_digits == 0:
        return "%s%d" % (sign, ip)
    return "%s%d.%0*d" % (sign, ip, frac_digits, fr)


class LoaderReport(object):
    """Per-source gate accounting. Rejected rows are quarantined verbatim."""

    def __init__(self, source):
        self.source = source
        self.rows_in = 0
        self.events_out = 0
        self.reasons = Counter()
        self.quarantined = []        # [{"reason": str, "row": original}]
        self.ts_regressions = 0      # non-monotonic input ts — reported, never fixed
        self.max_same_us_run = 0     # suspicious same-µs cluster size — reported

    def reject(self, row, reason):
        self.reasons[reason] += 1
        self.quarantined.append({"reason": reason, "row": row})

    @property
    def malformed_record_count(self):
        return sum(self.reasons.values())

    def summary(self):
        # Operator vocabulary verbatim (PLAN_GOLD_DATA_CONTRACT §3).
        return {"source": self.source,
                "rows_in": self.rows_in,
                "events_out": self.events_out,
                "Malformed Records": self.malformed_record_count,
                "Rejected Rows": dict(self.reasons),
                "Quarantined Input Rows": len(self.quarantined),
                "ts_regressions_reported": self.ts_regressions,
                "max_same_us_run": self.max_same_us_run}


def assert_clean(report):
    """Raise unless the source had zero rejected rows (report-only counters
    like ts_regressions do not fail a batch — they are surfaced, D2)."""
    if report.malformed_record_count:
        raise GoldLoadError("source %r not clean: %s"
                            % (report.source, dict(report.reasons)))


def iter_ndjson(lines, report):
    """NDJSON lines -> row dicts through the corrupt-line gate. Float tokens
    become _FloatToken sentinels (rejected later by field gates)."""
    for line in lines:
        s = line.strip()
        if not s:
            continue
        try:
            row = json.loads(s, parse_float=_FloatToken, parse_constant=_FloatToken)
        except ValueError:
            report.reject(line.rstrip("\n"), "malformed_json_line")
            continue
        if not isinstance(row, dict):
            report.reject(line.rstrip("\n"), "malformed_json_line")
            continue
        yield row


# ---------------------------------------------------------------- field gates

def _need(row, key):
    v = row.get(key)
    if v is None:
        raise _RowError("missing_field:%s" % key)
    return v


def _e4(row, key):
    try:
        return parse_e4(_need(row, key))
    except _RowError as e:
        if e.reason.startswith("missing_field"):
            raise
        raise _RowError("%s:%s" % (e.reason, key))


def _price(row, key):
    v = _e4(row, key)
    if not PRICE_MIN_E4 <= v <= PRICE_MAX_E4:
        raise _RowError("price_out_of_range:%s" % key)
    return v


def _qty(row, key, allow_zero=False):
    v = _e4(row, key)
    if v < 0 or (v == 0 and not allow_zero):
        raise _RowError("bad_quantity:%s" % key)
    return v


def _ts(row, now_us):
    v = _need(row, "ts_utc")
    if isinstance(v, bool) or "float" in type(v).__name__.lower():
        raise _RowError("bad_timestamp")
    if isinstance(v, str):
        if not (v.isascii() and v.isdigit()):
            raise _RowError("bad_timestamp")
        v = int(v)
    if not isinstance(v, int) or not TS_MIN_US <= v <= now_us + TS_SLACK_US:
        raise _RowError("bad_timestamp")
    return v


def _ticker(row, key="market_ticker"):
    v = _need(row, key)
    if not isinstance(v, str) or not _TICKER_RE.fullmatch(v):
        raise _RowError("invalid_ticker:%s" % key)
    return v


def _opt_tickers(row):
    for key in ("series_ticker", "event_ticker"):
        if row.get(key) is not None:
            _ticker(row, key)


def _levels(row, key):
    v = _need(row, key)
    if isinstance(v, str) and not isinstance(v, _FloatToken):
        try:
            v = json.loads(v, parse_float=_FloatToken, parse_constant=_FloatToken)
        except ValueError:
            raise _RowError("malformed_json:%s" % key)
    if not isinstance(v, list):
        raise _RowError("malformed_level_array:%s" % key)
    out = []
    for lvl in v:
        if not isinstance(lvl, list) or len(lvl) != 2:
            raise _RowError("malformed_level_array:%s" % key)
        p, q = lvl
        for x in (p, q):
            if isinstance(x, bool) or "float" in type(x).__name__.lower():
                raise _RowError("float_dtype:%s" % key)
            if not isinstance(x, int):
                raise _RowError("malformed_level_array:%s" % key)
        if not PRICE_MIN_E4 <= p <= PRICE_MAX_E4:
            raise _RowError("price_out_of_range:%s" % key)
        if q <= 0:
            raise _RowError("bad_quantity:%s" % key)
        out.append((p, q))
    return tuple(out)


# ---------------------------------------------------------------- row -> event

def _trade_event(row, now_us):
    ts = _ts(row, now_us)
    mt = _ticker(row)
    _opt_tickers(row)
    tid = _need(row, "trade_id")
    if not isinstance(tid, str) or not 1 <= len(tid) <= 64:
        raise _RowError("invalid_trade_id")
    yp = _price(row, "yes_price_e4")
    np_ = _price(row, "no_price_e4")
    if yp + np_ != 10000:
        raise _RowError("inconsistent_price_pair")
    ct = _qty(row, "count_e4")
    side = _need(row, "taker_side")
    if side not in ("yes", "no"):
        raise _RowError("invalid_taker_side")
    return Event(EVENT_TYPE["TRADE"], ts, mt, Trade(tid, yp, np_, ct, side))


def _l1_event(row, now_us):
    ts = _ts(row, now_us)
    mt = _ticker(row)
    _opt_tickers(row)
    bid = _e4(row, "yes_bid_e4")
    ask = _e4(row, "yes_ask_e4")
    bq = _qty(row, "yes_bid_qty_e4", allow_zero=True)
    aq = _qty(row, "yes_ask_qty_e4", allow_zero=True)
    if bid == 0:                       # empty-bid sentinel — qty must agree
        if bq != 0:
            raise _RowError("empty_side_qty_mismatch:bid")
    elif not PRICE_MIN_E4 <= bid <= PRICE_MAX_E4:
        raise _RowError("price_out_of_range:yes_bid_e4")
    if ask == 10000:                   # empty-ask sentinel — qty must agree
        if aq != 0:
            raise _RowError("empty_side_qty_mismatch:ask")
    elif not PRICE_MIN_E4 <= ask <= PRICE_MAX_E4:
        raise _RowError("price_out_of_range:yes_ask_e4")
    snap = row.get("is_snapshot")
    if not isinstance(snap, bool):
        raise _RowError("missing_field:is_snapshot" if snap is None
                        else "bad_type:is_snapshot")
    return Event(EVENT_TYPE["L1_TICKER"], ts, mt, L1Top(bid, bq, ask, aq, snap))


def _full_event(row, now_us):
    ts = _ts(row, now_us)
    mt = _ticker(row)
    _opt_tickers(row)
    mtype = _need(row, "msg_type")
    if mtype == "snapshot":
        yl = _levels(row, "yes_levels")
        nl = _levels(row, "no_levels")
        return Event(EVENT_TYPE["BOOK_SNAPSHOT"], ts, mt,
                     BookMsg(None, None, None, yl, nl))
    if mtype == "delta":
        side = _need(row, "side")
        if side not in ("yes", "no"):
            raise _RowError("invalid_side")
        px = _price(row, "price_e4")
        d = _e4(row, "delta_e4")
        if d == 0:
            raise _RowError("bad_delta")
        return Event(EVENT_TYPE["BOOK_DELTA"], ts, mt,
                     BookMsg(side, px, d, None, None))
    raise _RowError("bad_msg_type")


# -------------------------------------------------------------- public loaders

def _load(rows, source, now_us, build):
    rep = LoaderReport(source)
    if now_us is None:
        now_us = time.time_ns() // 1000
    # Timestamp monotonicity / same-µs clusters are tracked PER MARKET:
    # warehouse load() orders by (market_ticker, ts_utc), so a global tracker
    # would count every market boundary as a false regression. Report-only.
    pairs, last = [], {}   # market_ticker -> [last_ts, same_us_run]
    for row in rows:
        rep.rows_in += 1
        if not isinstance(row, dict):
            rep.reject(row, "bad_row_type")
            continue
        try:
            ev = build(row, now_us)
        except _RowError as e:
            rep.reject(row, e.reason)
            continue
        st = last.get(ev.market_ticker)
        if st is None:
            st = last[ev.market_ticker] = [ev.ts_us, 1]
        else:
            if ev.ts_us < st[0]:
                rep.ts_regressions += 1    # reported, never reordered
            st[1] = st[1] + 1 if ev.ts_us == st[0] else 1
            st[0] = ev.ts_us
        if st[1] > rep.max_same_us_run:
            rep.max_same_us_run = st[1]
        pairs.append((ev, row))
    rep.events_out = len(pairs)
    return pairs, rep


def load_l1(rows, source="orderbooks_l1", now_us=None):
    """L1 change/heartbeat rows -> L1_TICKER events (input order preserved)."""
    pairs, rep = _load(rows, source, now_us, _l1_event)
    return [e for e, _ in pairs], rep


def load_full(rows, source="orderbooks_full", now_us=None):
    """Full-depth rows -> BOOK_SNAPSHOT / BOOK_DELTA events (input order)."""
    pairs, rep = _load(rows, source, now_us, _full_event)
    return [e for e, _ in pairs], rep


def load_trades(rows, source="trades", now_us=None):
    """Trade rows -> TRADE events, stable-sorted (ts_us, trade_id), deduped
    on trade_id (V8-style: first print kept, duplicates quarantined)."""
    pairs, rep = _load(rows, source, now_us, _trade_event)
    pairs.sort(key=lambda p: (p[0].ts_us, p[0].payload.trade_id))
    events, seen = [], set()
    for ev, row in pairs:
        if ev.payload.trade_id in seen:
            rep.reject(row, "duplicate_trade_id")
            rep.events_out -= 1
            continue
        seen.add(ev.payload.trade_id)
        events.append(ev)
    return events, rep


def write_outputs(reports, out_dir, tag=None, sample_max=50):
    """Persist the operator-facing loader outputs (PLAN §2.2): loader report
    JSON, per-source quarantine NDJSON (original rows preserved verbatim),
    and a malformed_record_sample CSV. Returns the written paths."""
    if tag is None:
        tag = str(time.time_ns() // 1000)
    qdir = os.path.join(out_dir, "quarantine")
    os.makedirs(qdir, exist_ok=True)
    report_path = os.path.join(out_dir, "loader_report_%s.json" % tag)
    sample_path = os.path.join(out_dir, "malformed_record_sample_%s.csv" % tag)
    with open(report_path, "w") as f:
        json.dump({"tag": tag, "generated_us": time.time_ns() // 1000,
                   "sources": [r.summary() for r in reports]},
                  f, indent=1, default=str)
    with open(sample_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source", "reason", "row_json"])
        for r in reports:
            for q in r.quarantined[:sample_max]:
                w.writerow([r.source, q["reason"], json.dumps(q["row"], default=str)])
    for r in reports:
        if r.quarantined:
            with open(os.path.join(qdir, "%s_%s.ndjson" % (r.source, tag)), "w") as f:
                for q in r.quarantined:
                    f.write(json.dumps(q, default=str) + "\n")
    return {"report": report_path, "sample_csv": sample_path, "quarantine_dir": qdir}
