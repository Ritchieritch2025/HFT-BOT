from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from h3crypto.coinbase import (
    CoinbaseCollector,
    CoinbaseMessageValidator,
    _products_from_message,
)
from h3crypto.common import (
    AppendOnlyLedger,
    CaptureSizeLimit,
    SequenceTracker,
    is_rfc3339,
)
from h3crypto.kalshi import (
    PUBLIC_REST_BASE,
    SERIES_POLICIES,
    allowed_public_url,
    validate_event,
    validate_market,
    validate_orderbook,
    validate_series,
)


def good_series(ticker: str) -> dict:
    policy = SERIES_POLICIES[ticker]
    return {
        "series": {
            "ticker": ticker,
            "title": policy.title,
            "category": "Crypto",
            "frequency": "fifteen_min",
            "settlement_sources": [
                {
                    "name": "CF Benchmarks",
                    "url": "https://www.cfbenchmarks.com/",
                }
            ],
            "contract_url": "https://assets.kalshi.com/regulatory/a.pdf",
            "contract_terms_url": "https://assets.kalshi.com/terms/a.pdf",
        }
    }


class SequenceTests(unittest.TestCase):
    def test_gap_duplicate_and_out_of_order(self) -> None:
        tracker = SequenceTracker()
        args = ("connection", "__CONNECTION_STREAM__", ["__ALL_ENVELOPES__"])
        self.assertEqual(tracker.observe(*args, 10)[0]["status"], "BASELINE")
        self.assertEqual(tracker.observe(*args, 11)[0]["status"], "CONTIGUOUS")
        gap = tracker.observe(*args, 14)[0]
        self.assertEqual(gap["status"], "GAP")
        self.assertEqual(gap["missing_count"], 2)
        self.assertEqual(tracker.observe(*args, 14)[0]["status"], "DUPLICATE")
        self.assertEqual(tracker.observe(*args, 13)[0]["status"], "OUT_OF_ORDER")

    def test_variable_fraction_rfc3339(self) -> None:
        for value in (
            "2026-07-24T06:50:26.49828Z",
            "2026-07-24T06:50:26.165354705Z",
            "2026-07-24T06:50:26Z",
        ):
            self.assertTrue(is_rfc3339(value), value)
        self.assertFalse(is_rfc3339("2026-13-99T06:50:26Z"))
        self.assertFalse(is_rfc3339("not-a-time"))


class CoinbaseTests(unittest.TestCase):
    def test_ticker_products_and_trade_time(self) -> None:
        payload = {
            "channel": "ticker",
            "timestamp": "2026-07-24T06:50:26.165354705Z",
            "sequence_num": 3,
            "events": [
                {
                    "type": "update",
                    "tickers": [
                        {
                            "type": "ticker",
                            "product_id": "BTC-USD",
                            "price": "65000.00",
                        }
                    ],
                }
            ],
        }
        self.assertEqual(_products_from_message(payload), ["BTC-USD"])
        result = CoinbaseMessageValidator().validate(payload)
        self.assertTrue(result["valid"], result)

    def test_l2_update_before_snapshot_fails_closed(self) -> None:
        validator = CoinbaseMessageValidator()
        update = {
            "channel": "l2_data",
            "timestamp": "2026-07-24T06:50:26.165354705Z",
            "sequence_num": 1,
            "events": [
                {
                    "type": "update",
                    "product_id": "BTC-USD",
                    "updates": [
                        {
                            "side": "bid",
                            "event_time": "2026-07-24T06:50:26.1Z",
                            "price_level": "65000",
                            "new_quantity": "1",
                        }
                    ],
                }
            ],
        }
        result = validator.validate(update)
        self.assertIn("L2_UPDATE_BEFORE_SNAPSHOT:BTC-USD", result["errors"])
        snapshot = json.loads(json.dumps(update))
        snapshot["events"][0]["type"] = "snapshot"
        self.assertTrue(validator.validate(snapshot)["valid"])
        self.assertTrue(validator.validate(update)["valid"])

    def test_channels_are_strict_and_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with AppendOnlyLedger(Path(temp)) as ledger:
                with self.assertRaises(ValueError):
                    CoinbaseCollector(ledger, channels=("ticker", "heartbeats"))
                with self.assertRaises(ValueError):
                    CoinbaseCollector(
                        ledger, channels=("market_trades", "user", "ticker")
                    )


class KalshiMappingTests(unittest.TestCase):
    def test_exact_series_passes_and_identity_drift_fails(self) -> None:
        policy = SERIES_POLICIES["KXBTC15M"]
        payload = good_series(policy.ticker)
        self.assertEqual(validate_series(payload, policy), [])
        payload["series"]["ticker"] = "KXBTC"
        self.assertIn("SERIES_TICKER_MISMATCH", validate_series(payload, policy))

    def test_title_category_and_source_are_independent_gates(self) -> None:
        policy = SERIES_POLICIES["KXETH15M"]
        for field, replacement, expected in (
            ("title", "ETH-ish", "SERIES_TITLE_MISMATCH"),
            ("category", "Sports", "SERIES_CATEGORY_MISMATCH"),
            ("settlement_sources", [], "SERIES_SETTLEMENT_SOURCES_MISMATCH"),
        ):
            payload = good_series(policy.ticker)
            payload["series"][field] = replacement
            self.assertIn(expected, validate_series(payload, policy))

    def test_event_and_market_membership_fields(self) -> None:
        policy = SERIES_POLICIES["KXBTC15M"]
        event_ticker = "KXBTC15M-26JUL240300"
        market_ticker = event_ticker + "-00"
        event = {
            "event": {
                "event_ticker": event_ticker,
                "series_ticker": policy.ticker,
                "category": "Crypto",
                "title": "BTC 15 min · $65,383.32 target",
                "settlement_sources": [
                    {
                        "name": "CF Benchmarks",
                        "url": "https://www.cfbenchmarks.com/",
                    }
                ],
                "markets": [{"ticker": market_ticker}],
            }
        }
        self.assertEqual(validate_event(event, event_ticker, policy), [])
        event["event"]["series_ticker"] = "KXETH15M"
        self.assertIn(
            "EVENT_SERIES_TICKER_MISMATCH",
            validate_event(event, event_ticker, policy),
        )

        market = {
            "market": {
                "ticker": market_ticker,
                "event_ticker": event_ticker,
                "title": policy.market_title,
                "market_type": "binary",
                "strike_type": "greater_or_equal",
                "open_time": "2026-07-24T06:45:00Z",
                "close_time": "2026-07-24T07:00:00Z",
                "updated_time": "2026-07-24T06:50:00.123456789Z",
                "rules_primary": "rule",
                "rules_secondary": "source",
            }
        }
        self.assertEqual(
            validate_market(market, market_ticker, event_ticker, policy), []
        )
        market["market"]["title"] = "BTC maybe"
        self.assertIn(
            "MARKET_TITLE_MISMATCH",
            validate_market(market, market_ticker, event_ticker, policy),
        )

    def test_orderbook_shape(self) -> None:
        good = {
            "orderbook_fp": {
                "yes_dollars": [["0.4000", "10.00"]],
                "no_dollars": [["0.5000", "20.00"]],
            }
        }
        self.assertEqual(validate_orderbook(good), [])
        good["orderbook_fp"]["yes_dollars"] = [["0.4"]]
        self.assertIn(
            "ORDERBOOK_YES_DOLLARS_LEVEL_SHAPE", validate_orderbook(good)
        )

    def test_only_official_public_get_surface_is_allowed(self) -> None:
        self.assertTrue(allowed_public_url(PUBLIC_REST_BASE + "/series/KXBTC15M"))
        self.assertFalse(
            allowed_public_url(
                "https://external-api.kalshi.com/trade-api/v2/portfolio/orders"
            )
        )
        self.assertFalse(
            allowed_public_url("http://external-api.kalshi.com/trade-api/v2/markets")
        )
        self.assertFalse(
            allowed_public_url("https://evil.example/trade-api/v2/markets")
        )


class AppendOnlyTests(unittest.TestCase):
    def test_append_preserves_prior_lines_and_new_run_refuses_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = AppendOnlyLedger(root, run_id="fixed")
            ledger.receipt("ONE")
            ledger.receipt("TWO")
            ledger.close()
            records = [
                json.loads(line)
                for line in (root / "fixed" / "receipts.ndjson").read_text().splitlines()
            ]
            self.assertEqual([item["event"] for item in records], ["ONE", "TWO"])
            with self.assertRaises(FileExistsError):
                AppendOnlyLedger(root, run_id="fixed")

    def test_data_cap_fails_before_append_but_receipt_remains_available(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with AppendOnlyLedger(
                Path(temp), run_id="cap", max_data_bytes=1
            ) as ledger:
                with self.assertRaises(CaptureSizeLimit):
                    ledger.append(
                        "coinbase",
                        {
                            "record_type": "coinbase_ws_envelope",
                            "source": "test",
                        },
                    )
                ledger.receipt("CAP_STILL_REPORTABLE")
                self.assertEqual(ledger.counts["receipts"], 1)


class SchemaTests(unittest.TestCase):
    def test_schema_is_json_and_has_get_only_constraint(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schema"
            / "record-v1.schema.json"
        )
        schema = json.loads(schema_path.read_text())
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        serialized = json.dumps(schema)
        self.assertIn('"const": "GET"', serialized)
        self.assertIn('"const": false', serialized)


if __name__ == "__main__":
    unittest.main()
