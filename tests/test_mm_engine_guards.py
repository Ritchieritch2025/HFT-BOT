"""Hard-assertion tests for mm_engine (A1-A6) + pricing correctness.

These are the gates that must be green before any further live fire.
The engine module is imported with shadow mode and dummy credentials.
"""
from __future__ import annotations

import importlib
import json
import os
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
ENG_DIR = REPO / "tools" / "research" / "crypto_mm"
sys.path.insert(0, str(ENG_DIR))

# ---- stub out network deps so the module imports offline -------------
if "websockets" not in sys.modules:
    sys.modules["websockets"] = types.SimpleNamespace(connect=None)

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

_TMP_KEY = Path("/tmp/_mm_test_key.pem")
if not _TMP_KEY.exists():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _TMP_KEY.write_bytes(k.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()))

os.environ.setdefault("KALSHI_API_KEY_ID", "test-key")
os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(_TMP_KEY)
os.environ["MM_MODE"] = "shadow"
os.environ["MM_MAX_COST"] = "8"
os.environ["MM_CLIP"] = "2.00"
os.environ["MM_OUT"] = "/tmp/_mm_test_out"
os.environ["MM_CTRL"] = "/tmp/_mm_test_control.json"
os.environ["MM_STATUS"] = "/tmp/_mm_test_control_status.json"
os.environ["MM_HARD_MAX_COST"] = "200"

import mm_engine as E  # noqa: E402
import mm_control as mc  # noqa: E402
import rti_pricing as rp  # noqa: E402


def reset():
    E.S.orders.clear(); E.S.latch.clear(); E.S.net_pos.clear()
    # speed-directive state (2026-07-27): brakes and fill clocks must not
    # leak between tests -- one test's fills would brake the next one's side
    for attr in ("side_brake", "side_fill_times", "recent_fill_mono",
                 "order_tombstones", "want_unmet", "exit_intent",
                 "last_pos_action", "last_pos_log", "recon_suspect",
                 "unknown_orders", "pricing_unready"):
        if hasattr(E.S, attr):
            getattr(E.S, attr).clear()
    if hasattr(E.S, "last_budget_trip_err"):
        E.S.last_budget_trip_err = ""
        E.S.last_budget_trip_log = 0.0
    E.S.open_cost = 0.0; E.S.halted = False; E.S.tokens = 8.0
    E.S.tok_t = 0.0; E.S.limit_breached = False
    E.S.control_error = ""; E.S.last_control_cancel = 0.0
    E.S.last_replace.clear(); E.S.books.clear(); E.S.meta.clear()
    for series in E.SERIES:
        E.S.rti[series].clear()
        if hasattr(E.S, "rti_src_ms"):
            E.S.rti_src_ms[series].clear()
        E.S.rti_t[series] = 0.0
        if hasattr(E.S, "rti_lock"):
            E.S.rti_lock[series] = None
        if hasattr(E.S, "cf_session_ticks"):
            E.S.cf_session_ticks[series].clear()
    if hasattr(E.S, "cf_stream_ok"):
        E.S.cf_stream_ok = False
        E.S.cf_sid = None
        E.S.cf_seq = None
    if hasattr(E.S, "public_trades"):
        E.S.public_trades.clear()
        E.S.public_trade_ids.clear()
        E.S.public_trade_id_fifo.clear()
    E.S.recon_fails = 0
    E.S.fills_selfcheck_ok = False
    E.S.contract_selfcheck_ok = False
    E.S.last_recon_ok_mono = None
    if hasattr(E.S, "last_eval"):
        E.S.last_eval.clear()
    if hasattr(E.S, "last_entry_source_ms"):
        E.S.last_entry_source_ms.clear()
    if hasattr(E.S, "last_sentinel_log"):
        E.S.last_sentinel_log.clear()
    if hasattr(E.S, "requote_suppressed"):
        E.S.requote_suppressed = 0
    if hasattr(E.S, "fills_seen"):
        E.S.fills_seen.clear()
    E.S.realized = 0.0
    if hasattr(E.S, "settle_zero_streak"):
        E.S.settle_zero_streak = 0
    if hasattr(E.S, "settled_seen"):
        E.S.settled_seen.clear()
    if hasattr(E.S, "unpaired"):
        E.S.unpaired.clear()
    if hasattr(E.S, "flatten_sent"):
        E.S.flatten_sent.clear()
    if hasattr(E.S, "flatten_pending"):
        E.S.flatten_pending.clear()
    if hasattr(E.S, "unknown_orders"):
        E.S.unknown_orders.clear()
    if hasattr(E.S, "pending_new"):
        E.S.pending_new.clear()
    if hasattr(E.S, "filled_by_order"):
        E.S.filled_by_order.clear()
    if hasattr(E.S, "place_not_before"):
        E.S.place_not_before.clear()
    if hasattr(E.S, "pair_locked_by_market"):
        E.S.pair_locked_by_market.clear()
        E.S.pair_locked_total = 0.0
    if hasattr(E.S, "cycle_active"):
        E.S.cycle_active.clear()
        E.S.completed_cycles = 0
        E.S.canary_done = False
    E.MAX_OPEN_COST = 8.0; E.CLIP = "2.00"; E.MAX_NET = 6
    E._control_loaded = True
    E.CTRL.clear()
    E.CTRL.update(mc.validate_control(
        mc.default_control(
            max_open_cost=8.0, clip="2.00", max_net=6, paused=False
        ),
        hard_max_cost=E.HARD_MAX_OPEN_COST,
        hard_max_clip=E.HARD_MAX_CLIP,
        hard_max_net=E.HARD_MAX_NET,
    ))
    E._ctrl_t = 0.0


def seed_timed_rti(series, ticks, now=None, spacing_ms=1000):
    """Seed the same strict source-time state used by live pricing."""
    now = E.time.time() if now is None else float(now)
    end_ms = int(now * 1000)
    start_ms = end_ms - (len(ticks) - 1) * int(spacing_ms)
    E.S.rti[series].clear()
    E.S.rti[series].extend(ticks)
    E.S.rti_src_ms[series].clear()
    E.S.rti_src_ms[series].extend(
        start_ms + i * int(spacing_ms) for i in range(len(ticks))
    )
    E.S.rti_t[series] = now
    E.S.rti_lock[series] = None
    E.S.cf_stream_ok = True


def advance_timed_rti(series, value=None, seconds=1):
    """Advance one authoritative source tick for entry-decision tests."""
    value = E.S.rti[series][-1] if value is None else float(value)
    E.S.rti[series].append(value)
    E.S.rti_src_ms[series].append(
        E.S.rti_src_ms[series][-1] + int(seconds * 1000)
    )
    E.S.rti_t[series] = E.time.time()
    E.S.cf_stream_ok = True


class A1_ImmediateAccounting(unittest.TestCase):
    def test_exposure_counts_resting_orders_without_any_poll(self):
        reset()
        self.assertEqual(E.exposure(), 0.0)
        E.S.orders[("M1", "bid")] = {"id": "x", "px": 0.32, "t": 0}
        # 0.32 * 2 contracts = 0.64 immediately, no fill poll involved
        self.assertAlmostEqual(E.exposure(), 0.64, places=6)
        E.S.orders[("M1", "ask_no")] = {"id": "y", "px": 0.66, "t": 0}
        self.assertAlmostEqual(E.exposure(), 0.64 + 1.32, places=6)

    def test_exposure_includes_fills(self):
        reset()
        E.S.net_pos["M1"] = {"y": 5, "n": 0, "cost": 1.60}
        self.assertAlmostEqual(E.exposure(), 1.60, places=6)

    def test_old_order_keeps_original_qty_after_clip_change(self):
        reset()
        E.S.orders[("M1", "bid")] = {
            "id": "x", "px": 0.40, "qty": 5.0, "t": 0
        }
        E.CLIP = "1.00"
        self.assertAlmostEqual(E.exposure(), 2.0, places=6)


class A4_RateLimiter(unittest.TestCase):
    def test_burst_capped_then_refills(self):
        reset()
        E.S.tokens = 8.0
        E.S.tok_t = E.time.time()
        granted = sum(1 for _ in range(50) if E.take_token())
        self.assertLessEqual(granted, 9, "burst must be capped ~8")
        self.assertGreaterEqual(granted, 8)

    def test_refill_rate(self):
        reset()
        E.S.tokens = 0.0
        E.S.tok_t = E.time.time() - 1.0      # one second elapsed
        self.assertTrue(E.take_token())      # ~4 tokens refilled


class A6_ExposureCap(unittest.TestCase):
    def test_cap_value_from_env(self):
        self.assertEqual(E.MAX_OPEN_COST, 8.0)

    def test_exposure_above_cap_is_detected(self):
        reset()
        for i in range(20):
            E.S.orders[(f"M{i}", "bid")] = {"id": str(i), "px": 0.50, "t": 0}
        self.assertGreater(E.exposure(), E.MAX_OPEN_COST)

    def test_candidate_may_not_cross_cap(self):
        reset()
        E.S.orders[("M1", "bid")] = {
            "id": "x", "px": 0.50, "qty": 15.0, "t": 0
        }
        self.assertAlmostEqual(E.exposure(), 7.5)
        self.assertFalse(E.within_cap(0.50, 2.0))
        self.assertTrue(E.within_cap(0.25, 2.0))

    def test_no_order_reserves_no_cost_not_yes_wire_price(self):
        reset()
        E.MAX_OPEN_COST = 1.0
        with mock.patch.object(E, "load_control", return_value=False):
            placed = E.order_place("M1", "ask", 0.90, risk_px=0.10)
        self.assertIsNotNone(placed)  # 2 NO contracts cost $0.20, not $1.80

    def test_no_order_rejects_rich_no_even_if_yes_wire_price_is_low(self):
        reset()
        E.MAX_OPEN_COST = 1.0
        with mock.patch.object(E, "load_control", return_value=False):
            placed = E.order_place("M1", "ask", 0.10, risk_px=0.90)
        self.assertIsNone(placed)  # 2 NO contracts would cost $1.80


class HotControl(unittest.TestCase):
    def test_first_external_revision_zero_replaces_memory_defaults(self):
        reset()
        E._control_loaded = False
        first = mc.validate_control(
            mc.default_control(
                max_open_cost=0.0,
                clip="1.00",
                max_net=0,
                paused=True,
            ),
            hard_max_cost=E.HARD_MAX_OPEN_COST,
            hard_max_clip=E.HARD_MAX_CLIP,
            hard_max_net=E.HARD_MAX_NET,
        )
        self.assertEqual(first["revision"], E.CTRL["revision"])
        self.assertTrue(E.apply_control_document(first))
        self.assertTrue(E._control_loaded)
        self.assertTrue(E.CTRL["paused"])
        self.assertEqual(E.MAX_OPEN_COST, 0.0)
        self.assertEqual(E.CLIP, "1.00")

    def test_live_missing_control_file_halts_fail_closed(self):
        reset()
        with (
            mock.patch.object(E, "MODE", "live"),
            mock.patch.object(E, "read_control", side_effect=FileNotFoundError),
        ):
            self.assertFalse(E.load_control(force=True))
        self.assertTrue(E.CTRL["paused"])
        self.assertTrue(E.S.halted)
        self.assertIn("missing", E.S.control_error)

    def test_live_unpause_rejected_until_pipelines_proven(self):
        # The interlock is EVIDENCE-based now (F2/F3 exist): live may
        # only be unpaused after the fills self-check has passed AND
        # reconciliation has succeeded at least once THIS process.
        reset()
        E.S.fills_selfcheck_ok = False
        E.S.last_recon_ok_mono = None
        document = mc.next_control(
            E.CTRL,
            {"paused": False, "note": "must not auto-arm live"},
            hard_max_cost=E.HARD_MAX_OPEN_COST,
            hard_max_clip=E.HARD_MAX_CLIP,
            hard_max_net=E.HARD_MAX_NET,
        )
        with (
            mock.patch.object(E, "MODE", "live"),
            self.assertRaisesRegex(mc.ControlError, "live resume disabled"),
        ):
            E.apply_control_document(document)
        # selfcheck alone is not enough
        E.S.fills_selfcheck_ok = True
        with (
            mock.patch.object(E, "MODE", "live"),
            self.assertRaisesRegex(mc.ControlError, "live resume disabled"),
        ):
            E.apply_control_document(document)

    def test_live_unpause_accepted_once_selfcheck_and_recon_green(self):
        reset()
        E.S.fills_selfcheck_ok = True
        E.S.contract_selfcheck_ok = True
        E.S.last_recon_ok_mono = E.time.monotonic()
        document = mc.next_control(
            E.CTRL,
            {"paused": False, "note": "operator ignition"},
            hard_max_cost=E.HARD_MAX_OPEN_COST,
            hard_max_clip=E.HARD_MAX_CLIP,
            hard_max_net=E.HARD_MAX_NET,
        )
        with mock.patch.object(E, "MODE", "live"):
            self.assertTrue(E.apply_control_document(document))
        self.assertFalse(E.CTRL["paused"])

    def test_live_unpause_rejected_when_recon_success_is_stale(self):
        reset()
        E.S.fills_selfcheck_ok = True
        E.S.last_recon_ok_mono = E.time.monotonic() - 120.0   # 2min old
        document = mc.next_control(
            E.CTRL,
            {"paused": False, "note": "stale recon"},
            hard_max_cost=E.HARD_MAX_OPEN_COST,
            hard_max_clip=E.HARD_MAX_CLIP,
            hard_max_net=E.HARD_MAX_NET,
        )
        with (
            mock.patch.object(E, "MODE", "live"),
            self.assertRaisesRegex(mc.ControlError, "live resume disabled"),
        ):
            E.apply_control_document(document)

    def test_pause_applies_limits_and_drains_shadow_orders(self):
        reset()
        E.S.orders[("M1", "bid")] = {
            "id": "shadow-x", "px": 0.32, "qty": 2.0, "t": 0
        }
        doc = mc.next_control(
            E.CTRL,
            {
                "max_open_cost": 4.0,
                "clip": "1.00",
                "max_net": 3,
                "paused": True,
                "note": "operator pause",
            },
            hard_max_cost=E.HARD_MAX_OPEN_COST,
            hard_max_clip=E.HARD_MAX_CLIP,
            hard_max_net=E.HARD_MAX_NET,
        )
        self.assertTrue(E.apply_control_document(doc))
        self.assertEqual(E.MAX_OPEN_COST, 4.0)
        self.assertEqual(E.CLIP, "1.00")
        self.assertEqual(E.MAX_NET, 3)
        self.assertTrue(E.CTRL["paused"])
        self.assertFalse(E.S.orders)
        self.assertFalse(E.S.halted)

    def test_reduced_cap_drains_orders_without_permanent_halt(self):
        reset()
        E.S.orders[("M1", "bid")] = {
            "id": "shadow-x", "px": 0.40, "qty": 2.0, "t": 0
        }
        doc = mc.next_control(
            E.CTRL,
            {"max_open_cost": 0.50, "note": "reduce"},
            hard_max_cost=E.HARD_MAX_OPEN_COST,
            hard_max_clip=E.HARD_MAX_CLIP,
            hard_max_net=E.HARD_MAX_NET,
        )
        E.apply_control_document(doc)
        self.assertFalse(E.S.orders)
        self.assertFalse(E.S.halted)

    def test_kill_latches_engine(self):
        reset()
        doc = mc.next_control(
            E.CTRL,
            {"paused": True, "kill": True, "note": "emergency"},
            hard_max_cost=E.HARD_MAX_OPEN_COST,
            hard_max_clip=E.HARD_MAX_CLIP,
            hard_max_net=E.HARD_MAX_NET,
        )
        E.apply_control_document(doc)
        self.assertTrue(E.S.halted)
        self.assertTrue(E.CTRL["kill"])

    def test_control_cannot_exceed_startup_hard_cap(self):
        reset()
        with self.assertRaises(mc.ControlError):
            mc.next_control(
                E.CTRL,
                {"max_open_cost": E.HARD_MAX_OPEN_COST + 0.01},
                hard_max_cost=E.HARD_MAX_OPEN_COST,
                hard_max_clip=E.HARD_MAX_CLIP,
                hard_max_net=E.HARD_MAX_NET,
            )


class ZoneMap(unittest.TestCase):
    def test_graveyard_closed(self):
        self.assertFalse(E.zone_ok(119, 50))
        self.assertFalse(E.zone_ok(10, 95))

    def test_early_window_all_strikes(self):
        self.assertTrue(E.zone_ok(700, 50))
        self.assertTrue(E.zone_ok(700, 95))

    def test_mid_window_only_off_center(self):
        self.assertFalse(E.zone_ok(400, 52))
        self.assertTrue(E.zone_ok(400, 70))

    def test_late_window_tails_only(self):
        self.assertFalse(E.zone_ok(200, 60))
        self.assertTrue(E.zone_ok(200, 95))
        self.assertTrue(E.zone_ok(200, 5))


class TailCheapOnly(unittest.TestCase):
    def test_disabled_preserves_both_sides(self):
        with mock.patch.object(E, "TAIL_CHEAP_ONLY", False):
            self.assertTrue(E.tail_cheap_entry_allowed(85, 79))
            self.assertTrue(E.tail_cheap_entry_allowed(85, 21))

    def test_tail_rejects_expensive_outcome_but_keeps_cheap_one(self):
        with mock.patch.object(E, "TAIL_CHEAP_ONLY", True):
            self.assertFalse(E.tail_cheap_entry_allowed(85, 79))
            self.assertTrue(E.tail_cheap_entry_allowed(85, 21))
            self.assertTrue(E.tail_cheap_entry_allowed(15, 15))
            self.assertFalse(E.tail_cheap_entry_allowed(15, 85))

    def test_mid_market_is_not_filtered(self):
        with mock.patch.object(E, "TAIL_CHEAP_ONLY", True):
            self.assertTrue(E.tail_cheap_entry_allowed(50, 79))


class TickBands(unittest.TestCase):
    def test_tail_band_improves_by_deci_cent(self):
        # bid 95.0c, ask 96.0c -> improve to 95.1c
        self.assertAlmostEqual(E.q_px(9500, 9600), 0.951, places=4)

    def test_mid_band_joins_without_improving(self):
        # bid 47c, ask 49c -> join at 47c (whole-cent book)
        self.assertAlmostEqual(E.q_px(4700, 4900), 0.47, places=4)

    def test_never_crosses_the_ask(self):
        # 1-tick-wide tail book: improvement must clamp below ask
        px = E.q_px(9900, 9910)
        self.assertLess(px, 0.991)


class F0_KernelSideSelection(unittest.TestCase):
    """F0 (2026-07-25): side selection must come from the pricing kernel.

    V6 proved the kernel's sign is right on 101 real fills (YES edge
    +4.27c -> pnl +30.09c; NO edge -3.87c -> pnl -25.81c) while the live
    engine bought 67 NO lots the kernel priced negative, because think()
    chose sides from market mid / wind fair and used the kernel only for
    the 15c sentinel.  These tests fail on that engine by construction.
    """

    SER = "KXBTC15M"

    def _arm_market(self, fair_target_c):
        """Static book: yes bid 45, yes ask 47 (mid 46); NO cost 53c.
        RTI ticks with sigma~3; strike solved so kernel fair ~= target."""
        reset()
        now = E.time.time()
        ticks = [
            65000.0 + (3.0 if i % 2 else -3.0) for i in range(301)
        ]
        seed_timed_rti(self.SER, ticks, now=now)
        tte = 700.0
        sigma = max(
            rp.sigma_from_timed_ticks(
                ticks, list(E.S.rti_src_ms[self.SER]), window_s=60),
            rp.sigma_from_timed_ticks(
                ticks, list(E.S.rti_src_ms[self.SER]), window_s=300),
        )
        self.assertIsNotNone(sigma)
        # invert the pre-lock variance for the strike hitting the target
        import math
        inside = (
            rp.rw_avg_var_factor(rp.LOCK_TICKS - 1)
            / (rp.LOCK_TICKS ** 2)
        )
        sd = sigma * math.sqrt((tte - rp.LOCK_TICKS) + inside)
        z = {55.0: 0.12566, 37.0: -0.33185}[fair_target_c]
        strike = ticks[-1] - z * sd
        p = rp.p_settle_above(ticks[-1], strike, sigma, tte)
        self.assertAlmostEqual(100.0 * p, fair_target_c, delta=0.2)
        E.S.meta["M1"] = (self.SER, now + tte, strike)
        E.S.books["M1"] = {"y": {4500: 10.0}, "n": {5300: 10.0}}
        return p

    def test_kernel_rich_no_side_is_never_quoted(self):
        # kernel fair 55c: YES at 45c is +10c cheap, NO at 53c is -8c rich
        self._arm_market(55.0)
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
        self.assertIn(("M1", "bid"), E.S.orders)
        self.assertNotIn(("M1", "ask_no"), E.S.orders)

    def test_kernel_rich_yes_side_is_never_quoted(self):
        # kernel fair 37c: YES at 45c is -8c rich, NO at 53c is +10c cheap
        self._arm_market(37.0)
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
        self.assertNotIn(("M1", "bid"), E.S.orders)
        self.assertIn(("M1", "ask_no"), E.S.orders)

    def test_unpriceable_market_is_not_quoted(self):
        # kernel cannot price (flat ticks -> sigma None -> p degenerate):
        # fail closed on BOTH sides rather than fall back to market mid.
        reset()
        now = E.time.time()
        E.S.rti[self.SER].clear()
        E.S.rti[self.SER].extend([65000.0] * 40)
        E.S.rti_t[self.SER] = now
        E.S.meta["M1"] = (self.SER, now + 700.0, 65000.0)
        E.S.books["M1"] = {"y": {4500: 10.0}, "n": {5300: 10.0}}
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
        self.assertNotIn(("M1", "bid"), E.S.orders)
        self.assertNotIn(("M1", "ask_no"), E.S.orders)


class H5_SourceTimedPricing(unittest.TestCase):
    """Live pricing must preserve the exchange's one-second time units."""

    SER = "KXBTC15M"

    @staticmethod
    def _msg(source_ms, value, lock=None, *, seq=1):
        return {
            "type": "cfbenchmarks_value",
            "sid": 1,
            "seq": seq,
            "msg": {
                "index_id": "BRTI",
                "received_at": source_ms + 100,
                "data": json.dumps({
                    "type": "value",
                    "id": "BRTI",
                    "time": source_ms,
                    "value": str(value),
                }),
                "avg_60s_data": {
                    "value": f"{float(value):.8f}",
                    "window_size": 0,
                    "window_start_ts_ms": source_ms - 60_000,
                    "window_end_ts_exclusive": source_ms,
                },
                **({
                    "last_60s_windowed_average_15min": lock
                } if lock is not None else {}),
            },
        }

    def test_phase_840_starts_official_window_without_shifted_field(self):
        reset()
        close_ms = 1_800_000
        source_ms = close_ms - 60_000
        self.assertTrue(E.ingest_cf_value(
            self._msg(source_ms, 65001.25),
            received_wall_s=source_ms / 1000.0,
        ))
        self.assertEqual(list(E.S.rti_src_ms[self.SER]), [source_ms])
        self.assertEqual(E.S.rti_lock[self.SER]["close_ms"], close_ms)
        self.assertEqual(E.S.rti_lock[self.SER]["n"], 1)
        self.assertAlmostEqual(
            E.S.rti_lock[self.SER]["sum"], 65001.25, places=6)

    def test_phase_841_uses_own_window_and_validates_shifted_field(self):
        reset()
        close_ms = 1_800_000
        first_ms = close_ms - 60_000
        self.assertTrue(E.ingest_cf_value(
            self._msg(first_ms, 65000.0, seq=1),
            received_wall_s=first_ms / 1000.0,
        ))
        source_ms = first_ms + 1000
        shifted = {
            # This exchange field deliberately excludes phase :14:00 and
            # includes the current :14:01 tick.  It is validated, but is not
            # the contract's [close-60s, close) settlement accumulator.
            "value": "65010.00000000",
            "window_size": 1,
            "window_start_ts_ms": first_ms,
            "window_end_ts_exclusive": source_ms,
        }
        frame = self._msg(source_ms, 65010.0, shifted, seq=2)
        frame["msg"]["avg_60s_data"] = {
            "value": "65000.00000000",
            "window_size": 1,
            "window_start_ts_ms": source_ms - 60_000,
            "window_end_ts_exclusive": source_ms,
        }
        self.assertTrue(E.ingest_cf_value(
            frame, received_wall_s=source_ms / 1000.0))
        official = E.S.rti_lock[self.SER]
        self.assertEqual(official["n"], 2)
        self.assertAlmostEqual(official["sum"], 130010.0, places=8)
        self.assertAlmostEqual(official["avg"], 65005.0, places=8)

    def test_duplicate_source_timestamp_is_not_a_second_observation(self):
        reset()
        msg = self._msg(1_800_001, 65000.0)
        self.assertTrue(E.ingest_cf_value(msg, received_wall_s=1800.001))
        self.assertFalse(E.ingest_cf_value(msg, received_wall_s=1800.002))
        self.assertEqual(len(E.S.rti[self.SER]), 1)
        self.assertEqual(len(E.S.rti_src_ms[self.SER]), 1)

    def test_missing_required_final_minute_field_rejects_atomically(self):
        reset()
        source_ms = 1_800_000 - 59_000  # phase :14:01, lock n=1 required
        self.assertFalse(E.ingest_cf_value(
            self._msg(source_ms, 65000.0),
            received_wall_s=source_ms / 1000.0,
        ))
        self.assertEqual(list(E.S.rti[self.SER]), [])
        self.assertEqual(list(E.S.rti_src_ms[self.SER]), [])
        self.assertFalse(E.S.cf_stream_ok)

    def test_reconnect_mid_final_minute_accepts_session_scoped_n1(self):
        reset()
        source_ms = 1_785_040_169_000
        frame = self._msg(
            source_ms,
            "64489.51",
            {
                "value": "64489.51000000",
                "window_size": 1,
                "window_start_ts_ms": 1_785_040_140_000,
                "window_end_ts_exclusive": source_ms,
            },
            seq=1,
        )
        # Both feed-maintained averages restart their observation count with
        # the subscription.  Their metadata keeps the contract window start,
        # so phase :14:29 can legitimately carry n=1 after a reconnect.
        self.assertTrue(E.ingest_cf_value(
            frame, received_wall_s=(source_ms + 100) / 1000.0))
        self.assertEqual(list(E.S.rti_src_ms[self.SER]), [source_ms])
        # Missing phase :14:00..:14:28 raw ticks means the official
        # accumulator still fails closed.
        self.assertIsNone(E.S.rti_lock[self.SER])

        next_ms = source_ms + 1000
        next_frame = self._msg(
            next_ms,
            "64490.51",
            {
                "value": "64490.01000000",
                "window_size": 2,
                "window_start_ts_ms": 1_785_040_140_000,
                "window_end_ts_exclusive": next_ms,
            },
            seq=2,
        )
        next_frame["msg"]["avg_60s_data"] = {
            "value": "64489.51000000",
            "window_size": 1,
            "window_start_ts_ms": next_ms - 60_000,
            "window_end_ts_exclusive": next_ms,
        }
        self.assertTrue(E.ingest_cf_value(
            next_frame, received_wall_s=(next_ms + 100) / 1000.0))
        self.assertIsNone(E.S.rti_lock[self.SER])

    def test_hydrated_raw_ticks_restore_official_lock_after_reconnect(self):
        reset()
        source_ms = 1_785_040_169_000
        official_start = 1_785_040_140_000
        prior = [
            Decimal("64460.00") + Decimal(i)
            for i in range(29)
        ]
        E.S.rti_src_ms[self.SER].extend(
            official_start + i * 1000 for i in range(29))
        E.S.rti[self.SER].extend(float(v) for v in prior)
        current = Decimal("64489.51")
        frame = self._msg(
            source_ms,
            str(current),
            {
                # New subscription: session-scoped shifted field restarts
                # at one even though raw recorder history is continuous.
                "value": f"{current:.8f}",
                "window_size": 1,
                "window_start_ts_ms": official_start,
                "window_end_ts_exclusive": source_ms,
            },
            seq=1,
        )
        self.assertTrue(E.ingest_cf_value(
            frame, received_wall_s=(source_ms + 100) / 1000.0))
        lock = E.S.rti_lock[self.SER]
        expected_sum = sum(prior, Decimal(0)) + current
        self.assertEqual(lock["n"], 30)
        self.assertAlmostEqual(lock["sum"], float(expected_sum), places=8)
        self.assertAlmostEqual(
            lock["avg"], float(expected_sum / 30), places=8)

    def test_close_tick_keeps_official_prior_60_not_shifted_last_60(self):
        reset()
        close_ms = 1_800_000
        # Index 0..59 are the official [close-60s, close) observations.
        # Index 60 is the close tick and must NOT enter this contract.
        values = [Decimal("65000.00") + Decimal(i) for i in range(61)]
        for i, value in enumerate(values):
            source_ms = close_ms - 60_000 + i * 1000
            prior = values[max(0, i - 60):i]
            avg60 = (
                sum(prior, Decimal(0)) / len(prior) if prior else value
            )
            shifted = None
            if i:
                shifted_values = values[1:i + 1]
                shifted_avg = (
                    sum(shifted_values, Decimal(0))
                    / len(shifted_values)
                )
                shifted = {
                    "value": f"{shifted_avg:.8f}",
                    "window_size": i,
                    "window_start_ts_ms": close_ms - 60_000,
                    "window_end_ts_exclusive": source_ms,
                }
            frame = self._msg(
                source_ms, f"{value:.2f}", shifted, seq=i + 1)
            frame["msg"]["avg_60s_data"] = {
                "value": f"{avg60:.8f}",
                "window_size": len(prior),
                "window_start_ts_ms": source_ms - 60_000,
                "window_end_ts_exclusive": source_ms,
            }
            self.assertTrue(E.ingest_cf_value(
                frame, received_wall_s=source_ms / 1000.0))

        final_lock = E.S.rti_lock[self.SER]
        authoritative = sum(values[:60], Decimal(0)) / 60
        shifted = sum(values[1:], Decimal(0)) / 60
        self.assertEqual(final_lock["n"], 60)
        self.assertAlmostEqual(final_lock["avg"], float(authoritative), places=8)
        self.assertNotAlmostEqual(
            final_lock["avg"], float(shifted), places=8)

    def test_pricing_requires_a_continuous_full_300_second_window(self):
        reset()
        now = E.time.time()
        short = [
            65000.0 + (3.0 if i % 2 else -3.0) for i in range(300)
        ]
        seed_timed_rti(self.SER, short, now=now)
        self.assertIsNone(E.pricing_state(self.SER, now + 700, now_s=now))

        full = short + [65003.0]
        seed_timed_rti(self.SER, full, now=now)
        self.assertIsNotNone(
            E.pricing_state(self.SER, now + 700, now_s=now))

        # One missing second anywhere in the long window invalidates entry
        # pricing until that gap rolls out of the 300-second history.
        end_ms = int(now * 1000)
        start_ms = end_ms - 301_000
        times = []
        current = start_ms
        for i in range(301):
            if i:
                current += 2000 if i == 150 else 1000
            times.append(current)
        E.S.rti[self.SER].clear()
        E.S.rti[self.SER].extend(full)
        E.S.rti_src_ms[self.SER].clear()
        E.S.rti_src_ms[self.SER].extend(times)
        E.S.rti_t[self.SER] = now
        self.assertEqual(times[-1], end_ms)
        self.assertIsNone(E.pricing_state(
            self.SER, now + 700, now_s=now))

    def test_multiscale_estimator_uses_the_more_conservative_sigma(self):
        reset()
        now = E.time.time()
        ticks = [65000.0]
        for i in range(1, 301):
            amp = 10.0 if i <= 240 else 1.0
            ticks.append(ticks[-1] + (amp if i % 2 else -amp))
        seed_timed_rti(self.SER, ticks, now=now)
        state = E.pricing_state(self.SER, now + 700, now_s=now)
        self.assertIsNotNone(state)
        self.assertGreater(state["sigma_long"], state["sigma_short"])
        self.assertAlmostEqual(
            state["sigma"], state["sigma_long"], places=9)

    def test_lock_state_is_used_only_for_its_exact_market_close(self):
        reset()
        now = E.time.time()
        ticks = [
            65000.0 + (3.0 if i % 2 else -3.0) for i in range(301)
        ]
        seed_timed_rti(self.SER, ticks, now=now)
        close_ms = int(round((now + 45.0) * 1000.0))
        E.S.rti_lock[self.SER] = {
            "sum": 65002.0 * 15,
            "n": 15,
            "close_ms": close_ms,
            "source_ms": int(now * 1000),
            "avg": 65002.0,
        }
        matching = E.pricing_state(
            self.SER, close_ms / 1000.0, now_s=now)
        other = E.pricing_state(
            self.SER, close_ms / 1000.0 + 900.0, now_s=now)
        self.assertEqual(matching["locked_n"], 15)
        self.assertEqual(other["locked_n"], 0)

    def test_restart_hydrates_rolling_history_without_five_minute_wait(self):
        reset()
        with tempfile.TemporaryDirectory() as td:
            capture = Path(td) / "cfb.ndjson"
            end_ms = 1_900_000
            lines = []
            for i in range(301):
                source_ms = end_ms - (300 - i) * 1000
                value = f"{65000 + (3 if i % 2 else -3):.2f}"
                frame = {
                    "type": "cfbenchmarks_value",
                    "sid": 7,
                    "seq": i + 1,
                    "msg": {
                        "index_id": "BRTI",
                        "received_at": source_ms + 80,
                        "data": json.dumps({
                            "type": "value", "id": "BRTI",
                            "time": source_ms, "value": value,
                        }),
                    },
                }
                lines.append(json.dumps({
                    "kind": "FRAME", "payload": json.dumps(frame) + "\n",
                }))
            capture.write_text("\n".join(lines) + "\n", encoding="utf-8")
            loaded = E.hydrate_rti_from_capture(
                str(Path(td) / "*.ndjson"),
                now_s=(end_ms + 1000) / 1000.0,
            )
            self.assertEqual(loaded, 301)
            self.assertFalse(E.S.cf_stream_ok)

            # The first current-session tick establishes stream integrity;
            # pricing is ready immediately from the recorder's rolling state.
            live = self._msg(
                end_ms + 1000, 65003.0, seq=1)
            self.assertTrue(E.ingest_cf_value(
                live, received_wall_s=(end_ms + 1100) / 1000.0))
            state = E.pricing_state(
                self.SER, (end_ms + 701_000) / 1000.0,
                now_s=(end_ms + 1100) / 1000.0,
            )
            self.assertIsNotNone(state)


class H6_PublicPairFlow(unittest.TestCase):
    """Public taker flow is the observable input to pair completion odds."""

    @staticmethod
    def _trade(trade_id, outcome, yes_px, qty, ts_ms):
        return {
            "trade_id": trade_id,
            "market_ticker": "M1",
            "yes_price_dollars": f"{yes_px:.4f}",
            "count_fp": f"{qty:.2f}",
            "taker_outcome_side": outcome,
            "taker_book_side": "bid" if outcome == "yes" else "ask",
            "ts_ms": ts_ms,
        }

    def test_flow_is_side_and_price_executable_and_deduped(self):
        reset()
        now_ms = 2_000_000
        # taker NO sells YES into our YES bid; taker YES sells NO into
        # our NO bid.  Only trades through the proposed levels count.
        yes_hit = self._trade("t-no", "no", 0.40, 6.0, now_ms - 5_000)
        no_hit = self._trade("t-yes", "yes", 0.60, 3.0, now_ms - 4_000)
        self.assertTrue(E.apply_public_trade(yes_hit))
        self.assertTrue(E.apply_public_trade(no_hit))
        self.assertIsNone(E.apply_public_trade(dict(no_hit)))
        E.S.books["M1"] = {
            "y": {4500: 4.0},
            "n": {4100: 2.0},
        }
        with mock.patch.object(E, "CLIP", "1.00"):
            got = E.pair_flow_stats(
                "M1", y_px=0.45, n_px=0.41, now_ms=now_ms)
        self.assertEqual(got["y_flow_10s"], 6.0)
        self.assertEqual(got["n_flow_10s"], 3.0)
        self.assertEqual(got["y_queue_ahead"], 4.0)
        self.assertEqual(got["n_queue_ahead"], 2.0)
        self.assertAlmostEqual(got["y_clear_eta_s"], 50.0, places=6)
        self.assertAlmostEqual(got["n_clear_eta_s"], 60.0, places=6)

    def test_conflicting_public_trade_direction_is_rejected(self):
        reset()
        bad = self._trade("bad", "yes", 0.60, 1.0, 2_000_000)
        bad["taker_book_side"] = "ask"
        self.assertFalse(E.apply_public_trade(bad))
        self.assertFalse(E.S.public_trades)


class F1_InventorySkew(unittest.TestCase):
    """F1: graduated quote retreat against inventory, before the hard
    MAX_NET latch.  gamma derivation: docs/research_reports/
    MM_SKEW_GAMMA_DERIVATION.md (capital-constraint bound, default
    0.5c/contract, replay-bracketed by grid_replay_v2 arms 0.2/0.5/1.0).
    """

    def setUp(self):
        # These cases specify the FIXED-cent skew contract (GAMMA_C per
        # contract).  The sigma-transduced skew is a different regime with
        # its own test below, so pin the regime explicitly instead of
        # letting the ambient default decide what the assertions mean.
        self._scale = mock.patch.object(E, "SIGMA_SCALE", False)
        self._scale.start()
        self.addCleanup(self._scale.stop)

    def _arm(self, fair_target_c, net_y=0, net_n=0):
        F0_KernelSideSelection._arm_market(
            F0_KernelSideSelection(methodName="run"), fair_target_c)
        if net_y or net_n:
            E.S.net_pos["M1"] = {"y": net_y, "n": net_n,
                                 "cost": 0.45 * net_y + 0.53 * net_n}

    def test_sigma_transduced_skew_scales_with_contract_volatility(self):
        """With transduction ON the retreat is GAMMA_K sigma-units, so a
        more volatile contract retreats further for the same inventory."""
        with mock.patch.object(E, "SIGMA_SCALE", True):
            calm = {"sigma": 1.0, "dp_ds_c_per_usd": 0.5}
            wild = {"sigma": 4.0, "dp_ds_c_per_usd": 0.5}
            g_calm = E.gamma_c_dyn(calm)
            g_wild = E.gamma_c_dyn(wild)
            self.assertGreater(g_wild, g_calm)
            self.assertAlmostEqual(g_wild / g_calm, 4.0, places=6)
            # and the entry threshold moves with the same scale
            self.assertGreater(E.margin_c(wild), E.margin_c(calm))
        # transduction off -> legacy constant, unaffected by sigma
        with mock.patch.object(E, "SIGMA_SCALE", False):
            self.assertEqual(E.gamma_c_dyn({"sigma": 9.0,
                                            "dp_ds_c_per_usd": 0.5}),
                             E.GAMMA_C)

    def test_long_yes_retreats_yes_bid_by_gamma_per_contract(self):
        # net +4, gamma 0.5c/ct -> retreat 2c: 45c baseline -> 43c
        self._arm(55.0, net_y=4)
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
        self.assertIn(("M1", "bid"), E.S.orders)
        self.assertAlmostEqual(E.S.orders[("M1", "bid")]["px"], 0.43,
                               places=4)

    def test_long_no_retreats_no_quote_symmetric(self):
        # net -4 (long NO), NO baseline 53c -> 51c
        self._arm(37.0, net_n=4)
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
        self.assertIn(("M1", "ask_no"), E.S.orders)
        self.assertAlmostEqual(E.S.orders[("M1", "ask_no")]["px"], 0.51,
                               places=4)

    def test_zero_inventory_quotes_baseline(self):
        self._arm(55.0)
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
        self.assertAlmostEqual(E.S.orders[("M1", "bid")]["px"], 0.45,
                               places=4)

    def test_offsetting_side_never_retreats(self):
        # long YES must NOT retreat the NO quote (it offsets inventory)
        self._arm(37.0, net_y=4)
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
        self.assertIn(("M1", "ask_no"), E.S.orders)
        self.assertAlmostEqual(E.S.orders[("M1", "ask_no")]["px"], 0.53,
                               places=4)

    def test_retreat_snaps_down_to_legal_mid_band_tick(self):
        # net +1 -> raw retreat 0.5c: 45c -> 44.5c, mid band is whole-cent
        # only, so the quote must floor to 44c (retreat further, never less)
        self._arm(55.0, net_y=1)
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
        self.assertAlmostEqual(E.S.orders[("M1", "bid")]["px"], 0.44,
                               places=4)

    def test_legal_floor_bands(self):
        self.assertAlmostEqual(E.legal_floor(0.445), 0.44, places=4)
        self.assertAlmostEqual(E.legal_floor(0.946), 0.946, places=4)
        self.assertAlmostEqual(E.legal_floor(0.0525), 0.052, places=4)


class RestSigning(unittest.TestCase):
    """Kalshi signs ts+method+PATH — query string EXCLUDED.  The engine
    signed the full path including '?min_ts=...' so every /portfolio/
    fills and /portfolio/positions call returned 401 (caught by the
    fills selfcheck at live ignition 2026-07-26; order placement never
    noticed because those paths carry no query).  D1 was therefore a
    DOUBLE defect: ms units AND signature scope.
    """

    def _captured_sig_path(self, method, path):
        captured = {}

        def fake_sig(m, p):
            captured["path"] = p
            return {}

        with (
            mock.patch.object(E, "_sig", side_effect=fake_sig),
            mock.patch.object(E.urllib.request, "urlopen",
                              side_effect=OSError("no network in tests")),
        ):
            code, _ = E.rest(method, path)
        self.assertEqual(code, -1)                 # no network happened
        return captured["path"]

    def test_rest_signs_path_without_query_string(self):
        self.assertEqual(
            self._captured_sig_path("GET", "/portfolio/fills?min_ts=5&limit=1"),
            "/portfolio/fills")

    def test_rest_signs_bare_path_unchanged(self):
        self.assertEqual(
            self._captured_sig_path("DELETE", "/portfolio/events/orders/abc123"),
            "/portfolio/events/orders/abc123")


class F2_PositionReconciliation(unittest.TestCase):
    """F2: every 5s the engine compares local net_pos against the
    EXCHANGE's /portfolio/positions truth.  >2 contracts divergence in
    any market -> HALT + cancel_all.  On 2026-07-25 the exchange held 58
    NO the engine had never seen (D1); the only backstop was the
    exchange's insufficient_balance.  Blindness (repeated fetch failure)
    also halts: a dead data source means stop, not keep quoting.
    """

    def setUp(self):
        reset()
        # The settlement-window exemption (race #5 sibling, 2026-07-27)
        # scopes recon to OPEN markets; these cases assert the OPEN-market
        # contract, so the fixture market must be live in S.meta.
        E.S.meta["MKT"] = ("KXBTC15M", E.time.time() + 600.0, 65000.0)

    def test_replay_of_incident_58_unseen_no_contracts_halts(self):
        reset()
        E.S.meta["MKT"] = ("KXBTC15M", E.time.time() + 600.0, 65000.0)
        E.S.orders[("MKT", "bid")] = {"id": "shadow-1", "px": 0.4,
                                      "qty": 2.0, "t": 0}
        resp = {"market_positions": [{"ticker": "MKT", "position": -58}]}
        with mock.patch.object(E, "rest", return_value=(200, resp)):
            ok = E.recon_check()          # strike 1: suspect only
            self.assertFalse(ok)
            self.assertFalse(E.S.halted)
            self.assertFalse(E.recon_check())   # strike 2: halt
        self.assertTrue(E.S.halted)
        self.assertFalse(E.S.orders)          # cancel_all drained

    def test_any_position_divergence_halts_on_second_strike(self):
        reset()
        E.S.meta["MKT"] = ("KXBTC15M", E.time.time() + 600.0, 65000.0)
        E.S.net_pos["MKT"] = {"y": 4, "n": 0, "cost": 1.8}
        resp = {"market_positions": [{"ticker": "MKT", "position": 5}]}
        with mock.patch.object(E, "rest", return_value=(200, resp)):
            self.assertFalse(E.recon_check())
            self.assertFalse(E.S.halted)
            self.assertFalse(E.recon_check())
        self.assertTrue(E.S.halted)

    def test_transient_divergence_clears_without_halt(self):
        # Race #16 mirror: exchange records our fill before the local
        # pipeline applies it.  One divergent pass, then agreement -- the
        # suspect must clear and the engine must keep running.
        reset()
        E.S.meta["MKT"] = ("KXBTC15M", E.time.time() + 600.0, 65000.0)
        E.S.net_pos["MKT"] = {"y": 0, "n": 2, "cost": 1.5}
        diverged = {"market_positions": [{"ticker": "MKT", "position": 0}]}
        agreed = {"market_positions": [{"ticker": "MKT", "position": -2}]}
        with mock.patch.object(E, "rest",
                               side_effect=[(200, diverged), (200, agreed),
                                            (200, diverged)]):
            self.assertFalse(E.recon_check())   # suspect
            self.assertTrue(E.recon_check())    # resolved: suspect cleared
            self.assertFalse(E.recon_check())   # NEW suspect, not strike 2
        self.assertFalse(E.S.halted)

    def test_local_position_missing_on_exchange_halts(self):
        # local says long 5, exchange says flat -> phantom inventory
        reset()
        E.S.meta["MKT"] = ("KXBTC15M", E.time.time() + 600.0, 65000.0)
        E.S.net_pos["MKT"] = {"y": 5, "n": 0, "cost": 2.0}
        resp = {"market_positions": []}
        with mock.patch.object(E, "rest", return_value=(200, resp)):
            self.assertFalse(E.recon_check())
            self.assertFalse(E.recon_check())
        self.assertTrue(E.S.halted)

    def test_three_consecutive_fetch_failures_halt_blind(self):
        reset()
        with mock.patch.object(E, "rest", return_value=(500, {"error": "x"})):
            E.recon_check()
            E.recon_check()
            self.assertFalse(E.S.halted)      # two strikes: still alive
            E.recon_check()
        self.assertTrue(E.S.halted)

    def test_fetch_success_resets_failure_counter(self):
        reset()
        good = (200, {"market_positions": []})
        bad = (500, {"error": "x"})
        with mock.patch.object(E, "rest", side_effect=[bad, bad, good, bad]):
            E.recon_check(); E.recon_check(); E.recon_check(); E.recon_check()
        self.assertFalse(E.S.halted)
        self.assertEqual(E.S.recon_fails, 1)

    def test_own_recent_fill_divergence_is_graced_not_halted(self):
        # Race #16 (2026-07-28T02:29): graveyard-flatten taker fill moved
        # local to 0 while the positions replica still showed -2.  Within
        # 10s of OUR OWN fill on that market the divergence is explained.
        reset()
        E.S.meta["MKT"] = ("KXBTC15M", E.time.time() + 600.0, 65000.0)
        E.S.recent_fill_mono["MKT"] = E.time.monotonic()
        resp = {"market_positions": [{"ticker": "MKT", "position": -2}]}
        with mock.patch.object(E, "rest", return_value=(200, resp)):
            self.assertTrue(E.recon_check())
        self.assertFalse(E.S.halted)

    def test_stale_fill_grace_expired_divergence_still_halts(self):
        reset()
        E.S.meta["MKT"] = ("KXBTC15M", E.time.time() + 600.0, 65000.0)
        E.S.recent_fill_mono["MKT"] = E.time.monotonic() - 11.0
        resp = {"market_positions": [{"ticker": "MKT", "position": -2}]}
        with mock.patch.object(E, "rest", return_value=(200, resp)):
            self.assertFalse(E.recon_check())
            self.assertFalse(E.recon_check())
        self.assertTrue(E.S.halted)

    def test_unknown_order_record_carries_reservation_economics(self):
        # 2026-07-28T02:48 latch: an unknown-order record with no economics
        # (qty/px/xpx all None) durably latched the budget guard once the
        # local order row was gone.  order_cancel must copy the economics
        # into the record it creates, and the adapter must accept it alone.
        reset()
        E.S.unknown_orders.clear()
        E.S.orders[("MKT", "bid")] = {
            "id": "oid-1", "px": 0.40, "qty": 2.0, "t": 0,
            "wire_side": "bid", "exchange_px": 0.40,
        }
        with mock.patch.object(E, "rest", return_value=(500, {})), \
                mock.patch.object(E, "MODE", "live"):
            E.order_cancel("MKT", "bid", "oid-1", reason="test")
        rec = dict(E.S.unknown_orders["oid-1"])
        E.S.unknown_orders.clear()
        self.assertEqual(rec["qty"], 2.0)
        self.assertEqual(rec["px"], 0.40)
        self.assertEqual(rec["wire_side"], "bid")
        self.assertEqual(rec["exchange_px"], 0.40)
        # Orphaned record (local row gone) must validate on its own.
        E.S.orders.clear()
        E._budget_validate_open_orders(
            [], orders={}, pending_new={},
            unknown_orders={"oid-1": rec})

    def test_repeated_blind_trip_is_deduped(self):
        # One latch = one trip: the 1 Hz monitor must not re-run the
        # cancel storm on an identical, already-latched error.
        reset()
        E.S.last_budget_trip_err = ""
        E.S.last_budget_trip_log = 0.0
        trip_calls = []
        guard = mock.Mock()
        guard.record.latched = True
        guard.trip_receipt = None
        guard.trip_blind = lambda **kw: trip_calls.append(kw)
        boom = E.mba.AdapterError("same failure")
        with mock.patch.object(E.S, "budget_guard", guard), \
                mock.patch.object(E, "capture_budget_snapshot",
                                  side_effect=boom):
            E.S.halted = False
            E._budget_snapshot_or_trip("MONITOR_SNAPSHOT")   # real trip
            E.S.halted = True
            E._budget_snapshot_or_trip("MONITOR_SNAPSHOT")   # deduped
            E._budget_snapshot_or_trip("MONITOR_SNAPSHOT")   # deduped
        self.assertEqual(len(trip_calls), 1)


class F3_FillPipelineAndReservation(unittest.TestCase):
    """F3 + D1: the fills cursor is SECONDS (the API's min_ts unit — the
    2026-07-25 root cause was milliseconds -> empty responses forever),
    fills transfer reservation from resting to filled without double
    counting, and a startup self-check proves the pipeline can actually
    see fills before live quoting starts.
    """

    def test_fills_cursor_is_seconds_not_milliseconds(self):
        # ms cursors are ~1.7e12; seconds are ~1.7e9.  D1 regression pin.
        self.assertLessEqual(E.S.fills_cursor, E.time.time() + 2)

    def test_fill_created_s_normalizes_units(self):
        self.assertEqual(E.fill_created_s({"created_ts": 1_770_000_000}),
                         1_770_000_000)
        self.assertEqual(E.fill_created_s({"created_ts": 1_770_000_000_000}),
                         1_770_000_000)
        self.assertAlmostEqual(
            E.fill_created_s({"created_ts": 1_770_000_000_958}),
            1_770_000_000.958)
        self.assertEqual(
            E.fill_created_s({"created_time": "2026-07-25T22:00:00Z"}),
            1_785_016_800)
        self.assertAlmostEqual(
            E.fill_created_s(
                {"created_time": "2026-07-25T22:00:00.958Z"}),
            1_785_016_800.958)
        self.assertIsNone(E.fill_created_s({}))
        self.assertIsNone(E.fill_created_s({"created_ts": "nan"}))

    def test_apply_fill_transfers_reservation_no_double_count(self):
        reset()
        E.S.orders[("M1", "bid")] = {"id": "x", "px": 0.40, "qty": 2.0,
                                     "t": 0}
        self.assertAlmostEqual(E.exposure(), 0.80)
        E.apply_fill({"ticker": "M1", "side": "yes", "count": 2,
                      "yes_price": 40, "created_ts": 1_770_000_000})
        # resting -> filled: exposure stays 0.80, never 1.60
        self.assertAlmostEqual(E.exposure(), 0.80)
        self.assertNotIn(("M1", "bid"), E.S.orders)
        self.assertEqual(E.S.net_pos["M1"]["y"], 2)
        self.assertIn(("M1", "bid"), E.S.latch)
        self.assertGreaterEqual(E.S.fills_cursor, 1_770_000_000)

    def test_apply_fill_partial_keeps_remainder_reserved(self):
        reset()
        E.S.orders[("M1", "bid")] = {"id": "x", "px": 0.40, "qty": 2.0,
                                     "t": 0}
        E.apply_fill({"ticker": "M1", "side": "yes", "count": 1,
                      "yes_price": 40, "created_ts": 1_770_000_000})
        self.assertIn(("M1", "bid"), E.S.orders)
        self.assertAlmostEqual(E.S.orders[("M1", "bid")]["qty"], 1.0)
        self.assertAlmostEqual(E.exposure(), 0.80)  # 0.40 rest + 0.40 fill

    def test_apply_fill_no_side_uses_no_price(self):
        reset()
        E.apply_fill({"ticker": "M1", "side": "no", "count": 2,
                      "no_price": 60, "created_ts": 1_770_000_000})
        self.assertEqual(E.S.net_pos["M1"]["n"], 2)
        self.assertAlmostEqual(E.S.net_pos["M1"]["cost"], 1.20)
        self.assertIn(("M1", "ask_no"), E.S.latch)

    def test_apply_fill_dedupes_by_trade_id_across_ws_and_rest(self):
        # WS pushes the fill, then the REST poll returns the same record:
        # it must be applied exactly once or inventory double-counts.
        reset()
        f = {"ticker": "M1", "side": "yes", "count": 2, "yes_price": 40,
             "created_ts": 1_770_000_000, "trade_id": "t-123"}
        E.apply_fill(f)
        E.apply_fill(dict(f))
        self.assertEqual(E.S.net_pos["M1"]["y"], 2)
        self.assertAlmostEqual(E.S.net_pos["M1"]["cost"], 0.80)

    def test_ws_fill_normalizes_to_rest_shape(self):
        reset()
        msg = {"market_ticker": "M1", "side": "no", "count": 3,
               "no_price": 60, "yes_price": 40, "ts": 1_770_000_000,
               "trade_id": "t-9"}
        f = E.ws_fill_to_rest(msg)
        E.apply_fill(f)
        self.assertEqual(E.S.net_pos["M1"]["n"], 3)
        self.assertAlmostEqual(E.S.net_pos["M1"]["cost"], 1.80)
        self.assertGreaterEqual(E.S.fills_cursor, 1_770_000_000)

    def test_startup_selfcheck_passes_when_probe_sees_fill(self):
        reset()
        newest = {"fills": [{"created_ts": 1_770_000_000}]}
        with mock.patch.object(E, "rest",
                               side_effect=[(200, newest), (200, newest)]):
            ok, why = E.fills_visibility_selfcheck()
        self.assertTrue(ok)

    def test_startup_selfcheck_fails_on_unit_bug_empty_probe(self):
        # a fill exists, but querying min_ts just below it returns empty:
        # exactly the D1 failure shape -> refuse to go live
        reset()
        newest = {"fills": [{"created_ts": 1_770_000_000}]}
        with mock.patch.object(E, "rest",
                               side_effect=[(200, newest), (200, {"fills": []})]):
            ok, why = E.fills_visibility_selfcheck()
        self.assertFalse(ok)

    def test_startup_selfcheck_fails_on_http_error(self):
        reset()
        with mock.patch.object(E, "rest", return_value=(500, {"error": "x"})):
            ok, why = E.fills_visibility_selfcheck()
        self.assertFalse(ok)


class F4_EconomicBreaker(unittest.TestCase):
    """F4: realized P&L comes from EXCHANGE settlements (never from
    balance, never from memory).  Two zero-revenue settlements with
    nonzero cost — the exact 2026-07-25 signature — trip the breaker;
    so does cumulative realized below the econ threshold.  The breaker
    halts without auto-resume.
    """

    def _settle(self, ticker, revenue_c, cost_c, when="2026-07-25T22:15:00Z"):
        return {"ticker": ticker, "market_result": "no",
                "revenue": revenue_c, "yes_total_cost": cost_c,
                "no_total_cost": 0, "settled_time": when}

    def test_two_zero_revenue_settlements_trip_breaker(self):
        reset()
        E.apply_settlement(self._settle("KXBTC15M-A", 0, 80))
        self.assertFalse(E.S.halted)          # one bad settle: not yet
        E.apply_settlement(self._settle("KXBTC15M-B", 0, 60,
                                        "2026-07-25T22:30:00Z"))
        self.assertTrue(E.S.halted)

    def test_profitable_settlement_resets_streak_and_accumulates(self):
        reset()
        E.apply_settlement(self._settle("A", 0, 100))
        E.apply_settlement(self._settle("B", 400, 232,
                                        "2026-07-25T22:30:00Z"))
        self.assertFalse(E.S.halted)
        E.apply_settlement(self._settle("C", 0, 100,
                                        "2026-07-25T22:45:00Z"))
        self.assertFalse(E.S.halted)          # streak was reset by B
        self.assertAlmostEqual(E.S.realized, -1.00 + 1.68 - 1.00, places=6)

    def test_cumulative_loss_beyond_econ_threshold_halts(self):
        reset()
        # -$1.10 then -$1.00: crosses the -$2 econ line on the second
        E.apply_settlement(self._settle("A", 0, 110))
        E.S.settle_zero_streak = 0            # isolate the cumulative rule
        E.apply_settlement(self._settle("B", 50, 150,
                                        "2026-07-25T22:30:00Z"))
        self.assertTrue(E.S.halted)

    def test_only_settlements_after_session_start_are_applied(self):
        # First live boot applied ALL-TIME history (realized $45,674
        # garbage) -> the -$2 econ threshold compared against noise.
        # Only settlements settled after process start may count.
        reset()
        E.S.session_start_ts = 1_785_027_600     # process start (epoch s)
        old = self._settle("OLD", 0, 80, "2026-07-25T21:00:00Z")
        new = self._settle("NEW", 0, 80, "2026-07-26T01:05:00Z")
        self.assertFalse(E.settlement_in_session(old))
        self.assertTrue(E.settlement_in_session(new))

    def test_settlement_applied_once_despite_repolls(self):
        reset()
        s = self._settle("A", 400, 232)
        E.apply_settlement(s)
        E.apply_settlement(s)
        E.apply_settlement(dict(s))
        self.assertAlmostEqual(E.S.realized, 1.68, places=6)


class F5_MinRequoteThreshold(unittest.TestCase):
    """F5 (D7): 98.8% of live requote intents were rate-limiter blocks
    (18,854 vs 232 orders) because ANY 0.1c book move triggered
    cancel+replace.  Sub-threshold moves must HOLD the resting order.
    """

    SER = "KXBTC15M"

    def _arm_tail_market(self):
        """YES quoted in the 90c+ tail band where deci-cent moves exist:
        y best 95.0c, ask 96.0c, kernel fair ~97c."""
        reset()
        now = E.time.time()
        ticks = [
            65000.0 + (3.0 if i % 2 else -3.0) for i in range(301)
        ]
        seed_timed_rti(self.SER, ticks, now=now)
        tte = 700.0
        sigma = max(
            rp.sigma_from_timed_ticks(
                ticks, list(E.S.rti_src_ms[self.SER]), window_s=60),
            rp.sigma_from_timed_ticks(
                ticks, list(E.S.rti_src_ms[self.SER]), window_s=300),
        )
        import math
        inside = (
            rp.rw_avg_var_factor(rp.LOCK_TICKS - 1)
            / (rp.LOCK_TICKS ** 2)
        )
        sd = sigma * math.sqrt((tte - rp.LOCK_TICKS) + inside)
        strike = ticks[-1] - 1.8808 * sd          # p ~= 0.97
        E.S.meta["M1"] = (self.SER, now + tte, strike)
        E.S.books["M1"] = {"y": {9500: 10.0}, "n": {400: 10.0}}
        return ticks[-1], sd, now + tte

    def test_subthreshold_move_holds_resting_order(self):
        self._arm_tail_market()
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
            self.assertAlmostEqual(E.S.orders[("M1", "bid")]["px"], 0.951,
                                   places=4)
            # book improves 0.2c: below the 0.5c threshold -> hold
            E.S.books["M1"]["y"][9520] = 10.0
            advance_timed_rti(self.SER)
            E.think()
        self.assertAlmostEqual(E.S.orders[("M1", "bid")]["px"], 0.951,
                               places=4)
        self.assertGreaterEqual(E.S.requote_suppressed, 1)

    def test_threshold_move_replaces_order(self):
        self._arm_tail_market()
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
            E.S.books["M1"]["y"][9560] = 10.0     # +0.6c >= threshold
            E.S.orders[("M1", "bid")]["t"] -= E.MIN_QUOTE_AGE_S + 1
            advance_timed_rti(self.SER)
            E.think()
        self.assertAlmostEqual(E.S.orders[("M1", "bid")]["px"], 0.957,
                               places=4)

    def test_unwanted_side_still_cancels_regardless_of_threshold(self):
        # threshold must never keep an order alive once the side turns
        # rich vs kernel fair — risk-off cancels bypass F5
        last, sd, close_s = self._arm_tail_market()
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
            self.assertIn(("M1", "bid"), E.S.orders)
            # kernel fair drops to ~85c (inside the sentinel, but the
            # 95.1c quote is now rich): want=False must cancel at once
            E.S.meta["M1"] = (self.SER, close_s, last - 1.0364 * sd)
            advance_timed_rti(self.SER)
            E.think()
        self.assertNotIn(("M1", "bid"), E.S.orders)

    def test_queue_hysteresis_never_overrides_a_closed_strategy_zone(self):
        # Queue age is useful only inside a regime whose measured
        # adverse-selection gate still permits entry.  Crossing from the
        # >=10m all-market zone into the 5-10m near-50 exclusion must drain
        # an old quote even when that quote retains positive kernel edge.
        F0_KernelSideSelection._arm_market(
            F0_KernelSideSelection(methodName="run"), 55.0)
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
            self.assertIn(("M1", "bid"), E.S.orders)
            ser, _close_s, strike = E.S.meta["M1"]
            E.S.meta["M1"] = (ser, E.time.time() + 500.0, strike)
            advance_timed_rti(ser)
            E.think()
        self.assertNotIn(("M1", "bid"), E.S.orders)


class MD_SessionWatchdog(unittest.TestCase):
    """Market-discovery deadlock (found live in shadow 2026-07-26):
    after the subscribed market settled, its channel went silent and the
    old `async for raw in ws` session-rotation check only ran on message
    arrival — so fetch_open() never re-ran and markets stayed 0 for 18
    minutes while the exchange had an open market.  The session must
    end within its budget even on a COMPLETELY SILENT channel.
    """

    def test_silent_channel_session_still_ends_within_budget(self):
        import asyncio

        class SilentWS:
            async def recv(self):
                await asyncio.sleep(3600)      # never speaks

        async def run():
            with mock.patch.object(E, "MD_SESSION_S", 0.3), \
                 mock.patch.object(E, "MD_RECV_TIMEOUT_S", 0.05):
                # must return on its own well before the outer timeout
                await asyncio.wait_for(E._md_session(SilentWS()), timeout=2.0)

        asyncio.get_event_loop().run_until_complete(run())

    def test_messages_still_processed_and_budget_still_enforced(self):
        import asyncio

        class BookWS:
            def __init__(self):
                self.sent = 0

            async def recv(self):
                self.sent += 1
                await asyncio.sleep(0.01)
                return json.dumps({
                    "type": "orderbook_snapshot",
                    "msg": {"market_ticker": "MWS",
                            "yes": [[0.45, 7]], "no": [[0.53, 5]]}})

        reset()
        ws = BookWS()

        async def run():
            with mock.patch.object(E, "MD_SESSION_S", 0.2), \
                 mock.patch.object(E, "MD_RECV_TIMEOUT_S", 0.05), \
                 mock.patch.object(E, "think"):
                await asyncio.wait_for(E._md_session(ws), timeout=2.0)

        asyncio.get_event_loop().run_until_complete(run())
        self.assertGreater(ws.sent, 1)
        self.assertIn("MWS", E.S.books)


class QuoteEvalTelemetry(unittest.TestCase):
    """The live probe's primary product is EVIDENCE.  Every pricing
    evaluation must leave a receipt — kernel fair, sigma, mid, both
    edges, inventory, want flags — throttled to 1/s per market so the
    calibration study covers the times we chose NOT to quote."""

    def test_eval_logged_with_full_fields_and_throttled(self):
        F0_KernelSideSelection._arm_market(
            F0_KernelSideSelection(methodName="run"), 55.0)
        events = []
        with (
            mock.patch.object(E.L, "w", side_effect=lambda o: events.append(o)),
            mock.patch.object(E, "load_control", return_value=False),
        ):
            E.think()
            E.think()                      # same second: must be throttled
        evals = [e for e in events if e.get("ev") == "QUOTE_EVAL"]
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        for key in ("mt", "tte", "fair_c", "mid_c", "sigma", "rti",
                    "edge_bid", "edge_no", "net", "want_bid", "want_no",
                    "y_px", "n_px", "zone_ok"):
            self.assertIn(key, ev)
        self.assertAlmostEqual(ev["fair_c"], 55.0, delta=0.3)

    def test_eval_reemitted_after_one_second(self):
        F0_KernelSideSelection._arm_market(
            F0_KernelSideSelection(methodName="run"), 55.0)
        events = []
        with (
            mock.patch.object(E.L, "w", side_effect=lambda o: events.append(o)),
            mock.patch.object(E, "load_control", return_value=False),
        ):
            E.think()
            E.S.last_eval["M1"] -= 1.1     # pretend a second passed
            advance_timed_rti("KXBTC15M")
            E.think()
        evals = [e for e in events if e.get("ev") == "QUOTE_EVAL"]
        self.assertEqual(len(evals), 2)


class PricingKernel(unittest.TestCase):
    """The FV that gates side selection must be arithmetically sane."""

    def test_atm_is_half(self):
        self.assertAlmostEqual(
            rp.p_settle_above(65000.0, 65000.0, 3.0, 600.0), 0.5, places=6)

    def test_deep_itm_approaches_one(self):
        p = rp.p_settle_above(65500.0, 65000.0, 3.0, 120.0)
        self.assertGreater(p, 0.95)

    def test_lock_in_collapses_uncertainty(self):
        p = rp.p_settle_above(65000.0, 65000.0, 3.0, 5.0,
                              locked_sum=55 * 65010.0, locked_n=55)
        self.assertGreater(p, 0.9)

    def test_edges_use_complementary_no_bid(self):
        e_y, e_n = rp.maker_edges(0.5, 47.0, 49.0)
        self.assertAlmostEqual(e_y, 3.0)
        # YES ask 49 implies NO bid 51, so buying NO is 1c rich.
        self.assertAlmostEqual(e_n, -1.0)
        self.assertAlmostEqual(e_y + e_n, 2.0)


class RealSchemaContract(unittest.TestCase):
    """Incident #3 (2026-07-26 01:2xZ): three schema-name guesses each
    blinded a safety layer.  These tests pin the VERBATIM records
    captured live — the engine must read THESE, not the names we wish
    the exchange used."""

    REAL_WS_FILL = {"action": "sell", "book_side": "ask",
                    "count_fp": "2.00",
                    "created_time": "2026-07-26T01:21:34.292512Z",
                    "fee_cost": "0.000000", "is_taker": False,
                    "market_ticker": "KXBTC15M-26JUL252130-30",
                    "no_price_dollars": "0.7500", "outcome_side": "no",
                    "side": "no", "trade_id": "t-real-1",
                    "yes_price_dollars": "0.2500"}
    REAL_POSITION = {"ticker": "KXBTC15M-26JUL252130-30",
                     "position_fp": "-20.00",
                     "market_exposure_dollars": "14.680000",
                     "fees_paid_dollars": "0.000000",
                     "realized_pnl_dollars": "0.000000",
                     "total_traded_dollars": "14.680000"}
    REAL_SETTLEMENT = {"event_ticker": "KXBTC15M-26JUL252130",
                       "fee_cost": "0.000000", "market_result": "no",
                       "no_count_fp": "20.00",
                       "no_total_cost_dollars": "14.680000",
                       "revenue": 2000,
                       "settled_time": "2026-07-26T01:30:06.038277Z",
                       "ticker": "KXBTC15M-26JUL252130-30", "value": 0,
                       "yes_count_fp": "0.00",
                       "yes_total_cost_dollars": "0.000000"}

    def setUp(self):
        reset()

    def test_ws_fill_applies_to_ledger(self):
        E.apply_fill(E.ws_fill_to_rest(dict(self.REAL_WS_FILL)))
        d = E.S.net_pos["KXBTC15M-26JUL252130-30"]
        self.assertEqual(d["n"], 2.0)
        self.assertAlmostEqual(d["cost"], 2 * 0.75)
        self.assertIn(("KXBTC15M-26JUL252130-30", "ask_no"), E.S.latch)

    def test_rest_fill_same_record_dedupes_by_trade_id(self):
        E.apply_fill(E.ws_fill_to_rest(dict(self.REAL_WS_FILL)))
        rest_shape = dict(self.REAL_WS_FILL)
        rest_shape["ticker"] = rest_shape.pop("market_ticker")
        E.apply_fill(rest_shape)
        self.assertEqual(E.S.net_pos["KXBTC15M-26JUL252130-30"]["n"], 2.0)

    def test_unparseable_fill_halts_live_and_keeps_cursor(self):
        blind = dict(self.REAL_WS_FILL)
        del blind["count_fp"]           # the exact incident-#3 shape
        cursor0 = E.S.fills_cursor
        with mock.patch.object(E, "MODE", "live"), \
                mock.patch.object(E, "cancel_all") as ca, \
                mock.patch.object(E, "write_control_status"):
            E.apply_fill(E.ws_fill_to_rest(blind))
        self.assertTrue(E.S.halted)
        ca.assert_called_once()
        self.assertEqual(E.S.fills_cursor, cursor0,
                         "cursor must not advance past an unapplied fill")
        self.assertEqual(E.S.net_pos, {})

    def test_settlement_real_schema_realized(self):
        self.assertTrue(E.apply_settlement(dict(self.REAL_SETTLEMENT)))
        self.assertAlmostEqual(E.S.realized, 5.32)
        self.assertEqual(E.S.settle_zero_streak, 0)

    def test_settlement_zero_revenue_streak_sees_dollar_costs(self):
        s = dict(self.REAL_SETTLEMENT)
        s["revenue"] = 0
        E.apply_settlement(s)
        self.assertEqual(E.S.settle_zero_streak, 1,
                         "cost must come from *_dollars fields, not the "
                         "legacy names that read 0")

    def test_unparseable_settlement_halts_live(self):
        s = dict(self.REAL_SETTLEMENT)
        del s["revenue"]
        with mock.patch.object(E, "MODE", "live"), \
                mock.patch.object(E, "cancel_all") as ca, \
                mock.patch.object(E, "write_control_status"):
            self.assertFalse(E.apply_settlement(s))
        self.assertTrue(E.S.halted)
        ca.assert_called_once()

    def test_recon_reads_position_fp(self):
        diffs = E.recon_divergences({}, [dict(self.REAL_POSITION)])
        self.assertEqual(diffs,
                         [("KXBTC15M-26JUL252130-30", 0, -20)])

    def test_recon_unparseable_position_is_divergence(self):
        p = {"ticker": "KXBTC15M-26JUL252130-30"}
        diffs = E.recon_divergences({}, [p])
        self.assertEqual(diffs, [("KXBTC15M-26JUL252130-30", 0, None)])

    def test_contract_selfcheck_passes_on_real_records(self):
        def fake_rest(method, path, body=None, host=None):
            if path.startswith("/portfolio/fills"):
                f = dict(self.REAL_WS_FILL)
                f["ticker"] = f.pop("market_ticker")
                return 200, {"fills": [f]}
            if path.startswith("/portfolio/positions"):
                return 200, {"market_positions": [dict(self.REAL_POSITION)]}
            return 200, {"settlements": [dict(self.REAL_SETTLEMENT)]}
        with mock.patch.object(E, "rest", fake_rest):
            ok, why = E.contract_selfcheck()
        self.assertTrue(ok, why)

    def test_contract_selfcheck_refuses_on_schema_break(self):
        def fake_rest(method, path, body=None, host=None):
            if path.startswith("/portfolio/positions"):
                # a record with neither position nor position_fp
                return 200, {"market_positions": [
                    {"ticker": "X", "exposure": "1.00"}]}
            if path.startswith("/portfolio/fills"):
                return 200, {"fills": []}
            return 200, {"settlements": []}
        with mock.patch.object(E, "rest", fake_rest):
            ok, why = E.contract_selfcheck()
        self.assertFalse(ok)
        self.assertIn("positions", why)

    def test_contract_selfcheck_empty_surfaces_pass_unverified(self):
        with mock.patch.object(E, "rest",
                               lambda *a, **k: (200, {})):
            ok, why = E.contract_selfcheck()
        self.assertTrue(ok)
        self.assertIn("EMPTY", why)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DirectionAuthorityAndFirstStrike(unittest.TestCase):
    """Audit 2026-07-26: Kalshi direction rules say outcome_side /
    book_side are authoritative; a 'sell yes' print is LONG NO (ask).
    Reading the bare 'side' field books the wrong side.  Plus the two
    first-strike rules: cancel-404 means GONE-maybe-FILLED (sync the
    ledger before freeing the slot), and insufficient_balance from the
    exchange means our ledger is wrong -> halt on the FIRST one, not
    after 317 retries."""

    SELL_YES_FILL = {"action": "sell", "book_side": "ask",
                     "outcome_side": "no", "side": "yes",
                     "count_fp": "2.00",
                     "fee_cost": "0.000000",
                     "created_time": "2026-07-26T01:21:34.292512Z",
                     "market_ticker": "KXBTC15M-26JUL252130-30",
                     "no_price_dollars": "0.7500",
                     "yes_price_dollars": "0.2500",
                     "trade_id": "t-dir-1"}

    def setUp(self):
        reset()

    def test_sell_yes_books_long_no(self):
        E.apply_fill(E.ws_fill_to_rest(dict(self.SELL_YES_FILL)))
        d = E.S.net_pos["KXBTC15M-26JUL252130-30"]
        self.assertEqual(d["n"], 2.0, "sell YES = long NO, not long YES")
        self.assertEqual(d["y"], 0)
        self.assertAlmostEqual(d["cost"], 2 * 0.75)
        self.assertIn(("KXBTC15M-26JUL252130-30", "ask_no"), E.S.latch)
        self.assertNotIn(("KXBTC15M-26JUL252130-30", "bid"), E.S.latch)

    def test_book_side_beats_bare_side(self):
        f = dict(self.SELL_YES_FILL)
        del f["outcome_side"]
        E.apply_fill(E.ws_fill_to_rest(f))
        self.assertEqual(
            E.S.net_pos["KXBTC15M-26JUL252130-30"]["n"], 2.0)

    def test_legacy_side_only_still_parses(self):
        f = dict(self.SELL_YES_FILL)
        del f["outcome_side"], f["book_side"], f["action"]
        E.apply_fill(E.ws_fill_to_rest(f))
        self.assertEqual(
            E.S.net_pos["KXBTC15M-26JUL252130-30"]["y"], 2.0)

    def test_insufficient_balance_halts_on_first_reject(self):
        calls = []

        def fake_rest(method, path, body=None, host=None):
            calls.append(method)
            return 400, {"error": "insufficient_balance"}
        with mock.patch.object(E, "MODE", "live"), \
                mock.patch.object(E, "rest", fake_rest), \
                mock.patch.object(E, "load_control", return_value=False), \
                mock.patch.object(E, "cancel_all") as ca, \
                mock.patch.object(E, "write_control_status"):
            out = E.order_place("KXBTC15M-X", "yes", 0.30, edge_c=1.0)
        self.assertIsNone(out)
        self.assertTrue(E.S.halted)
        ca.assert_called_once()

    def test_cancel_404_requires_proven_full_fill_before_freeing_slot(self):
        fill = dict(self.SELL_YES_FILL)
        fill["ticker"] = fill.pop("market_ticker")
        fill["order_id"] = "oid-1"
        E.S.orders[("KXBTC15M-26JUL252130-30", "ask_no")] = {
            "id": "oid-1", "px": 0.75, "qty": 2.0, "t": 0,
        }

        def fake_rest(method, path, body=None, host=None):
            if method == "DELETE":
                return 404, {"error": "order not found"}
            return 200, {"fills": [fill]}
        with mock.patch.object(E, "MODE", "live"), \
                mock.patch.object(E, "rest", fake_rest):
            ok = E.order_cancel("KXBTC15M-26JUL252130-30", "ask_no", "oid-1")
        self.assertTrue(ok)
        self.assertEqual(
            E.S.net_pos["KXBTC15M-26JUL252130-30"]["n"], 2.0,
            "404 = gone-maybe-FILLED: the fill must be booked before "
            "the caller frees the slot and requotes")


class PairingLoop(unittest.TestCase):
    """Engine-side pairing closed loop (audit 2026-07-26 must-list,
    MM_PAIR gated): fill one leg -> only the OPPOSITE leg may be added,
    pushed to the pair-cost ceiling; a netted YES/NO pair returns
    $1/contract (Kalshi nets positions) so realized locks 100c - both
    entries and collateral leaves the ledger NOW; unpaired inventory is
    capped at one clip and aged lots are cut by an IOC taker cross."""

    def setUp(self):
        reset()
        E.S.unpaired.clear()
        E.S.flatten_sent.clear()
        self._pair = mock.patch.object(E, "PAIR", True)
        self._pair.start()

    def tearDown(self):
        self._pair.stop()

    @staticmethod
    def fill(side, px_dollars, ct=2.0, ts="2026-07-26T02:00:00Z",
             tid="t"):
        yes = px_dollars if side == "yes" else round(1 - px_dollars, 4)
        no = round(1 - yes, 4)
        return {"ticker": "MKT", "outcome_side": side,
                "count_fp": f"{ct:.2f}", "created_time": ts,
                "yes_price_dollars": f"{yes:.4f}",
                "no_price_dollars": f"{no:.4f}", "trade_id": tid}

    def test_opposite_fills_pair_off_and_lock_realized(self):
        E.apply_fill(self.fill("yes", 0.30, tid="a"))
        E.apply_fill(self.fill("no", 0.65, tid="b"))
        d = E.S.net_pos["MKT"]
        self.assertEqual((d["y"], d["n"]), (0.0, 0.0))
        self.assertAlmostEqual(d["cost"], 0.0)
        self.assertAlmostEqual(E.S.realized, (1.0 - 0.30 - 0.65) * 2)
        self.assertEqual(E.unpaired_ct("MKT", "bid"), 0.0)
        self.assertEqual(E.unpaired_ct("MKT", "ask_no"), 0.0)

    def test_partial_fill_pairs_fifo_remainder_stays(self):
        E.apply_fill(self.fill("yes", 0.30, ct=2.0, tid="a"))
        E.apply_fill(self.fill("no", 0.65, ct=1.0, tid="b"))
        self.assertAlmostEqual(E.S.realized, (1.0 - 0.95) * 1)
        self.assertEqual(E.unpaired_ct("MKT", "bid"), 1.0)

    def test_entry_blocked_at_unpaired_cap_exit_side_open(self):
        """Operator speed directive 2026-07-27: the one-leg latch became a
        net-cap latch -- concurrent cycles are allowed up to MAX_NET, and
        entries block only AT the cap.  The exit side stays always open."""
        E.apply_fill(self.fill("yes", 0.30, ct=2.0, tid="a"))
        with mock.patch.object(E, "MAX_NET", 2):
            self.assertTrue(E.side_blocked("MKT", "bid"),
                            "at the net cap = no more entries this side")
        with mock.patch.object(E, "MAX_NET", 3):
            E.S.side_brake.clear()
            self.assertFalse(E.side_blocked("MKT", "bid"),
                             "below the cap entries stay open")
        self.assertFalse(E.side_blocked("MKT", "ask_no"),
                         "the exit leg must always be allowed")

    def test_rapid_fire_brake_blocks_side_after_two_quick_fills(self):
        """Two same-side fills inside BRAKE_N_S = a sweep; that side pauses
        BRAKE_HOLD_S while the exit side keeps working."""
        E.apply_fill(self.fill("yes", 0.30, tid="a"))
        E.apply_fill(self.fill("yes", 0.31, tid="b"))
        with mock.patch.object(E, "MAX_NET", 10):
            self.assertTrue(E.side_blocked("MKT", "bid"),
                            "brake must hold the swept side")
            self.assertFalse(E.side_blocked("MKT", "ask_no"))
        E.S.side_brake.clear()
        with mock.patch.object(E, "MAX_NET", 10):
            self.assertFalse(E.side_blocked("MKT", "bid"))

    def test_push_prices_opposite_leg_to_lock_ceiling(self):
        # Young-lot semantics: the complement is pinned at the PROFIT
        # ceiling.  The bounded-loss relaxation is age-driven and has its
        # own tests, so disable it here rather than let the fixture's fixed
        # timestamp decide what this case means.
        patcher = mock.patch.object(E, "ORPHAN_MAKER_MAX_LOSS_C", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        E.apply_fill(self.fill("yes", 0.30, tid="a"))
        # yes best bid 30c -> NO ask 70c; net-edge ceiling 98.5-30=68.5c;
        # legal whole-cent quote is 68c, not a forced 69c completion.
        y_px, n_px, exit_bid, exit_no = E.pair_push_prices(
            "MKT", 0.30, 0.50, 3000, 3200)
        self.assertTrue(exit_no)
        self.assertFalse(exit_bid)
        self.assertAlmostEqual(n_px, 0.68)

    def test_push_ceiling_binds_when_market_is_higher(self):
        patcher = mock.patch.object(E, "ORPHAN_MAKER_MAX_LOSS_C", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        E.apply_fill(self.fill("yes", 0.60, tid="a"))
        # net-edge ceiling 98.5-60=38.5c even though NO ask is 70c
        _, n_px, _, exit_no = E.pair_push_prices(
            "MKT", 0.60, 0.50, 3000, 3200)
        self.assertTrue(exit_no)
        self.assertAlmostEqual(n_px, 0.38)

    def test_pair_exit_does_not_add_collateral_over_pair_budget(self):
        E.apply_fill(self.fill("yes", 0.30, ct=2.0, tid="a"))
        with mock.patch.object(E, "MAX_OPEN_COST", 0.50):
            _, _, _, exit_no = E.pair_push_prices(
                "MKT", 0.30, 0.50, 3000, 3200)
        self.assertFalse(
            exit_no,
            "pair completion must not create more locked collateral than the cap",
        )

    def test_aged_lot_is_flattened_by_taker_cross(self):
        E.apply_fill(self.fill("yes", 0.30, tid="a",
                               ts="2026-07-26T02:00:00Z"))
        fill_ts = E.fill_created_s({"created_time":
                                    "2026-07-26T02:00:00Z"})
        due = E.flatten_due(fill_ts + E.UNPAIRED_AGE_S + 1)
        self.assertEqual(due, [("MKT", "bid")])
        E.S.books["MKT"] = {"y": {3000: 100}, "n": {6500: 100}}
        sent = []
        with mock.patch.object(E, "order_taker",
                               side_effect=lambda *a, **k: sent.append(a)):
            E.flatten_lot_taker("MKT", "bid", fill_ts + 91)
        # long YES cut = buy NO at its ask (1-0.30) + 1c buffer,
        # wire = ask side at 1 - no_px
        self.assertEqual(sent[0][0], "MKT")
        self.assertEqual(sent[0][1], "ask")
        self.assertAlmostEqual(sent[0][2], round(1 - 0.71, 4))
        # cooldown recorded so the 1s task does not spam IOCs
        self.assertIn(("MKT", "bid"), E.S.flatten_sent)

    def test_flatten_fill_flows_back_and_books_the_loss_cut(self):
        E.apply_fill(self.fill("yes", 0.30, tid="a"))
        E.apply_fill(self.fill("no", 0.72, tid="b"))   # the IOC's fill
        self.assertAlmostEqual(E.S.realized, (1.0 - 0.30 - 0.72) * 2)
        self.assertEqual(E.unpaired_ct("MKT", "bid"), 0.0)

    def test_stale_recon_blocks_new_entries_when_position_on_book(self):
        E.apply_fill(self.fill("yes", 0.30, ct=1.0, tid="a"))
        with mock.patch.object(E, "MODE", "live"):
            E.S.last_recon_ok_mono = None
            self.assertTrue(E.side_blocked("MKT2", "bid"),
                            "position on book + no fresh exchange "
                            "confirmation = no new entries")
            E.S.last_recon_ok_mono = E.time.monotonic()
            self.assertFalse(E.side_blocked("MKT2", "bid"))
