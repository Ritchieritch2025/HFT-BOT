from __future__ import annotations

from collections import defaultdict
import copy
import hashlib
import json
from pathlib import Path
from typing import Dict, Optional

import pytest

from tools.research.crypto_mm import round4_stage2_terminal_labels as T


ROOT = Path(__file__).resolve().parents[1]
ROSTER = (
    ROOT
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_stage2_terminal_label_roster_20260726.json"
)


class AdvancingClock:
    def __init__(self) -> None:
        self.wall = 1_785_000_000_000_000_000
        self.mono = 900_000_000_000

    def __call__(self):
        self.wall += 1_000_000_000
        self.mono += 1_000_000_000
        return self.wall, self.mono


def market_body(
    ticker: str, *, result: str, source_date: str = "2026-07-22"
) -> bytes:
    market = {
        "ticker": ticker,
        "status": "finalized",
        "result": result,
        "settlement_value_dollars": (
            "1.0000" if result == "yes" else "0.0000"
        ),
        "settlement_ts": f"{source_date}T20:15:09.838000Z",
        "price_level_structure": "tapered_deci_cent",
        "price_ranges": copy.deepcopy(T.EXPECTED_PRICE_RANGES),
        "open_time": f"{source_date}T20:00:00Z",
        "close_time": f"{source_date}T20:15:00Z",
        "title": "unconsumed fields remain covered by raw SHA",
    }
    return T.canonical_json_bytes({"market": market})


class FakeTransport:
    def __init__(
        self,
        *,
        first_status_by_ticker: Optional[Dict[str, int]] = None,
    ) -> None:
        self.calls = defaultdict(int)
        self.first_status_by_ticker = first_status_by_ticker or {}
        self.result_by_ticker: Dict[str, str] = {}

    def __call__(self, url: str, timeout_seconds: int) -> T.HttpResult:
        assert timeout_seconds == T.TIMEOUT_SECONDS
        ticker = url.rsplit("/", 1)[-1]
        self.calls[ticker] += 1
        if (
            self.calls[ticker] == 1
            and ticker in self.first_status_by_ticker
        ):
            status = self.first_status_by_ticker[ticker]
            return T.HttpResult(
                status=status,
                final_url=url,
                body=b'{"error":"fixture"}',
                retry_after="1" if status == 429 else None,
            )
        if ticker not in self.result_by_ticker:
            self.result_by_ticker[ticker] = (
                "yes"
                if len(self.result_by_ticker) % 2 == 0
                else "no"
            )
        return T.HttpResult(
            status=200,
            final_url=url,
            body=market_body(
                ticker,
                result=self.result_by_ticker[ticker],
                source_date=TICKER_SOURCE_DATE[ticker],
            ),
        )


_, _ROSTER_ENTRIES, _ = T.load_roster(ROSTER)
TICKER_SOURCE_DATE = {
    entry.market_ticker: entry.source_date_utc
    for entry in _ROSTER_ENTRIES
}


def test_sealed_roster_is_exact_preterminal_72_market_authority() -> None:
    value, entries, raw_sha = T.load_roster(ROSTER)
    assert len(entries) == 72
    assert len({entry.market_ticker for entry in entries}) == 72
    assert {entry.source_date_utc for entry in entries} == set(
        T.EXPECTED_DATES
    )
    assert value["roster_markets_sha256"] == (
        "b4c1cf503c8c84417d7ab6c729501e0b0c22c6a63038edb0a7336829e8adf247"
    )
    assert raw_sha == hashlib.sha256(ROSTER.read_bytes()).hexdigest()
    assert (
        value["audited_contract_context"][
            "commit_or_extraction_binding_authorized"
        ]
        is False
    )


def test_complete_capture_preserves_raw_bodies_and_label_only_roots(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    receipt, receipt_path, sums_path = T.capture_terminal_labels(
        ROSTER,
        tmp_path / "capture",
        transport=transport,
        clock=AdvancingClock(),
        sleeper=lambda _: None,
        max_attempts=1,
    )
    assert receipt["status"] == T.COMPLETE_STATUS
    assert receipt["complete"] is True
    assert receipt["market_count_captured"] == 72
    assert receipt["raw_response_count"] == 72
    assert receipt["result_counts"] == {"YES": 36, "NO": 36}
    assert receipt["price_level_structures"] == ["tapered_deci_cent"]
    assert receipt["candidate_selection_authorized"] is False
    assert receipt["live_authorized"] is False
    assert (
        receipt["explicit_feature_firewall"][
            "all_values_from_this_capture_forbidden_from_decision_features"
        ]
        is True
    )
    assert (
        receipt["audited_contract_context"]["status"]
        == "REJECTED_BY_INDEPENDENT_AUDIT"
    )
    assert (
        receipt["audited_contract_context"][
            "commit_or_extraction_binding_authorized"
        ]
        is False
    )
    assert T.SHA256_RE.fullmatch(
        receipt["all_raw_body_content_root_sha256"]
    )
    assert T.SHA256_RE.fullmatch(
        receipt["selected_label_content_root_sha256"]
    )

    first = receipt["markets"][0]
    attempt = first["attempts"][0]
    raw = (receipt_path.parent / attempt["raw_response_path"]).read_bytes()
    assert attempt["raw_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert attempt["raw_response_bytes"] == len(raw)
    assert (
        attempt["request_started_wall_ns"]
        <= attempt["request_finished_wall_ns"]
    )
    assert first["label"]["official_result"] == "YES"
    assert first["label"]["settlement_ts"].endswith("Z")
    assert first["label"]["price_ranges"] == T.EXPECTED_PRICE_RANGES

    disk_receipt = json.loads(receipt_path.read_bytes())
    assert disk_receipt == receipt
    manifest = sums_path.read_text()
    assert (
        f"{hashlib.sha256(receipt_path.read_bytes()).hexdigest()}  "
        "terminal_label_receipt.json"
    ) in manifest
    assert len(manifest.splitlines()) == 73
    verified = T.verify_terminal_label_capture(receipt_path, sums_path)
    assert verified["status"] == "VERIFIED"
    assert verified["market_count"] == 72
    assert verified["receipt_sha256"] == hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()


def test_429_retry_preserves_both_raw_responses_in_content_root(
    tmp_path: Path,
) -> None:
    _, entries, _ = T.load_roster(ROSTER)
    first_ticker = entries[0].market_ticker
    transport = FakeTransport(first_status_by_ticker={first_ticker: 429})
    receipt, _, _ = T.capture_terminal_labels(
        ROSTER,
        tmp_path / "retry-capture",
        transport=transport,
        clock=AdvancingClock(),
        sleeper=lambda _: None,
        max_attempts=2,
    )
    assert receipt["complete"] is True
    assert receipt["raw_response_count"] == 73
    assert len(receipt["markets"][0]["attempts"]) == 2
    assert receipt["markets"][0]["attempts"][0]["http_status"] == 429
    assert receipt["markets"][0]["attempts"][1]["http_status"] == 200
    assert transport.calls[first_ticker] == 2


@pytest.mark.parametrize(
    "mutation, message",
    [
        (
            lambda value: value["markets_by_source_date_utc"][
                "2026-07-20"
            ].__setitem__(
                1,
                value["markets_by_source_date_utc"]["2026-07-20"][0],
            ),
            "unique",
        ),
        (
            lambda value: value["selection_authority"].__setitem__(
                "terminal_or_outcome_field_used", True
            ),
            "pre-terminal",
        ),
        (
            lambda value: value["audited_contract_context"].__setitem__(
                "commit_or_extraction_binding_authorized", True
            ),
            "firewall",
        ),
        (
            lambda value: value.__setitem__(
                "roster_markets_sha256", "0" * 64
            ),
            "hash mismatch",
        ),
    ],
)
def test_roster_mutations_fail_closed(mutation, message: str) -> None:
    value = json.loads(ROSTER.read_bytes())
    mutation(value)
    with pytest.raises(T.TerminalLabelError, match=message):
        T.validate_roster(value)


@pytest.mark.parametrize(
    "field, value, message",
    [
        ("ticker", "KXBTC15M-OTHER", "ticker mismatch"),
        ("status", "closed", "not finalized"),
        ("result", "maybe", "yes/no"),
        ("settlement_value_dollars", "0.5000", "payout disagree"),
        ("price_level_structure", "linear_cent", "structure drifted"),
        ("settlement_ts", "2026-07-22T20:14:59Z", "order is invalid"),
    ],
)
def test_terminal_response_semantic_drift_fails_closed(
    field: str, value: object, message: str
) -> None:
    ticker = "KXBTC15M-26JUL221915-15"
    payload = json.loads(market_body(ticker, result="yes"))
    payload["market"][field] = value
    with pytest.raises(T.TerminalLabelError, match=message):
        T.parse_terminal_market_response(
            T.canonical_json_bytes(payload), expected_ticker=ticker
        )


def test_nonretryable_http_failure_writes_incomplete_receipt(
    tmp_path: Path,
) -> None:
    _, entries, _ = T.load_roster(ROSTER)
    first_ticker = entries[0].market_ticker
    transport = FakeTransport(first_status_by_ticker={first_ticker: 404})
    receipt, receipt_path, sums_path = T.capture_terminal_labels(
        ROSTER,
        tmp_path / "incomplete-capture",
        transport=transport,
        clock=AdvancingClock(),
        sleeper=lambda _: None,
        max_attempts=2,
    )
    assert receipt["status"] == T.INCOMPLETE_STATUS
    assert receipt["complete"] is False
    assert receipt["market_count_captured"] == 71
    assert receipt["errors"][0]["market_ticker"] == first_ticker
    assert receipt_path.exists()
    assert sums_path.exists()
    verified = T.verify_terminal_label_capture(receipt_path, sums_path)
    assert verified["complete"] is False
    assert verified["market_count"] == 71


def test_disk_verifier_detects_raw_body_tampering(tmp_path: Path) -> None:
    transport = FakeTransport()
    receipt, receipt_path, sums_path = T.capture_terminal_labels(
        ROSTER,
        tmp_path / "tamper-capture",
        transport=transport,
        clock=AdvancingClock(),
        sleeper=lambda _: None,
        max_attempts=1,
    )
    raw_relative = receipt["markets"][0]["attempts"][0][
        "raw_response_path"
    ]
    raw_path = receipt_path.parent / raw_relative
    raw_path.chmod(0o600)
    raw_path.write_bytes(raw_path.read_bytes() + b"\n")
    with pytest.raises(T.TerminalLabelError, match="hash/size mismatch"):
        T.verify_terminal_label_capture(receipt_path, sums_path)
