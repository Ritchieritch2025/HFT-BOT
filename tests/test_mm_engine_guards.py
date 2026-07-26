"""Hard-assertion tests for mm_engine (A1-A6) + pricing correctness.

These are the gates that must be green before any further live fire.
The engine module is imported with shadow mode and dummy credentials.
"""
from __future__ import annotations

import importlib
import json
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
    E.S.last_replace.clear(); E.S.books.clear(); E.S.meta.clear()
    E.S.recon_fails = 0
    E.S.fills_selfcheck_ok = False
    E.S.last_recon_ok_mono = None
    if hasattr(E.S, "requote_suppressed"):
        E.S.requote_suppressed = 0
    if hasattr(E.S, "fills_seen"):
        E.S.fills_seen.clear()
    E.S.realized = 0.0
    if hasattr(E.S, "settle_zero_streak"):
        E.S.settle_zero_streak = 0
    if hasattr(E.S, "settled_seen"):
        E.S.settled_seen.clear()
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
        ticks = [65000.0 + (3.0 if i % 2 else -3.0) for i in range(40)]
        E.S.rti[self.SER].clear()
        E.S.rti[self.SER].extend(ticks)
        E.S.rti_t[self.SER] = now
        tte = 700.0
        sigma = rp.sigma_from_ticks(list(ticks))
        self.assertIsNotNone(sigma)
        # invert the pre-lock variance for the strike hitting the target
        import math
        inside = rp.rw_avg_var_factor(rp.LOCK_TICKS) / (rp.LOCK_TICKS ** 2)
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


class F1_InventorySkew(unittest.TestCase):
    """F1: graduated quote retreat against inventory, before the hard
    MAX_NET latch.  gamma derivation: docs/research_reports/
    MM_SKEW_GAMMA_DERIVATION.md (capital-constraint bound, default
    0.5c/contract, replay-bracketed by grid_replay_v2 arms 0.2/0.5/1.0).
    """

    def _arm(self, fair_target_c, net_y=0, net_n=0):
        F0_KernelSideSelection._arm_market(
            F0_KernelSideSelection(methodName="run"), fair_target_c)
        if net_y or net_n:
            E.S.net_pos["M1"] = {"y": net_y, "n": net_n,
                                 "cost": 0.45 * net_y + 0.53 * net_n}

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

    def test_replay_of_incident_58_unseen_no_contracts_halts(self):
        reset()
        E.S.orders[("MKT", "bid")] = {"id": "shadow-1", "px": 0.4,
                                      "qty": 2.0, "t": 0}
        resp = {"market_positions": [{"ticker": "MKT", "position": -58}]}
        with mock.patch.object(E, "rest", return_value=(200, resp)):
            ok = E.recon_check()
        self.assertFalse(ok)
        self.assertTrue(E.S.halted)
        self.assertFalse(E.S.orders)          # cancel_all drained

    def test_small_divergence_within_tolerance_passes(self):
        reset()
        E.S.net_pos["MKT"] = {"y": 4, "n": 0, "cost": 1.8}
        resp = {"market_positions": [{"ticker": "MKT", "position": 5}]}
        with mock.patch.object(E, "rest", return_value=(200, resp)):
            self.assertTrue(E.recon_check())
        self.assertFalse(E.S.halted)

    def test_local_position_missing_on_exchange_halts(self):
        # local says long 5, exchange says flat -> phantom inventory
        reset()
        E.S.net_pos["MKT"] = {"y": 5, "n": 0, "cost": 2.0}
        resp = {"market_positions": []}
        with mock.patch.object(E, "rest", return_value=(200, resp)):
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
        self.assertEqual(
            E.fill_created_s({"created_time": "2026-07-25T22:00:00Z"}),
            1_785_016_800)
        self.assertIsNone(E.fill_created_s({}))

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
        self.assertGreaterEqual(E.S.fills_cursor, 1_770_000_001)

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
        self.assertGreaterEqual(E.S.fills_cursor, 1_770_000_001)

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
        ticks = [65000.0 + (3.0 if i % 2 else -3.0) for i in range(40)]
        E.S.rti[self.SER].clear()
        E.S.rti[self.SER].extend(ticks)
        E.S.rti_t[self.SER] = now
        tte = 700.0
        sigma = rp.sigma_from_ticks(list(ticks))
        import math
        inside = rp.rw_avg_var_factor(rp.LOCK_TICKS) / (rp.LOCK_TICKS ** 2)
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
            E.think()
        self.assertAlmostEqual(E.S.orders[("M1", "bid")]["px"], 0.951,
                               places=4)
        self.assertGreaterEqual(E.S.requote_suppressed, 1)

    def test_threshold_move_replaces_order(self):
        self._arm_tail_market()
        with mock.patch.object(E, "load_control", return_value=False):
            E.think()
            E.S.books["M1"]["y"][9560] = 10.0     # +0.6c >= threshold
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
