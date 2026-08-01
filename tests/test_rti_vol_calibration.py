from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1]
        / "tools" / "research" / "crypto_mm"),
)

import rti_vol_calibration as C  # noqa: E402


def _line(
    ts_ms: int,
    value: float,
    *,
    avg60=None,
    avg60_n=0,
    lock=None,
    lock_n=0,
) -> str:
    msg = {
        "index_id": "BRTI",
        "data": json.dumps({
            "type": "value", "time": ts_ms,
            "id": "BRTI", "value": str(value),
        }),
    }
    if lock is not None:
        msg["last_60s_windowed_average_15min"] = {
            "value": str(lock), "window_size": lock_n,
        }
    if avg60 is not None:
        msg["avg_60s_data"] = {
            "value": str(avg60), "window_size": avg60_n,
        }
    frame = {"type": "cfbenchmarks_value", "msg": msg}
    return json.dumps({"kind": "FRAME", "payload": json.dumps(frame)})


class Parse(unittest.TestCase):
    def test_nested_capture_frame(self):
        tick = C.parse_tick_line(
            _line(
                1_800_000, 100.5,
                avg60=100.75, avg60_n=60,
                lock=101.25, lock_n=60,
            ))
        self.assertEqual(tick.ts_ms, 1_800_000)
        self.assertEqual(tick.value, 100.5)
        self.assertEqual(tick.avg60, 100.75)
        self.assertEqual(tick.avg60_n, 60)
        self.assertEqual(tick.lock_avg, 101.25)
        self.assertEqual(tick.lock_n, 60)


class Sigma(unittest.TestCase):
    def test_timestamp_aware_known_rate(self):
        ticks = [
            C.Tick(ts_ms=i * 1000, value=100.0 + (i % 2))
            for i in range(61)
        ]
        sigma = C.sigma_per_s(ticks, 60, 60)
        self.assertAlmostEqual(sigma, 1.0, places=9)

    def test_gap_fails_closed(self):
        ticks = [
            C.Tick(ts_ms=0, value=100),
            C.Tick(ts_ms=1000, value=101),
            C.Tick(ts_ms=3000, value=100),
        ]
        self.assertIsNone(
            C.sigma_per_s(
                ticks, 2, 3, min_coverage=0.5, max_gap_s=1.5))


class Settlement(unittest.TestCase):
    def test_only_complete_quarter_close_is_authoritative(self):
        ticks = [
            C.Tick(
                ts_ms=900_000, value=100,
                avg60=99, avg60_n=59,
                lock_avg=999, lock_n=60,
            ),
            C.Tick(
                ts_ms=1_800_000, value=101,
                avg60=100, avg60_n=60,
                lock_avg=101.25, lock_n=60,
            ),
            C.Tick(
                ts_ms=1_801_000, value=101,
                avg60=777, avg60_n=60,
                lock_avg=888, lock_n=60,
            ),
        ]
        self.assertEqual(C.settlement_closes(ticks), {1_800_000: 100.0})


if __name__ == "__main__":
    unittest.main()
