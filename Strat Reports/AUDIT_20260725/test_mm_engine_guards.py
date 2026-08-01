"""Hard-assertion tests for mm_engine (A1-A6) + pricing correctness.

These are the gates that must be green before any further live fire.
The engine module is imported with shadow mode and dummy credentials.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
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
    E.S.open_cost = 0.0; E.S.halted = False; E.S.tokens = 8.0
    E.S.tok_t = 0.0; E.S.limit_breached = False
    E.S.control_error = ""; E.S.last_control_cancel = 0.0
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

    def test_live_unpaused_document_is_rejected(self):
        reset()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
