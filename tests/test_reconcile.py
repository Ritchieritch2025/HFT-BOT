"""W-K5 acceptance (PLAN_RISK_KILLSWITCH §3, contract #8): the reconcile loop.

Each seeded drift class is detected and classified; a zero-drift fixture
reports CLEAN with comparison COUNTS (a green must never lie — D2); the
exchange-wins policy is asserted (no recommendation ever says resend/retry);
a malformed / unreadable side fails CLOSED (never CLEAN). Offline only — mock
snapshots, no network.
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import reconcile as rec  # noqa: E402

RECONCILE = os.path.join(ROOT, "tools", "reconcile.py")


def _order(cid, tk="KXBTC-25DEC31-B50", side="bid", rem=50000, oid=None,
           price=None):
    o = {"client_order_id": cid, "ticker": tk, "book_side": side,
         "remaining_count_fp_e4": rem}
    if oid is not None:
        o["order_id"] = oid
    if price is not None:
        o["yes_price_e6"] = price
    return o


def _pos(tk, e6):
    return {"ticker": tk, "position_fp_e4": e6}


# ── clean: zero drift reports CLEAN *with counts* (D2) ──────────────────

def test_clean_reports_with_counts():
    state = {"resting_orders": [_order("o1"), _order("o2")],
             "positions": [_pos("KXBTC-25DEC31-B50", -50000)]}
    drifts, counts = rec.reconcile(state, dict(state))  # identical snapshots
    assert drifts == []
    assert counts["orders_compared"] == 2
    assert counts["positions_compared"] == 1


# ── the four seeded drift classes ───────────────────────────────────────

def test_order_only_at_exchange():
    ex = {"resting_orders": [_order("orphan")], "positions": []}
    en = {"resting_orders": [], "positions": []}
    drifts, _ = rec.reconcile(ex, en)
    assert len(drifts) == 1
    assert drifts[0]["class"] == "ORDER_ONLY_AT_EXCHANGE"
    assert "never assume it is gone" in drifts[0]["recommend"]


def test_order_only_in_engine():
    ex = {"resting_orders": [], "positions": []}
    en = {"resting_orders": [_order("phantom")], "positions": []}
    drifts, _ = rec.reconcile(ex, en)
    assert drifts[0]["class"] == "ORDER_ONLY_IN_ENGINE"
    assert "do not resend" in drifts[0]["recommend"]


def test_order_attr_drift_missed_partial_fill():
    # engine thinks 5 remaining; exchange shows 2 (a partial fill it missed)
    ex = {"resting_orders": [_order("o1", rem=20000)], "positions": []}
    en = {"resting_orders": [_order("o1", rem=50000)], "positions": []}
    drifts, _ = rec.reconcile(ex, en)
    assert drifts[0]["class"] == "ORDER_ATTR_DRIFT"
    assert "rem 20000!=50000" in drifts[0]["detail"]


def test_position_drift_missed_fill():
    ex = {"resting_orders": [], "positions": [_pos("KXBTC-25DEC31-B50", -30000)]}
    en = {"resting_orders": [], "positions": [_pos("KXBTC-25DEC31-B50", -50000)]}
    drifts, _ = rec.reconcile(ex, en)
    assert drifts[0]["class"] == "POSITION_DRIFT"
    assert "delta=20000" in drifts[0]["detail"]


def test_position_missing_on_one_side_is_drift():
    # engine holds a position the exchange doesn't (or vice versa) -> drift
    ex = {"resting_orders": [], "positions": []}
    en = {"resting_orders": [], "positions": [_pos("KXNHL-X", 10000)]}
    drifts, _ = rec.reconcile(ex, en)
    assert drifts[0]["class"] == "POSITION_DRIFT"
    assert "exchange position_fp_e4=0" in drifts[0]["detail"]


def test_disconnect_window_multiple_drifts():
    """A disconnect window: the engine missed a cancel (phantom), a fill
    (position), and a partial (attr) all at once — all three classified."""
    ex = {"resting_orders": [_order("o1", rem=10000)],
          "positions": [_pos("KXBTC-25DEC31-B50", -60000)]}
    en = {"resting_orders": [_order("o1", rem=50000), _order("gone")],
          "positions": [_pos("KXBTC-25DEC31-B50", -50000)]}
    drifts, counts = rec.reconcile(ex, en)
    classes = sorted(d["class"] for d in drifts)
    assert classes == ["ORDER_ATTR_DRIFT", "ORDER_ONLY_IN_ENGINE",
                       "POSITION_DRIFT"]
    assert counts["orders_compared"] == 2


# ── exchange-wins policy: no remedy ever recommends a resend (S2) ────────

_FORBIDDEN_VERBS = ("resend", "re-send", "retry", "re-try", "replace",
                    "reissue", "re-issue", "resubmit", "re-submit",
                    "retransmit", "re-transmit")


def test_no_remedy_ever_says_resend():
    for cls, remedy in rec.REMEDY.items():
        low = remedy.lower()
        for verb in _FORBIDDEN_VERBS:
            # allowed only inside an explicit negation ("do not resend")
            idx = low.find(verb)
            if idx >= 0:
                prefix = low[max(0, idx - 12):idx]
                assert "not " in prefix or "never " in prefix, \
                    "%s remedy has an un-negated %r: %s" % (cls, verb, remedy)
    # and the module source itself names the anti-pattern + tags the policy
    src = " ".join(open(RECONCILE).read().split())   # normalize line wraps
    assert "blind retry" in src        # the anti-pattern is named
    assert "exchange_wins_never_retry" in src  # the policy tag


# ── fail-closed: a malformed side is never CLEAN ────────────────────────

def test_malformed_side_fails_closed():
    good = {"resting_orders": [], "positions": []}
    bad = {"resting_orders": [{"ticker": "X"}], "positions": []}  # no client_order_id
    with pytest.raises(rec.ReconcileError):
        rec.reconcile(bad, good)
    with pytest.raises(rec.ReconcileError):
        rec.reconcile(good, {"positions": [{"position_fp_e4": 5}]})  # no ticker


# ── audit N1: ack-loss join is by client_order_id, not order_id ─────────

def test_ack_loss_joins_on_client_order_id():
    """A lost create-ack: engine knows only its client_order_id (no order_id
    yet); the exchange returns the same client_order_id + its order_id. This
    is ONE order, not a false only-at-exchange + only-in-engine pair."""
    ex = {"resting_orders": [_order("CLIENT-abc", oid="EXCH-REAL-123")],
          "positions": []}
    en = {"resting_orders": [_order("CLIENT-abc")],  # no order_id learned yet
          "positions": []}
    drifts, counts = rec.reconcile(ex, en)
    assert drifts == []                     # matched -> no phantom terminal
    assert counts["orders_compared"] == 1


# ── audit N2: float/bool counts are rejected, never coerced to a false CLEAN

def test_float_and_bool_counts_rejected():
    ex = {"resting_orders": [_order("c1", rem=20000)], "positions": []}
    en_float = {"resting_orders": [_order("c1", rem=20000.9)], "positions": []}
    with pytest.raises(rec.ReconcileError):
        rec.reconcile(ex, en_float)
    en_bool = {"resting_orders": [], "positions": [_pos("KXBTC-25DEC31-B50", True)]}
    with pytest.raises(rec.ReconcileError):
        rec.reconcile({"resting_orders": [], "positions": []}, en_bool)


# ── audit N3: price drift on the same order is detected ─────────────────

def test_price_drift_detected():
    ex = {"resting_orders": [_order("c1", price=400000)], "positions": []}
    en = {"resting_orders": [_order("c1", price=990000)], "positions": []}
    drifts, _ = rec.reconcile(ex, en)
    assert drifts[0]["class"] == "ORDER_ATTR_DRIFT"
    assert "price 400000!=990000" in drifts[0]["detail"]


# ── audit N6: a LARGE position delta escalates to a hard panic recommend ─

def test_large_position_delta_escalates_to_panic():
    ex = {"resting_orders": [], "positions": [_pos("KXBTC-25DEC31-B50", 0)]}
    en = {"resting_orders": [], "positions": [_pos("KXBTC-25DEC31-B50", -2000000)]}
    drifts, _ = rec.reconcile(ex, en)     # delta 2e6 e4 = 200 contracts >= 100
    assert drifts[0]["class"] == "POSITION_DRIFT_LARGE"
    assert "RUN PANIC" in drifts[0]["recommend"]
    # a small delta stays the soft class
    en_small = {"resting_orders": [], "positions": [_pos("KXBTC-25DEC31-B50", -10000)]}
    d2, _ = rec.reconcile(ex, en_small)
    assert d2[0]["class"] == "POSITION_DRIFT"


# ── opposite-sign same-magnitude position is a drift (signed, not abs) ───

def test_opposite_sign_position_is_drift():
    ex = {"resting_orders": [], "positions": [_pos("X", 50000)]}
    en = {"resting_orders": [], "positions": [_pos("X", -50000)]}
    drifts, _ = rec.reconcile(ex, en)
    assert len(drifts) == 1 and "delta=100000" in drifts[0]["detail"]


# ── CLI end-to-end: clean exit 0, drift exit 1, report written ──────────

def test_cli_clean_and_drift(tmp_path):
    en = tmp_path / "engine.json"
    ex_clean = tmp_path / "ex_clean.json"
    ex_drift = tmp_path / "ex_drift.json"
    report = tmp_path / "report.json"
    state = {"resting_orders": [_order("o1")], "positions": []}
    en.write_text(json.dumps(state))
    ex_clean.write_text(json.dumps(state))
    ex_drift.write_text(json.dumps({"resting_orders": [], "positions": []}))

    p = subprocess.run([sys.executable, RECONCILE, "--engine", str(en),
                        "--exchange", str(ex_clean)], capture_output=True, text=True)
    assert p.returncode == 0
    assert "RECONCILE CLEAN" in p.stdout
    assert "compared 1 order" in p.stdout          # counts always printed (D2)

    p2 = subprocess.run([sys.executable, RECONCILE, "--engine", str(en),
                         "--exchange", str(ex_drift), "--report", str(report)],
                        capture_output=True, text=True)
    assert p2.returncode == 1
    assert "RECONCILE DRIFT" in p2.stdout
    assert "never resend" in p2.stdout
    rep = json.loads(report.read_text())
    assert rep["clean"] is False
    assert rep["policy"] == "exchange_wins_never_retry"
    assert rep["drifts"][0]["class"] == "ORDER_ONLY_IN_ENGINE"


def test_cli_unreadable_side_exits_2(tmp_path):
    en = tmp_path / "engine.json"
    en.write_text(json.dumps({"resting_orders": [], "positions": []}))
    p = subprocess.run([sys.executable, RECONCILE, "--engine", str(en),
                        "--exchange", str(tmp_path / "nope.json")],
                       capture_output=True, text=True)
    assert p.returncode == 2
    assert "FAIL (closed)" in p.stderr
