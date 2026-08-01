"""Tests for the H6b politics path study runner on synthetic fixtures.

Covers the fail-closed behaviours that the sealed execution file makes
mandatory: the two-contiguous-week stop condition, the strict look-ahead
rule on ``large_trade_flag``, stop-beats-target bracket ordering,
K_cluster = 1 non-overlap, DEPTH_REJECT tallies, fee arithmetic, and the
SCAN complement census on declared pairs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import unittest

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools" / "research" / "alpha_sprint"))

import h6b_politics_path_study as h6b  # noqa: E402


SOURCE_BINDING = "ab" * 32


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_stage(
    namespace: Path,
    stage: str,
    dates: list,
    rows_by_date: dict,
    schema: list,
    create_sql: str,
) -> None:
    stage_dir = namespace / stage
    (stage_dir / "receipts").mkdir(parents=True)
    (stage_dir / "data").mkdir(parents=True)
    con = duckdb.connect()
    partitions = []
    for date in dates:
        key = f"date={date}_bucket=00"
        data_path = stage_dir / "data" / f"{key}.parquet"
        con.execute("DROP TABLE IF EXISTS t")
        con.execute(create_sql)
        for row in rows_by_date.get(date, []):
            placeholders = ",".join("?" for _ in row)
            con.execute(f"INSERT INTO t VALUES ({placeholders})", row)
        con.execute(
            f"COPY t TO '{data_path}' (FORMAT PARQUET)"
        )
        payload = data_path.read_bytes()
        receipt = {
            "state": "COMPLETE",
            "stage": stage,
            "partition_key": key,
            "source_binding": SOURCE_BINDING,
            "schema": schema,
            "data": {
                "path": f"{stage}/data/{key}.parquet",
                "sha256": _sha256(payload),
                "size_bytes": len(payload),
                "row_count": len(rows_by_date.get(date, [])),
            },
        }
        receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        receipt_path = stage_dir / "receipts" / f"{key}.json"
        receipt_path.write_bytes(receipt_bytes)
        partitions.append(
            {
                "partition_key": key,
                "receipt_path": f"{stage}/receipts/{key}.json",
                "receipt_sha256": _sha256(receipt_bytes),
                "data_sha256": _sha256(payload),
            }
        )
    manifest = {
        "state": "COMPLETE",
        "stage": stage,
        "source_binding": SOURCE_BINDING,
        "partitions": partitions,
        "partition_count": len(partitions),
    }
    (stage_dir / "MANIFEST.json").write_bytes(
        (json.dumps(manifest, sort_keys=True) + "\n").encode()
    )


L2_SCHEMA = sorted(h6b.L2_REQUIRED_COLUMNS)
TRADE_SCHEMA = sorted(h6b.TRADE_REQUIRED_COLUMNS | {"taker_side"})

L2_CREATE = (
    "CREATE TABLE t (date VARCHAR, t_us BIGINT, market_ticker VARCHAR, "
    "book_valid BOOLEAN, bid_e4 INTEGER, bid_qty_e4 BIGINT, "
    "ask_e4 INTEGER, ask_qty_e4 BIGINT)"
)
TRADE_CREATE = (
    "CREATE TABLE t (date VARCHAR, t_us BIGINT, market_ticker VARCHAR, "
    "count_e4 BIGINT, taker_side VARCHAR)"
)


def _l2_row(date, t_us, ticker, bid_c, ask_c, bid_qty=1000, ask_qty=1000):
    return (
        date, t_us, ticker, True,
        int(bid_c * 100), int(bid_qty * 10_000),
        int(ask_c * 100), int(ask_qty * 10_000),
    )


def _trade_row(date, t_us, ticker, size, side="yes"):
    return (date, t_us, ticker, int(size * 10_000), side)


def _dates(start: str, count: int) -> list:
    day = dt.date.fromisoformat(start)
    return [(day + dt.timedelta(days=i)).isoformat() for i in range(count)]


def _make_universe(path: Path, markets: dict) -> Path:
    path.write_text(json.dumps({"markets": markets}, sort_keys=True))
    return path


class LargeFlagTest(unittest.TestCase):
    def test_trade_at_t_is_excluded(self) -> None:
        trades = {"t_us": [50, 100], "size": [500.0, 500.0]}
        flag, max_t = h6b._large_flag(trades, 100, 100.0, 10_000)
        self.assertEqual(flag, 1)          # trade at t=50 inside [t-L, t)
        self.assertEqual(max_t, 50)        # trade at t=100 excluded
        flag, max_t = h6b._large_flag(trades, 50, 100.0, 10_000)
        self.assertEqual(flag, 0)          # nothing strictly before t=50
        self.assertEqual(max_t, -1)

    def test_window_lower_bound_inclusive(self) -> None:
        trades = {"t_us": [90], "size": [500.0]}
        flag, _ = h6b._large_flag(trades, 100, 100.0, 10)
        self.assertEqual(flag, 1)          # window [90, 100): 90 inside
        flag, _ = h6b._large_flag(trades, 101, 100.0, 10)
        self.assertEqual(flag, 0)          # window [91, 101): 90 aged out
        flag, _ = h6b._large_flag(trades, 200, 100.0, 10)
        self.assertEqual(flag, 0)


class FeeTest(unittest.TestCase):
    def test_quadratic_peak_and_ceiling(self) -> None:
        # 25 contracts at 50c: 0.07*25*0.25 = $0.4375 -> ceil 44c/order.
        fee = h6b.taker_fee_cents_per_contract(50.0, 25, 0.07)
        self.assertAlmostEqual(fee, 44 / 25)
        # Tail price is much cheaper than mid (fee scales with p(1-p)).
        self.assertLess(
            h6b.taker_fee_cents_per_contract(90.0, 25, 0.07),
            h6b.taker_fee_cents_per_contract(55.0, 25, 0.07),
        )


class EngineTest(unittest.TestCase):
    def _stream(self, ticks):
        return {
            "cluster": "CL1",
            "t_us": [t for t, *_ in ticks],
            "yes_bid_c": [b for _, b, _a in ticks],
            "yes_ask_c": [a for _, _b, a in ticks],
            "bid_qty": [1000.0] * len(ticks),
            "ask_qty": [1000.0] * len(ticks),
        }

    def test_stop_fires_and_blocks_overlap(self) -> None:
        # Favorite = YES (ask 70 > no_ask 32). Price collapses through
        # the stop, then recovers; only one position may be open at a
        # time (K_cluster = 1).
        ticks = [
            (0, 68, 70),
            (1_000, 55, 57),     # fav_bid 55 <= 70-10 -> STOP
            (2_000, 68, 70),     # re-entry allowed after exit
            (3_000, 80, 82),     # fav_bid 80 >= 70+10 -> TARGET
            (4_000, 80, 82),
        ]
        quotes = {"M1": self._stream(ticks)}
        rows = [(t, "M1", i) for i, (t, _b, _a) in enumerate(ticks)]
        result = h6b.run_cluster_pass(
            quotes, rows, {},
            variant="B0", horizon_us=3_600_000_000,
            bracket_up=10, bracket_down=10,
            clip=25, s_large=1e9, taker_rate=0.07,
        )
        self.assertFalse(result["lookahead_violation"])
        reasons = [o["exit_reason"] for o in result["opportunities"]]
        self.assertEqual(reasons[0], "STOP")
        self.assertIn("TARGET", reasons)
        # Entries never overlap an open position.
        spans = [
            (o["t_entry_us"], o["t_exit_us"])
            for o in result["opportunities"]
        ]
        for (s1, e1), (s2, _e2) in zip(spans, spans[1:]):
            self.assertGreaterEqual(s2, e1)

    def test_depth_reject(self) -> None:
        ticks = [(0, 68, 70), (1_000, 68, 70)]
        quotes = {"M1": self._stream(ticks)}
        quotes["M1"]["ask_qty"] = [10.0, 10.0]   # below clip 25
        rows = [(t, "M1", i) for i, (t, _b, _a) in enumerate(ticks)]
        result = h6b.run_cluster_pass(
            quotes, rows, {},
            variant="B0", horizon_us=3_600_000_000,
            bracket_up=10, bracket_down=10,
            clip=25, s_large=1e9, taker_rate=0.07,
        )
        self.assertEqual(len(result["opportunities"]), 0)
        self.assertEqual(result["depth_rejects"], 2)

    def test_b1_requires_flag_and_net_pnl_is_fee_and_haircut_inclusive(self) -> None:
        ticks = [(1_000_000, 68, 70), (2_000_000, 68, 70),
                 (4_000_000_000, 68, 70)]
        quotes = {"M1": self._stream(ticks)}
        rows = [(t, "M1", i) for i, (t, _b, _a) in enumerate(ticks)]
        trades = {"M1": {"t_us": [500_000], "size": [200.0]}}
        result = h6b.run_cluster_pass(
            quotes, rows, trades,
            variant="B1", horizon_us=3_600_000_000,
            bracket_up=10, bracket_down=10,
            clip=25, s_large=100.0, taker_rate=0.07,
        )
        self.assertGreaterEqual(len(result["opportunities"]), 1)
        opp = result["opportunities"][0]
        self.assertEqual(opp["flag"], 1)
        # Flat market: loss = spread (2) + 2 ticks haircut + 2 fees.
        entry_price = 70 + 1
        exit_price = 68 - 1
        fees = (
            h6b.taker_fee_cents_per_contract(entry_price, 25, 0.07)
            + h6b.taker_fee_cents_per_contract(exit_price, 25, 0.07)
        )
        self.assertAlmostEqual(
            opp["net_pnl_c"], (exit_price - entry_price) - fees
        )

    def test_b2_enters_anti_favorite(self) -> None:
        ticks = [(1_000_000, 68, 70), (2_000_000, 68, 70),
                 (4_000_000_000, 68, 70)]
        quotes = {"M1": self._stream(ticks)}
        rows = [(t, "M1", i) for i, (t, _b, _a) in enumerate(ticks)]
        trades = {"M1": {"t_us": [500_000], "size": [200.0]}}
        result = h6b.run_cluster_pass(
            quotes, rows, trades,
            variant="B2", horizon_us=3_600_000_000,
            bracket_up=10, bracket_down=10,
            clip=25, s_large=100.0, taker_rate=0.07,
        )
        self.assertGreaterEqual(len(result["opportunities"]), 1)
        self.assertEqual(result["opportunities"][0]["side"], "no")
        self.assertEqual(
            result["opportunities"][0]["side_class"], "anti_favorite"
        )


class WindowTest(unittest.TestCase):
    def test_contiguous_runs_split_on_gaps(self) -> None:
        runs = h6b._contiguous_runs(
            ["2026-07-10", "2026-07-11", "2026-07-13", "2026-07-14"]
        )
        self.assertEqual([len(r) for r in runs], [2, 2])


class ScanTest(unittest.TestCase):
    def test_complement_violation_detected_on_declared_pair(self) -> None:
        universe = {
            "politics_markets": {
                "PA": {"category": "Politics__X__2026",
                       "event_cluster": "E1", "complement_of": "PB"},
                "PB": {"category": "Politics__X__2026",
                       "event_cluster": "E1", "complement_of": "PA"},
            }
        }
        quotes = {
            "2026-07-10": {
                "PA": {
                    "cluster": "E1", "t_us": [0],
                    "yes_bid_c": [60.0], "yes_ask_c": [62.0],
                    "bid_qty": [100.0], "ask_qty": [100.0],
                },
                "PB": {
                    "cluster": "E1", "t_us": [0],
                    "yes_bid_c": [48.0], "yes_ask_c": [50.0],
                    "bid_qty": [100.0], "ask_qty": [100.0],
                },
            }
        }
        result = h6b.run_scan(quotes, universe, 25, 0.07, None)
        self.assertEqual(result["complement_violation_count"], 1)
        violation = result["complement_violations_top"][0]
        # Sell both YES: 60 + 48 = 108 collected vs at most 100 paid.
        self.assertAlmostEqual(violation["gross_capture_c"], 8.0)
        self.assertTrue(violation["capturable"])

    def test_non_exhaustive_cheap_pair_is_not_a_violation(self) -> None:
        # Both asks sum far below 100 — legitimate when neither outcome
        # may occur.  The old ask-side test wrongly flagged this.
        universe = {
            "politics_markets": {
                "PA": {"category": "Politics__X__2026",
                       "event_cluster": "E1", "complement_of": "PB"},
                "PB": {"category": "Politics__X__2026",
                       "event_cluster": "E1", "complement_of": "PA"},
            }
        }
        quotes = {
            "2026-07-10": {
                "PA": {
                    "cluster": "E1", "t_us": [0],
                    "yes_bid_c": [20.0], "yes_ask_c": [22.0],
                    "bid_qty": [100.0], "ask_qty": [100.0],
                },
                "PB": {
                    "cluster": "E1", "t_us": [0],
                    "yes_bid_c": [30.0], "yes_ask_c": [32.0],
                    "bid_qty": [100.0], "ask_qty": [100.0],
                },
            }
        }
        result = h6b.run_scan(quotes, universe, 25, 0.07, None)
        self.assertEqual(result["complement_violation_count"], 0)

    def test_no_pairs_declared_is_not_constructible(self) -> None:
        universe = {
            "politics_markets": {
                "PA": {"category": "Politics__X__2026", "event_cluster": "E1"},
            }
        }
        result = h6b.run_scan({}, universe, 25, 0.07, None)
        self.assertIn("NOT_CONSTRUCTIBLE", result["complement_checks"])


class EndToEndTest(unittest.TestCase):
    def _build_checkpoint(self, namespace: Path, dates: list) -> None:
        l2_rows = {}
        trade_rows = {}
        for date in dates:
            base = 1_000_000
            rows = []
            # One politics market, flat 68/70 book, hourly updates so
            # every 1h horizon has an exit row.
            for hour in range(6):
                t = base + hour * 3_600_000_000
                rows.append(_l2_row(date, t, "POL-A", 68, 70))
                rows.append(_l2_row(date, t + 1000, "POL-B", 60, 62))
            l2_rows[date] = rows
            trade_rows[date] = [
                _trade_row(date, base - 500_000, "POL-A", 150.0),
                _trade_row(date, base + 500_000, "POL-B", 3.0),
            ]
        _write_stage(namespace, "l2_replay", dates, l2_rows,
                     L2_SCHEMA, L2_CREATE)
        _write_stage(namespace, "trades_market", dates, trade_rows,
                     TRADE_SCHEMA, TRADE_CREATE)

    def test_single_day_window_halts(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            namespace = tmp_path / "ckpt"
            dates = _dates("2026-07-10", 1)     # no TRAIN/VALIDATE split
            self._build_checkpoint(namespace, dates)
            universe = _make_universe(
                tmp_path / "universe.json",
                {
                    "POL-A": {"category": "Politics__X__2026",
                              "event_cluster": "E1"},
                    "POL-B": {"category": "Politics__Y__2026",
                              "event_cluster": "E2"},
                },
            )
            output = tmp_path / "out"
            with self.assertRaises(h6b.H6bHalt) as ctx:
                h6b.run_study(namespace, output, universe)
            self.assertEqual(ctx.exception.reason_code, "WINDOW_TOO_SHORT")
            halt = json.loads((output / "H6B_HALT_RECEIPT.json").read_text())
            self.assertEqual(halt["state"], "HALTED")
            self.assertEqual(halt["halt_reason_code"], "WINDOW_TOO_SHORT")
            self.assertEqual(
                halt["halt_detail"]["longest_contiguous_days"], 1
            )

    def test_short_window_runs_with_deviation_flag(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            namespace = tmp_path / "ckpt"
            dates = _dates("2026-07-10", 8)     # 8 days -> 4 TRAIN / 4 VAL
            self._build_checkpoint(namespace, dates)
            universe = _make_universe(
                tmp_path / "universe.json",
                {
                    "POL-A": {"category": "Politics__X__2026",
                              "event_cluster": "E1"},
                    "POL-B": {"category": "Politics__Y__2026",
                              "event_cluster": "E2"},
                },
            )
            output = tmp_path / "out"
            receipt = h6b.run_study(
                namespace, output, universe, stage_only_contract=True
            )
            self.assertEqual(receipt["state"], "CONTRACT_ONLY")
            self.assertEqual(receipt["window_days"], 8)
            self.assertFalse(receipt["meets_sealed_14_day_design"])
            self.assertTrue(receipt["spec_deviations"])
            self.assertEqual(
                receipt["train_range"], ["2026-07-10", "2026-07-13"]
            )
            self.assertEqual(
                receipt["validate_range"], ["2026-07-14", "2026-07-17"]
            )

    def test_latest_contiguous_block_wins_over_longer_stale_block(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            namespace = tmp_path / "ckpt"
            # 5-day stale block, gap, then a 3-day block to the latest
            # date: the runner must use the 3-day block.
            dates = _dates("2026-07-01", 5) + _dates("2026-07-10", 3)
            self._build_checkpoint(namespace, dates)
            universe = _make_universe(
                tmp_path / "universe.json",
                {
                    "POL-A": {"category": "Politics__X__2026",
                              "event_cluster": "E1"},
                    "POL-B": {"category": "Politics__Y__2026",
                              "event_cluster": "E2"},
                },
            )
            output = tmp_path / "out"
            receipt = h6b.run_study(
                namespace, output, universe, stage_only_contract=True
            )
            self.assertEqual(receipt["window_days"], 3)
            self.assertEqual(
                receipt["train_range"], ["2026-07-10", "2026-07-11"]
            )
            self.assertEqual(
                receipt["validate_range"], ["2026-07-12", "2026-07-12"]
            )

    def test_contract_stage_reports_window_and_granularity(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            namespace = tmp_path / "ckpt"
            dates = _dates("2026-07-10", 14)
            self._build_checkpoint(namespace, dates)
            universe = _make_universe(
                tmp_path / "universe.json",
                {
                    "POL-A": {"category": "Politics__X__2026",
                              "event_cluster": "E1"},
                    "POL-B": {"category": "Politics__Y__2026",
                              "event_cluster": "E2"},
                },
            )
            output = tmp_path / "out"
            receipt = h6b.run_study(
                namespace, output, universe, stage_only_contract=True
            )
            self.assertEqual(receipt["state"], "CONTRACT_ONLY")
            self.assertEqual(
                receipt["train_range"], ["2026-07-10", "2026-07-16"]
            )
            self.assertEqual(
                receipt["validate_range"], ["2026-07-17", "2026-07-23"]
            )
            self.assertFalse(
                receipt["data_contract"]["settlement_outcome_availability"]
            )
            self.assertTrue(
                receipt["data_contract"]["fee_schedule"]["fee_modeled"]
            )

    def test_full_run_produces_sealed_receipt(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            namespace = tmp_path / "ckpt"
            dates = _dates("2026-07-10", 14)
            self._build_checkpoint(namespace, dates)
            universe = _make_universe(
                tmp_path / "universe.json",
                {
                    "POL-A": {"category": "Politics__X__2026",
                              "event_cluster": "E1"},
                    "POL-B": {"category": "Politics__Y__2026",
                              "event_cluster": "E2"},
                },
            )
            output = tmp_path / "out"
            receipt = h6b.run_study(namespace, output, universe)
            self.assertEqual(receipt["state"], "COMPLETE")
            self.assertTrue((output / "H6B_TRAIN_SEAL.json").is_file())
            self.assertTrue((output / "H6B_RECEIPT.json").is_file())
            self.assertTrue((output / "H6B_REPORT.md").is_file())
            self.assertEqual(len(receipt["train_seal_hash"]), 64)
            self.assertTrue(receipt["fee_modeled"])
            self.assertIn("G0", receipt["modules_run"])
            self.assertIn("SCAN", receipt["modules_run"])
            self.assertEqual(
                receipt["g0_result_per_module"]["H6A_MV"], "NOT_ESTIMABLE"
            )
            # Deterministic synthetic paths -> sigma tiny -> H6b gate
            # passes and the lattice runs.
            self.assertIn("H6B", receipt["modules_run"])
            self.assertIsNotNone(receipt["secondary_test_count"])
            shortlist_labels = {
                item["template"]: item["label"]
                for item in receipt["shortlist"]
            }
            self.assertIn(
                "H6a-MV calibration machinery validation", shortlist_labels
            )


if __name__ == "__main__":
    unittest.main()


class SettlementTruthTest(unittest.TestCase):
    def _stream(self, ticks):
        return {
            "cluster": "CL1",
            "t_us": [t for t, *_ in ticks],
            "yes_bid_c": [b for _, b, _a in ticks],
            "yes_ask_c": [a for _, _b, a in ticks],
            "bid_qty": [1000.0] * len(ticks),
            "ask_qty": [1000.0] * len(ticks),
        }

    def test_settlement_marks_close_instead_of_unresolved(self) -> None:
        ticks = [(1_000_000, 68, 70), (2_000_000, 68, 70)]
        quotes = {"M1": self._stream(ticks)}
        rows = [(t, "M1", i) for i, (t, _b, _a) in enumerate(ticks)]
        result = h6b.run_cluster_pass(
            quotes, rows, {},
            variant="B0", horizon_us=-1,          # close bucket
            bracket_up=10, bracket_down=10,
            clip=25, s_large=1e9, taker_rate=0.07,
            settlements_truth={"M1": ("yes", 5_000_000)},
        )
        self.assertTrue(result["opportunities"])
        opp = result["opportunities"][0]
        self.assertEqual(opp["exit_reason"], "SETTLEMENT")
        # Favorite side is YES at ask 70; settled yes -> value 100.
        entry_price = 70 + 1
        entry_fee = h6b.taker_fee_cents_per_contract(entry_price, 25, 0.07)
        self.assertAlmostEqual(
            opp["net_pnl_c"], (100.0 - entry_price) - entry_fee
        )

    def test_unresolved_close_without_settlement_record(self) -> None:
        ticks = [(1_000_000, 68, 70), (2_000_000, 68, 70)]
        quotes = {"M1": self._stream(ticks)}
        rows = [(t, "M1", i) for i, (t, _b, _a) in enumerate(ticks)]
        result = h6b.run_cluster_pass(
            quotes, rows, {},
            variant="B0", horizon_us=-1,
            bracket_up=10, bracket_down=10,
            clip=25, s_large=1e9, taker_rate=0.07,
            settlements_truth={},
        )
        self.assertTrue(result["opportunities"])
        self.assertEqual(
            result["opportunities"][0]["exit_reason"], "UNRESOLVED_CLOSE"
        )

    def test_settled_in_window_without_outcome_halts(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            namespace = tmp_path / "ckpt"
            dates = _dates("2026-07-10", 4)
            suite = EndToEndTest()
            suite._build_checkpoint(namespace, dates)
            universe = _make_universe(
                tmp_path / "universe.json",
                {
                    "POL-A": {"category": "Politics__X__2026",
                              "event_cluster": "E1",
                              "settled_in_window": True},
                    "POL-B": {"category": "Politics__Y__2026",
                              "event_cluster": "E2"},
                },
            )
            settlement_path = tmp_path / "settlements.json"
            settlement_path.write_text(json.dumps({"outcomes": {}}))
            output = tmp_path / "out"
            with self.assertRaises(h6b.H6bHalt) as ctx:
                h6b.run_study(
                    namespace, output, universe,
                    settlement_path=settlement_path,
                )
            self.assertEqual(
                ctx.exception.reason_code, "SETTLEMENT_TRUTH_INCOMPLETE"
            )
