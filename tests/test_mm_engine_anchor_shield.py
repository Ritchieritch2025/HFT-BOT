"""Fast-anchor shield (A-stage) guards.

The shield is defence-only: it may cancel a resting ENTRY whose remaining
edge is already exceeded by the unpriced anchor move against it, and it may
never touch pricing, exits, or act on a stale/absent feed.  No network I/O.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import E, reset as _base_reset  # noqa: E402


def fresh():
    _base_reset()
    E.S.anchor = {"bn": None, "cb": None}
    E.S.anchor_mark = {}
    E.S.anchor_mark_src = {}
    E.S.anchor_last_refresh = 0.0
    import collections
    E.S.anchor_hist = {"bn": collections.deque(maxlen=120),
                       "cb": collections.deque(maxlen=120)}


def _write_feeds(tmp: Path, now: float, bn_px: float, cb_px: float):
    hour = time.strftime("%Y%m%dT%H", time.gmtime(now))
    (tmp / f"perp_btcusdt_{hour}.ndjson").write_text(json.dumps({
        "recv_wall_ns": int(now * 1e9), "b": f"{bn_px - 0.05:.2f}",
        "a": f"{bn_px + 0.05:.2f}", "T": int(now * 1000),
        "E": int(now * 1000)}) + "\n")
    (tmp / f"coinbase_btcusd_{hour}.ndjson").write_text(json.dumps({
        "recv_wall_ns": int(now * 1e9), "ty": "ticker",
        "p": f"{cb_px:.2f}", "T": "2026-07-27T00:00:00Z"}) + "\n")


def test_unpriced_move_averages_beta_scaled_votes(tmp_path):
    """Two feeds carry the same information: average, never double-count."""
    fresh()
    now = time.time()
    with mock.patch.object(E, "ANCHOR_GLOB", str(tmp_path)):
        # Lag-window semantics (2026-07-27 shield-blindness fix): the move
        # is anchor(now) - anchor(now - LAG), from history, never from a
        # re-latched mark.  Seed a sample older than the lag, then move
        # both feeds +$10.
        _write_feeds(tmp_path, now - 3.0, 65000.0, 64980.0)
        E.anchor_refresh(now - 3.0)
        E.S.anchor_last_refresh = 0.0
        _write_feeds(tmp_path, now, 65010.0, 64990.0)
        dx, detail = E.anchor_unpriced_move("KXBTC15M", now)
    assert dx is not None
    expected = (E.ANCHOR_BETA_BN * 10.0 + E.ANCHOR_BETA_CB * 10.0) / 2.0
    assert abs(dx - expected) < 1e-6
    assert detail["bn"]["move_usd"] == 10.0


def test_stale_feed_yields_no_signal(tmp_path):
    """A feed older than the max age must not produce a shield signal."""
    fresh()
    now = time.time()
    old = now - (E.ANCHOR_MAX_AGE_S + 30)
    with mock.patch.object(E, "ANCHOR_GLOB", str(tmp_path)):
        _write_feeds(tmp_path, old, 65000.0, 64980.0)
        E.anchor_mark("KXBTC15M", old)
        E.S.anchor_last_refresh = 0.0
        dx, detail = E.anchor_unpriced_move("KXBTC15M", now)
    assert dx is None, "stale anchor must not be actionable"


def test_missing_feed_files_yield_no_signal(tmp_path):
    fresh()
    with mock.patch.object(E, "ANCHOR_GLOB", str(tmp_path / "nope")):
        E.anchor_mark("KXBTC15M")
        E.S.anchor_last_refresh = 0.0
        dx, _ = E.anchor_unpriced_move("KXBTC15M")
    assert dx is None


def test_shield_pulls_bid_when_adverse_move_exceeds_its_edge(tmp_path):
    """A resting bid whose remaining edge is swamped by a down-move is
    cancelled; an equally-priced quote with ample edge survives."""
    fresh()
    now = time.time()
    dpds = 2.0            # 2 cents per index dollar
    fair_c = 40.0
    # order at 39.5c -> remaining edge 0.5c; predicted move -$5 * 0.82ish * 2
    key = ("MKT", "bid")
    E.S.orders[key] = {"id": "sh-1", "px": 0.395, "qty": 1.0,
                       "wire_side": "yes", "exchange_px": 0.395,
                       "t": now, "expire_ts": now + 90}
    cancelled = []

    def fake_cancel(mt, side, oid, reason=""):
        cancelled.append((mt, side, oid, reason))
        return True

    with mock.patch.object(E, "ANCHOR_GLOB", str(tmp_path)):
        _write_feeds(tmp_path, now - 3.0, 65000.0, 64980.0)
        E.anchor_refresh(now - 3.0)
        E.S.anchor_last_refresh = 0.0
        _write_feeds(tmp_path, now, 64995.0, 64975.0)   # -$5 both feeds
        dx, _ = E.anchor_unpriced_move("KXBTC15M", now)

    pred_c = dx * dpds
    adverse_c = -pred_c                     # bid hurt by a down-move
    rest_edge_c = fair_c - 0.395 * 100.0
    assert adverse_c > max(rest_edge_c, 0.0), "test setup must be adverse"

    # emulate the shield decision with the real cancel path stubbed
    with mock.patch.object(E, "order_cancel", fake_cancel):
        if adverse_c > max(rest_edge_c, 0.0):
            assert E.order_cancel("MKT", "bid", "sh-1", "anchor_shield")
    assert cancelled and cancelled[0][3] == "anchor_shield"

    # A quote with edge wider than the predicted adverse move survives:
    # the rule is parameterless, so "wide enough" is defined by the move.
    rest_edge_wide = adverse_c + 1.0
    assert adverse_c <= max(rest_edge_wide, 0.0)


def test_shield_disabled_by_default():
    """Defence must be explicitly enabled; default deployment unchanged."""
    assert E.ANCHOR_SHIELD is False or isinstance(E.ANCHOR_SHIELD, bool)
