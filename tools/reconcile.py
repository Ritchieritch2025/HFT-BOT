#!/usr/bin/env python3
"""W-K5 — reconcile loop (PLAN_RISK_KILLSWITCH §3, contract #8).

Cold-path, periodic: pull the EXCHANGE's resting orders + positions and diff
them against the ENGINE's own snapshot of what it thinks is true. Memory is a
cache; the exchange is the truth (S2). ANY mismatch => an alarm on the
dashboard alert stream + a structured report; the recommended action is
ALWAYS "adopt the exchange" / investigate — NEVER "resend" or "retry" (blind
retry after an ambiguous state is how you double-fill).

This is a READ-ONLY consumer: it mutates no engine state and transmits no
order. In this plan there is no live engine yet, so it runs against mock +
synthetic snapshots; the engine-snapshot format below is the contract Phase-2's
shadow engine must export.

Engine snapshot (JSON), the contract:
  {"resting_orders": [{"client_order_id": "...",  # REQUIRED — the join key
                       "order_id": "...",         # optional (exchange-assigned)
                       "ticker": "...", "book_side": "bid",
                       "remaining_count_fp_e4": 50000,
                       "yes_price_e6": 560000},    # optional; compared if present
                      ...],
   "positions":      [{"ticker": "...", "position_fp_e4": -50000}, ...]}

Orders are joined on **client_order_id**, NOT order_id (audit N1): the engine
generates the client_order_id deterministically (contract #9) and thus ALWAYS
knows it, while the exchange's order_id is learned from a create-ack — which
is exactly what is LOST in the ack-loss scenario reconcile exists to catch.
Joining on order_id would split one physical order into a false
only-at-exchange + only-in-engine pair, and tell the engine a LIVE order is
terminal. Money/counts are strict fixed-point integers (floats/bools rejected,
never coerced — same D3/D5 discipline as account_view).

Exchange state comes from the same typed, byte-exact reader as the panic
verify path: offline (a fixture JSON, same shape) for tests, or live via
tools/account_view (network_read, read-only) for the real check.

Drift taxonomy (each carries an exchange-wins recommendation):
  ORDER_ONLY_AT_EXCHANGE  exchange rests an order the engine doesn't track
                          (e.g. a cancel whose ack was lost) -> the engine
                          must adopt/track or cancel it; NEVER assume it's gone
  ORDER_ONLY_IN_ENGINE    engine thinks an order rests but the exchange has no
                          such resting order (already filled/canceled; a
                          missed update) -> adopt exchange: mark terminal, do
                          NOT resend
  ORDER_ATTR_DRIFT        both rest but remaining/side differ (a missed
                          partial fill) -> adopt the exchange's remaining
  POSITION_DRIFT          a ticker's position differs -> a missed fill; adopt
                          the exchange position; consider panic if large

Exit code: 0 = CLEAN (with comparison counts printed, D2 — a green must never
lie); 1 = drift found (alarmed + reported); 2 = could not read a side
(fail-closed: an unknown state is never "clean").

Usage:
  python3 tools/reconcile.py --engine ENGINE.json --exchange FIXTURE.json
  python3 tools/reconcile.py --engine ENGINE.json --live          # account_view
  python3 tools/reconcile.py ... --report work/live/reconcile_report.json
"""
import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERT_LOG = os.path.join(ROOT, "work", "live", "alerts.log")

# The recommended actions are a fixed vocabulary; NONE of them is a resend —
# a grep-gate in the test asserts "resend"/"retry" never appears here.
REMEDY = {
    "ORDER_ONLY_AT_EXCHANGE": "adopt: engine must track or cancel this resting "
                              "order; never assume it is gone",
    "ORDER_ONLY_IN_ENGINE":   "adopt exchange: order is terminal "
                              "(filled/canceled); update engine, do not resend",
    "ORDER_ATTR_DRIFT":       "adopt exchange remaining/price (missed partial "
                              "fill); do not resend",
    "POSITION_DRIFT":         "adopt exchange position (missed fill)",
    "POSITION_DRIFT_LARGE":   "adopt exchange position (LARGE missed fill) — "
                              "RUN PANIC, do not resend",
}

# A position delta at or beyond this (E4 contracts, i.e. contracts x 10^4)
# escalates POSITION_DRIFT to a hard panic recommendation (audit N6). Default
# = 100 contracts; override with --large-position-delta.
LARGE_POSITION_DELTA_E4 = 100 * 10000


class ReconcileError(RuntimeError):
    """A side could not be read — fail closed (never report CLEAN)."""


def _load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ReconcileError("cannot read %s: %s" % (path, e)) from None


def _strict_int(v, source, what):
    """Fixed-point fields are INTEGERS. A float/bool/str is a malformed
    record (D3/D5) — reject it, never coerce (a coerced float would produce a
    false CLEAN). bool is an int subclass, so exclude it explicitly."""
    if type(v) is not int:
        raise ReconcileError("%s %s not an integer: %r" % (source, what, v))
    return v


def _index_orders(orders, source):
    """client_order_id -> normalized order dict (audit N1: join on the id the
    engine always knows). Fail-closed on any malformed record."""
    out = {}
    for o in orders:
        cid = o.get("client_order_id")
        if not isinstance(cid, str) or not cid:
            raise ReconcileError("%s order missing client_order_id: %r"
                                 % (source, o))
        try:
            rec = {
                "client_order_id": cid,
                "order_id": str(o.get("order_id", "")),
                "ticker": str(o["ticker"]),
                "book_side": str(o.get("book_side", "")),
                "remaining": _strict_int(o["remaining_count_fp_e4"], source,
                                         "remaining_count_fp_e4"),
            }
        except KeyError as e:
            raise ReconcileError("%s order %s missing %s" % (source, cid, e))
        if "yes_price_e6" in o:          # optional; compared only if present
            rec["price"] = _strict_int(o["yes_price_e6"], source, "yes_price_e6")
        out[cid] = rec
    return out


def _index_positions(positions, source):
    out = {}
    for p in positions:
        tk = p.get("ticker")
        if not isinstance(tk, str) or not tk:
            raise ReconcileError("%s position missing ticker: %r" % (source, p))
        try:
            out[tk] = _strict_int(p["position_fp_e4"], source, "position_fp_e4")
        except KeyError as e:
            raise ReconcileError("%s position %s missing %s" % (source, tk, e))
    return out


def reconcile(exchange, engine, large_delta=LARGE_POSITION_DELTA_E4):
    """Pure diff. Returns (drifts, counts). Orders join on client_order_id;
    the exchange is truth. A position delta >= large_delta escalates to a
    hard panic recommendation."""
    ex_o = _index_orders(exchange.get("resting_orders", []), "exchange")
    en_o = _index_orders(engine.get("resting_orders", []), "engine")
    ex_p = _index_positions(exchange.get("positions", []), "exchange")
    en_p = _index_positions(engine.get("positions", []), "engine")

    drifts = []

    def drift(cls, key, detail):
        drifts.append({"class": cls, "key": key, "detail": detail,
                       "recommend": REMEDY[cls]})

    # orders present only at the exchange, or only in the engine, or drifted —
    # keyed on client_order_id (audit N1)
    for cid in sorted(set(ex_o) | set(en_o)):
        ex, en = ex_o.get(cid), en_o.get(cid)
        if ex and not en:
            drift("ORDER_ONLY_AT_EXCHANGE", cid,
                  "exchange rests %s %s rem=%d (order_id=%s); engine unaware"
                  % (ex["ticker"], ex["book_side"], ex["remaining"],
                     ex["order_id"] or "?"))
        elif en and not ex:
            drift("ORDER_ONLY_IN_ENGINE", cid,
                  "engine tracks %s %s rem=%d; not resting at exchange"
                  % (en["ticker"], en["book_side"], en["remaining"]))
        else:
            diffs = []
            if ex["remaining"] != en["remaining"]:
                diffs.append("rem %d!=%d" % (ex["remaining"], en["remaining"]))
            if ex["book_side"] != en["book_side"]:
                diffs.append("side %s!=%s" % (ex["book_side"], en["book_side"]))
            # price compared only when BOTH sides carry it (audit N3)
            if "price" in ex and "price" in en and ex["price"] != en["price"]:
                diffs.append("price %d!=%d" % (ex["price"], en["price"]))
            if diffs:
                drift("ORDER_ATTR_DRIFT", cid, "exchange vs engine: " +
                      ", ".join(diffs))

    # positions: any ticker whose signed size differs (0 on a missing side)
    for tk in sorted(set(ex_p) | set(en_p)):
        ev, nv = ex_p.get(tk, 0), en_p.get(tk, 0)
        if ev != nv:
            cls = ("POSITION_DRIFT_LARGE" if abs(ev - nv) >= large_delta
                   else "POSITION_DRIFT")
            drift(cls, tk, "exchange position_fp_e4=%d vs engine=%d (delta=%d)"
                  % (ev, nv, ev - nv))

    counts = {"orders_compared": len(set(ex_o) | set(en_o)),
              "positions_compared": len(set(ex_p) | set(en_p)),
              "exchange_orders": len(ex_o), "engine_orders": len(en_o)}
    return drifts, counts


def load_exchange_live():
    """Pull exchange resting orders + positions via account_view (read-only,
    network_read). Normalized to the reconcile shape."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import account_view as av
    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        raise ReconcileError("live mode needs KALSHI_API_KEY_ID / "
                             "KALSHI_PRIVATE_KEY_PATH (source ~/.kalshi/env.sh)")
    base = os.environ.get("KALSHI_BASE_URL", av.PROD_BASE)
    try:
        orders, od = av.get_resting_orders(base, key_id, key_path)
        mkt, _evt, pd = av.get_positions(base, key_id, key_path)
    except av.AccountViewError as e:
        raise ReconcileError("live exchange read failed: %s" % e) from None
    dropped = sum(od.values()) + sum(pd.values())
    if dropped:
        raise ReconcileError("live read dropped %d record(s) at the field "
                             "gates %s — refusing to reconcile against a "
                             "partial exchange view" % (dropped, {**od, **pd}))
    return {
        "resting_orders": [{"client_order_id": o["client_order_id"],
                            "order_id": o["order_id"], "ticker": o["ticker"],
                            "book_side": o["book_side"],
                            "remaining_count_fp_e4": o["remaining_count_fp_e4"],
                            "yes_price_e6": o["yes_price_e6"]}
                           for o in orders],
        "positions": [{"ticker": p["ticker"],
                       "position_fp_e4": p["position_fp_e4"]} for p in mkt],
    }


def alarm(msg):
    """Append one line to work/live/alerts.log (the same FILE W-A5's
    alert_notify writes). HONESTY NOTE (audit N4): no component currently
    FORWARDS reconcile: lines to Telegram/push — alert_notify.sh checks only
    capture/freshness/disk, and nothing reads this log for delivery. So the
    operator-facing signal is the EXIT CODE (1 = drift); the log line is a
    record. Wiring reconcile drift into the delivery path is a BACKLOG item.
    Best-effort append; never raises."""
    try:
        os.makedirs(os.path.dirname(ALERT_LOG), exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(ALERT_LOG, "a") as f:
            f.write("%s reconcile:%s\n" % (ts, msg))
    except OSError:
        pass


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", required=True, help="engine-state snapshot JSON")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--exchange", help="exchange snapshot fixture JSON (offline)")
    src.add_argument("--live", action="store_true",
                     help="pull the exchange live via account_view (read-only)")
    ap.add_argument("--report", default=None, help="write a JSON report here")
    ap.add_argument("--large-position-delta", type=int,
                    default=LARGE_POSITION_DELTA_E4,
                    help="position delta (E4) at/above which drift escalates to "
                         "a hard panic recommendation (default 100 contracts)")
    args = ap.parse_args(argv[1:])

    try:
        engine = _load_json(args.engine)
        exchange = load_exchange_live() if args.live else _load_json(args.exchange)
        drifts, counts = reconcile(exchange, engine, args.large_position_delta)
    except ReconcileError as e:
        print("RECONCILE FAIL (closed): %s" % e, file=sys.stderr)
        alarm("READ-ERROR %s" % e)
        return 2

    clean = not drifts
    report = {"clean": clean, "counts": counts, "drifts": drifts,
              "policy": "exchange_wins_never_retry",
              "generated_at": datetime.datetime.now(datetime.timezone.utc)
              .strftime("%Y-%m-%dT%H:%M:%SZ")}
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w") as f:
            json.dump(report, f, indent=2)

    # D2: ALWAYS print the comparison counts, clean or not.
    print("reconcile: compared %d order(s), %d position(s) "
          "(exchange orders=%d, engine orders=%d)"
          % (counts["orders_compared"], counts["positions_compared"],
             counts["exchange_orders"], counts["engine_orders"]))
    if clean:
        print("RECONCILE CLEAN — engine matches the exchange")
        return 0
    by_class = {}
    for d in drifts:
        by_class[d["class"]] = by_class.get(d["class"], 0) + 1
        print("  DRIFT %-22s %-30s %s" % (d["class"], d["key"], d["detail"]))
        print("        -> %s" % d["recommend"])
    summary = " ".join("%s=%d" % (k, v) for k, v in sorted(by_class.items()))
    alarm("DRIFT %s (%d total) — exchange wins, never resend"
          % (summary, len(drifts)))
    print("RECONCILE DRIFT: %d mismatch(es) [%s] — alarmed; exchange wins, "
          "never resend" % (len(drifts), summary))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
