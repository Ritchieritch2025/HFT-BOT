"""Known-answer tests for the RTI pricing kernel (CF-benchmark-only line)."""
from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "research" / "crypto_mm"))

import rti_pricing as rp  # noqa: E402


class LockInArithmetic(unittest.TestCase):
    def test_all_ticks_locked_is_deterministic(self) -> None:
        # 60 ticks observed averaging above strike -> exactly 1.
        self.assertEqual(
            rp.p_settle_above(rti=100.0, strike=99.0, sigma_s=5.0,
                              tte_s=0, locked_sum=60 * 100.0, locked_n=60),
            1.0,
        )
        self.assertEqual(
            rp.p_settle_above(rti=100.0, strike=101.0, sigma_s=5.0,
                              tte_s=0, locked_sum=60 * 100.0, locked_n=60),
            0.0,
        )

    def test_uncertainty_shrinks_tick_by_tick(self) -> None:
        # Same spot sitting ON the strike; more locked ticks ABOVE the
        # strike must monotonically raise P(up).
        strike, rti, sig = 65000.0, 65000.0, 3.0
        p_prev = 0.5
        for k in (10, 30, 50, 59):
            locked = k * (strike + 5.0)   # locked ticks 5 above strike
            p = rp.p_settle_above(rti, strike, sig, tte_s=60 - k,
                                  locked_sum=locked, locked_n=k)
            self.assertGreater(p, p_prev - 1e-12)
            p_prev = p
        self.assertGreater(p_prev, 0.95)

    def test_at_the_money_prelock_is_half(self) -> None:
        p = rp.p_settle_above(rti=65000.0, strike=65000.0, sigma_s=3.0,
                              tte_s=600.0)
        self.assertAlmostEqual(p, 0.5, places=6)

    def test_prelock_variance_grows_with_tte(self) -> None:
        # Slightly in the money: more time -> closer to 0.5 (more noise).
        kw = dict(rti=65010.0, strike=65000.0, sigma_s=3.0)
        p_short = rp.p_settle_above(tte_s=120.0, **kw)
        p_long = rp.p_settle_above(tte_s=1200.0, **kw)
        self.assertGreater(p_short, p_long)
        self.assertGreater(p_long, 0.5)

    def test_prelock_official_window_variance_known_value(self) -> None:
        # Official observations are [close-60, close), so after walking one
        # second to the first observation the in-window innovation weights
        # are 59/60, 58/60, ..., 1/60.  The close tick is not observed.
        tte_s = 61.0
        inside = sum(k * k for k in range(1, 60)) / (60.0 ** 2)
        expected_z = -1.0 / math.sqrt((tte_s - 60.0) + inside)
        expected = 0.5 * (1.0 + math.erf(expected_z / math.sqrt(2.0)))

        actual = rp.p_settle_above(
            rti=0.0, strike=1.0, sigma_s=1.0, tte_s=tte_s)

        self.assertAlmostEqual(actual, expected, places=15)

    def test_prelock_boundary_just_above_final_minute_excludes_close(self) -> None:
        # Keep this on the pre-lock side of the branch while pinning its
        # limiting variance as TTE approaches 60 seconds from above.
        tte_s = math.nextafter(60.0, math.inf)
        inside = rp.rw_avg_var_factor(59) / (60.0 ** 2)
        expected_z = -1.0 / math.sqrt((tte_s - 60.0) + inside)
        expected = 0.5 * (1.0 + math.erf(expected_z / math.sqrt(2.0)))

        actual = rp.p_settle_above(
            rti=0.0, strike=1.0, sigma_s=1.0, tte_s=tte_s)

        self.assertAlmostEqual(actual, expected, places=15)

    def test_rw_avg_var_factor_known_values(self) -> None:
        self.assertEqual(rp.rw_avg_var_factor(1), 1.0)          # 1^2
        self.assertEqual(rp.rw_avg_var_factor(2), 5.0)          # 1+4
        self.assertEqual(rp.rw_avg_var_factor(3), 14.0)         # 1+4+9
        self.assertEqual(rp.rw_avg_var_factor(59), 70210.0)

    def test_zero_sigma_degenerates_to_step(self) -> None:
        self.assertEqual(
            rp.p_settle_above(65001.0, 65000.0, 0.0, tte_s=600.0), 1.0)
        self.assertEqual(
            rp.p_settle_above(64999.0, 65000.0, 0.0, tte_s=600.0), 0.0)


class SigmaEstimator(unittest.TestCase):
    def test_requires_min_window(self) -> None:
        self.assertIsNone(rp.sigma_from_ticks([1.0] * 10))

    def test_constant_series_returns_none(self) -> None:
        self.assertIsNone(rp.sigma_from_ticks([100.0] * 60))

    def test_known_alternating_series(self) -> None:
        ticks = [100.0 + (i % 2) for i in range(62)]   # diffs alternate +-1
        sig = rp.sigma_from_ticks(ticks)
        self.assertAlmostEqual(sig, 1.0, delta=0.02)

    def test_timed_estimator_requires_full_continuous_window(self) -> None:
        values = [100.0 + (i % 2) for i in range(60)]
        times = [i * 1000 for i in range(60)]
        self.assertIsNone(
            rp.sigma_from_timed_ticks(values, times, window_s=60))

    def test_timed_estimator_known_per_second_rate(self) -> None:
        values = [100.0 + (i % 2) for i in range(61)]
        times = [i * 1000 for i in range(61)]
        self.assertAlmostEqual(
            rp.sigma_from_timed_ticks(values, times, window_s=60),
            1.0,
            places=9,
        )

    def test_timed_estimator_gap_fails_closed(self) -> None:
        values = [100.0, 101.0, 100.0]
        times = [0, 1000, 3000]
        self.assertIsNone(
            rp.sigma_from_timed_ticks(
                values, times, window_s=3,
                min_coverage=0.5, max_gap_s=1.5))

    def test_zero_drift_model_does_not_erase_trend(self) -> None:
        values = [100.0 + i for i in range(61)]
        times = [i * 1000 for i in range(61)]
        self.assertAlmostEqual(
            rp.sigma_from_timed_ticks(values, times, window_s=60),
            1.0,
            places=9,
        )


class MakerEdges(unittest.TestCase):
    def test_edges_sum_to_spread(self) -> None:
        # Joining both best bids earns the spread in expectation:
        # e_yes + e_no = ask - bid, independent of p.
        e_yes, e_no = rp.maker_edges(0.5, yes_bid_c=49.0, yes_ask_c=51.0)
        self.assertAlmostEqual(e_yes + e_no, 2.0, places=9)
        # explicit: fair 50; yes edge = 50-49 = +1; no edge = 50-49 = +1
        self.assertAlmostEqual(e_yes, 1.0)
        self.assertAlmostEqual(e_no, 1.0)

    def test_skew_moves_edge_to_one_side(self) -> None:
        e_yes, e_no = rp.maker_edges(0.60, yes_bid_c=49.0, yes_ask_c=51.0)
        self.assertAlmostEqual(e_yes, 11.0)
        self.assertAlmostEqual(e_no, -9.0)


if __name__ == "__main__":
    unittest.main()
