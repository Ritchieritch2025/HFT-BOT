from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

import pytest

from tools.research.pnl_spine import official_market_terminal_adapter as adapter
from tools.research.pnl_spine.official_market_terminal_adapter import (
    ADAPTER_CONFIG_SHA256,
    AUTHORITY_SCHEMA,
    RAW_PINS_SCHEMA,
    HttpResult,
    OfficialMarketAdapterError,
    _RejectRedirectHandler,
    _build_request,
    _direct_no_redirect_opener,
    _market_url,
    _parse_final_market_response,
    _read_pinned_file,
    _require_non_root,
    _validate_capture_receipt,
    _write_create_once,
    adapter_code_sha256,
    canonical_json_bytes,
    capture_markets,
    normalize_capture,
    validate_authority,
)


TICKER_A = "KXATPMATCH-26JUL17RUBBAE-RUB"
TICKER_B = "KXMLBGAME-26JUL17NYYBOS-NYY"


def utc_ns(text: str) -> int:
    return int(
        datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        * 1_000_000_000
    )


class FakeClock:
    def __init__(self) -> None:
        self.wall = utc_ns("2026-07-23T12:00:00Z")
        self.mono = 1_000_000

    def __call__(self) -> tuple[int, int]:
        self.wall += 1_000
        self.mono += 100
        return self.wall, self.mono


class FakeTransport:
    def __init__(self, responses: dict[str, tuple[int, bytes]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url: str, timeout: int) -> HttpResult:
        self.calls.append((url, timeout))
        status, body = self.responses[url]
        return HttpResult(status=status, final_url=url, body=body)


def authority(
    tickers: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    value = {
        "schema_version": AUTHORITY_SCHEMA,
        "authority_id": "A01-OFFICIAL-MARKET-20260723-01",
        "purpose": adapter.PURPOSE,
        "issued_at_utc": "2026-07-23T00:00:00Z",
        "expires_at_utc": "2026-07-24T00:00:00Z",
        "tickers": tickers or [TICKER_A],
        "adapter_code_sha256": adapter_code_sha256(),
        "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
    }
    raw = canonical_json_bytes(value) + b"\n"
    return value, hashlib.sha256(raw).hexdigest()


def market_response(
    ticker: str,
    *,
    status: str = "finalized",
    result: str = "yes",
    payout: str = "1.0000",
    structure: str = "linear_cent",
    ranges: object | None = None,
    omit: str | None = None,
) -> bytes:
    market: dict[str, Any] = {
        "ticker": ticker,
        "status": status,
        "result": result,
        "settlement_value_dollars": payout,
        "settlement_ts": "2026-07-17T14:32:02.613762Z",
        "price_level_structure": structure,
        "price_ranges": (
            [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}]
            if ranges is None
            else ranges
        ),
        "open_time": "2026-07-16T19:35:00Z",
        "close_time": "2026-07-17T14:29:56Z",
        "expected_expiration_time": "2026-07-17T15:00:00Z",
        "occurrence_datetime": "2026-07-17T14:20:00Z",
        # Additional official fields remain covered by the raw SHA.  The
        # adapter strictly validates every consumed field while tolerating
        # unrelated additive fields in the API market object.
        "title": "fixture",
    }
    if omit is not None:
        market.pop(omit)
    return canonical_json_bytes({"market": market})


def raw_pins(
    receipt: dict[str, Any],
    *,
    authority_sha: str,
    receipt_raw_sha: str,
) -> tuple[dict[str, Any], str]:
    selected: list[dict[str, Any]] = []
    for capture in receipt["captures"]:
        attempt = capture["attempts"][capture["selected_attempt_index"]]
        selected.append(
            {
                "ticker": capture["ticker"],
                "source_tier": attempt["source_tier"],
                "request_url": attempt["request_url"],
                "http_status": attempt["http_status"],
                "raw_size": attempt["raw_size"],
                "raw_sha256": attempt["raw_sha256"],
            }
        )
    value = {
        "schema_version": RAW_PINS_SCHEMA,
        "authority_raw_sha256": authority_sha,
        "capture_receipt_raw_sha256": receipt_raw_sha,
        "adapter_code_sha256": adapter_code_sha256(),
        "adapter_config_sha256": ADAPTER_CONFIG_SHA256,
        "selected_responses": selected,
    }
    raw = canonical_json_bytes(value) + b"\n"
    return value, hashlib.sha256(raw).hexdigest()


def capture_current(
    tmp_path: Path,
    *,
    ticker: str = TICKER_A,
    response: bytes | None = None,
) -> tuple[
    dict[str, Any],
    str,
    dict[str, Any],
    str,
    Path,
    FakeTransport,
]:
    auth, auth_sha = authority([ticker])
    url = _market_url(ticker, source_tier="current")
    transport = FakeTransport(
        {url: (200, response or market_response(ticker))}
    )
    output_dir = tmp_path / "capture"
    receipt, receipt_sha = capture_markets(
        auth,
        authority_raw_sha256=auth_sha,
        expected_code_sha256=adapter_code_sha256(),
        output_dir=output_dir,
        transport=transport,
        clock=FakeClock(),
    )
    return auth, auth_sha, receipt, receipt_sha, output_dir, transport


def test_current_200_normalizes_exact_terminal_and_honest_metadata(
    tmp_path: Path,
) -> None:
    auth, auth_sha, receipt, receipt_sha, directory, transport = (
        capture_current(tmp_path)
    )
    pins, pins_sha = raw_pins(
        receipt,
        authority_sha=auth_sha,
        receipt_raw_sha=receipt_sha,
    )
    output = normalize_capture(
        authority=auth,
        authority_raw_sha256=auth_sha,
        capture_receipt=receipt,
        capture_receipt_raw_sha256=receipt_sha,
        capture_dir=directory,
        raw_pins=pins,
        raw_pins_raw_sha256=pins_sha,
        expected_code_sha256=adapter_code_sha256(),
    )

    assert len(transport.calls) == 1
    assert transport.calls[0][0] == _market_url(
        TICKER_A, source_tier="current"
    )
    assert output["terminal_records"] == [
        {
            "settlement_id": (
                "kalshi-official:"
                f"{TICKER_A}:2026-07-17T14:32:02.613762Z:"
                f"{receipt['captures'][0]['attempts'][0]['raw_sha256'][:16]}"
            ),
            "market_ticker": TICKER_A,
            "status": "FINALIZED",
            "finalized": True,
            "yes_settlement_value_e4": 10_000,
            "observed_at_ns": receipt["captures"][0]["attempts"][0][
                "fetch_wall_ns_after"
            ],
            "revision": 0,
            "source_sha256": receipt["captures"][0]["attempts"][0][
                "raw_sha256"
            ],
        }
    ]
    evidence = output["metadata_evidence"][0]
    assert evidence["tick_size_e4"] == 100
    assert evidence["source_tier"] == "current"
    assert evidence["historical_asof_utc"] is None
    assert evidence["scheduled_start_ts_ns"] is None
    assert (
        evidence["scheduled_start_field_mapping"]
        == "BLOCKED_NO_AUTHORIZED_SCHEDULED_START_MAPPING"
    )
    assert (
        evidence["occurrence_datetime"]
        == "2026-07-17T14:20:00Z"
    )
    assert (
        "BLOCK_A01_HISTORICAL_POINT_IN_TIME_METADATA_INTERVALS_MISSING"
        in output["remaining_blockers"]
    )


def test_no_result_produces_zero_exact_yes_payout(tmp_path: Path) -> None:
    auth, auth_sha, receipt, receipt_sha, directory, _ = capture_current(
        tmp_path,
        response=market_response(
            TICKER_A,
            result="no",
            payout="0.0000",
        ),
    )
    pins, pins_sha = raw_pins(
        receipt,
        authority_sha=auth_sha,
        receipt_raw_sha=receipt_sha,
    )
    output = normalize_capture(
        authority=auth,
        authority_raw_sha256=auth_sha,
        capture_receipt=receipt,
        capture_receipt_raw_sha256=receipt_sha,
        capture_dir=directory,
        raw_pins=pins,
        raw_pins_raw_sha256=pins_sha,
        expected_code_sha256=adapter_code_sha256(),
    )
    assert output["terminal_records"][0]["yes_settlement_value_e4"] == 0


def test_historical_is_used_only_after_current_404(tmp_path: Path) -> None:
    auth, auth_sha = authority()
    current = _market_url(TICKER_A, source_tier="current")
    historical = _market_url(TICKER_A, source_tier="historical")
    transport = FakeTransport(
        {
            current: (404, b'{"error":"not found in current tier"}'),
            historical: (200, market_response(TICKER_A)),
        }
    )
    directory = tmp_path / "capture"
    receipt, receipt_sha = capture_markets(
        auth,
        authority_raw_sha256=auth_sha,
        expected_code_sha256=adapter_code_sha256(),
        output_dir=directory,
        transport=transport,
        clock=FakeClock(),
    )
    assert [url for url, _ in transport.calls] == [current, historical]
    capture = receipt["captures"][0]
    assert capture["selected_attempt_index"] == 1
    assert [a["http_status"] for a in capture["attempts"]] == [404, 200]
    assert (directory / "0000.current.response.json").read_bytes().startswith(
        b'{"error"'
    )

    pins, pins_sha = raw_pins(
        receipt,
        authority_sha=auth_sha,
        receipt_raw_sha=receipt_sha,
    )
    output = normalize_capture(
        authority=auth,
        authority_raw_sha256=auth_sha,
        capture_receipt=receipt,
        capture_receipt_raw_sha256=receipt_sha,
        capture_dir=directory,
        raw_pins=pins,
        raw_pins_raw_sha256=pins_sha,
        expected_code_sha256=adapter_code_sha256(),
    )
    assert output["metadata_evidence"][0]["source_tier"] == "historical"
    assert len(output["raw_response_evidence"]) == 2


@pytest.mark.parametrize("status", [301, 302, 307, 429, 500, 503])
def test_redirect_rate_limit_and_server_error_do_not_fallback(
    tmp_path: Path,
    status: int,
) -> None:
    auth, auth_sha = authority()
    current = _market_url(TICKER_A, source_tier="current")
    historical = _market_url(TICKER_A, source_tier="historical")
    transport = FakeTransport(
        {
            current: (status, b'{"error":"refused"}'),
            historical: (200, market_response(TICKER_A)),
        }
    )
    with pytest.raises(OfficialMarketAdapterError):
        capture_markets(
            auth,
            authority_raw_sha256=auth_sha,
            expected_code_sha256=adapter_code_sha256(),
            output_dir=tmp_path / f"capture-{status}",
            transport=transport,
            clock=FakeClock(),
        )
    assert [url for url, _ in transport.calls] == [current]


def test_transport_has_no_proxy_redirect_or_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8123")
    installed: list[object] = []
    sentinel = object()

    def fake_build_opener(*handlers: object) -> object:
        installed.extend(handlers)
        return sentinel

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    assert _direct_no_redirect_opener() is sentinel
    proxy_handlers = [
        handler
        for handler in installed
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert len(proxy_handlers) == 1 and proxy_handlers[0].proxies == {}
    assert any(
        isinstance(handler, _RejectRedirectHandler)
        for handler in installed
    )
    redirect = _RejectRedirectHandler()
    assert (
        redirect.redirect_request(
            _build_request(
                _market_url(TICKER_A, source_tier="current")
            ),
            io.BytesIO(),
            307,
            "redirect",
            {"Location": "https://attacker.invalid/steal"},
            "https://attacker.invalid/steal",
        )
        is None
    )
    request = _build_request(
        _market_url(TICKER_A, source_tier="current")
    )
    names = {name.lower() for name, _ in request.header_items()}
    assert "authorization" not in names
    assert "kalshi-access-key" not in names
    assert "kalshi-access-signature" not in names
    assert "proxy-authorization" not in names
    assert request.get_method() == "GET"


def test_urllib_transport_surfaces_redirect_without_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = _market_url(TICKER_A, source_tier="current")

    class RedirectingOpener:
        calls = 0

        def open(self, request: urllib.request.Request, timeout: int) -> Any:
            del timeout
            self.calls += 1
            raise urllib.error.HTTPError(
                request.full_url,
                307,
                "redirect",
                {"Location": "https://attacker.invalid/steal"},
                io.BytesIO(b'{"redirect":true}'),
            )

    opener = RedirectingOpener()
    monkeypatch.setattr(
        adapter, "_direct_no_redirect_opener", lambda: opener
    )
    result = adapter.urllib_transport(url, adapter.TIMEOUT_SECONDS)
    assert result.status == 307
    assert result.final_url == url
    assert opener.calls == 1


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            market_response(TICKER_A, omit="settlement_ts"),
            "schema drift",
        ),
        (
            market_response(TICKER_A, status="open"),
            "not finalized",
        ),
        (
            market_response(
                TICKER_A,
                ranges=[
                    {
                        "start": "0.0000",
                        "end": "1.0000",
                        "step": "0.0010",
                    }
                ],
            ),
            "non-standard tick",
        ),
        (
            market_response(TICKER_A, structure="deci_cent"),
            "non-standard price",
        ),
        (
            market_response(TICKER_A, result="yes", payout="0.0000"),
            "disagree",
        ),
    ],
)
def test_schema_nonfinal_and_nonstandard_tick_fail_closed(
    response: bytes,
    message: str,
) -> None:
    with pytest.raises(OfficialMarketAdapterError, match=message):
        _parse_final_market_response(
            response,
            expected_ticker=TICKER_A,
        )


def test_duplicate_json_keys_and_float_dollars_are_rejected() -> None:
    with pytest.raises(OfficialMarketAdapterError, match="duplicate"):
        adapter.parse_strict_json(
            b'{"market":{},"market":{}}',
            label="fixture",
        )
    raw = market_response(TICKER_A).replace(
        b'"settlement_value_dollars":"1.0000"',
        b'"settlement_value_dollars":1.0',
    )
    with pytest.raises(OfficialMarketAdapterError, match="floating-point"):
        _parse_final_market_response(raw, expected_ticker=TICKER_A)


def test_authority_requires_sorted_unique_tickers_and_live_validity() -> None:
    value, raw_sha = authority([TICKER_A, TICKER_A])
    with pytest.raises(OfficialMarketAdapterError, match="sorted and unique"):
        validate_authority(
            value,
            raw_sha256=raw_sha,
            expected_code_sha256=adapter_code_sha256(),
            now_wall_ns=utc_ns("2026-07-23T12:00:00Z"),
        )
    value, raw_sha = authority([TICKER_B, TICKER_A])
    with pytest.raises(OfficialMarketAdapterError, match="sorted and unique"):
        validate_authority(
            value,
            raw_sha256=raw_sha,
            expected_code_sha256=adapter_code_sha256(),
            now_wall_ns=utc_ns("2026-07-23T12:00:00Z"),
        )
    value, raw_sha = authority()
    with pytest.raises(OfficialMarketAdapterError, match="not valid"):
        validate_authority(
            value,
            raw_sha256=raw_sha,
            expected_code_sha256=adapter_code_sha256(),
            now_wall_ns=utc_ns("2026-07-25T12:00:00Z"),
        )


def test_ticker_path_attack_is_rejected() -> None:
    value, raw_sha = authority(["../../account/balance"])
    with pytest.raises(OfficialMarketAdapterError, match="ticker"):
        validate_authority(
            value,
            raw_sha256=raw_sha,
            expected_code_sha256=adapter_code_sha256(),
            now_wall_ns=utc_ns("2026-07-23T12:00:00Z"),
        )


def test_capture_receipt_path_attack_is_rejected(tmp_path: Path) -> None:
    _, _, receipt, _, _, _ = capture_current(tmp_path)
    attacked = json.loads(json.dumps(receipt))
    attacked["captures"][0]["attempts"][0]["raw_relative_path"] = (
        "../authority.json"
    )
    with pytest.raises(OfficialMarketAdapterError, match="raw path"):
        _validate_capture_receipt(attacked)
    attacked = json.loads(json.dumps(receipt))
    attacked["captures"][0]["attempts"][0][
        "adapter_config_sha256"
    ] = "0" * 64
    with pytest.raises(
        OfficialMarketAdapterError, match="config binding"
    ):
        _validate_capture_receipt(attacked)


def test_raw_body_tamper_and_raw_pin_tamper_fail_normalization(
    tmp_path: Path,
) -> None:
    auth, auth_sha, receipt, receipt_sha, directory, _ = capture_current(
        tmp_path
    )
    pins, pins_sha = raw_pins(
        receipt,
        authority_sha=auth_sha,
        receipt_raw_sha=receipt_sha,
    )
    raw_path = directory / "0000.current.response.json"
    os.chmod(raw_path, 0o600)
    raw_path.write_bytes(raw_path.read_bytes() + b" ")
    with pytest.raises(
        OfficialMarketAdapterError, match="size mismatch"
    ):
        normalize_capture(
            authority=auth,
            authority_raw_sha256=auth_sha,
            capture_receipt=receipt,
            capture_receipt_raw_sha256=receipt_sha,
            capture_dir=directory,
            raw_pins=pins,
            raw_pins_raw_sha256=pins_sha,
            expected_code_sha256=adapter_code_sha256(),
        )

    # A fresh capture proves the independent pin is also enforced.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _, _, receipt2, receipt_sha2, directory2, _ = capture_current(fresh)
    pins2, pins_sha2 = raw_pins(
        receipt2,
        authority_sha=auth_sha,
        receipt_raw_sha=receipt_sha2,
    )
    pins2["selected_responses"][0]["raw_sha256"] = "0" * 64
    with pytest.raises(OfficialMarketAdapterError, match="raw pin differs"):
        normalize_capture(
            authority=auth,
            authority_raw_sha256=auth_sha,
            capture_receipt=receipt2,
            capture_receipt_raw_sha256=receipt_sha2,
            capture_dir=directory2,
            raw_pins=pins2,
            raw_pins_raw_sha256=pins_sha2,
            expected_code_sha256=adapter_code_sha256(),
        )


def test_capture_output_directory_is_create_once(tmp_path: Path) -> None:
    auth, auth_sha = authority()
    directory = tmp_path / "capture"
    directory.mkdir()
    with pytest.raises(OfficialMarketAdapterError, match="create-once"):
        capture_markets(
            auth,
            authority_raw_sha256=auth_sha,
            expected_code_sha256=adapter_code_sha256(),
            output_dir=directory,
            transport=FakeTransport({}),
            clock=FakeClock(),
        )


def test_output_file_and_symlink_parent_are_create_once(
    tmp_path: Path,
) -> None:
    output = tmp_path / "receipt.json"
    first_sha = _write_create_once(output, b"first\n")
    assert first_sha == hashlib.sha256(b"first\n").hexdigest()
    with pytest.raises(OfficialMarketAdapterError, match="create-once"):
        _write_create_once(output, b"second\n")
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(OfficialMarketAdapterError, match="symlink"):
        _write_create_once(linked / "receipt.json", b"blocked\n")


def test_production_control_file_must_be_root_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "authority.json"
    raw = b"{}\n"
    control.write_bytes(raw)
    control.chmod(0o400)
    with pytest.raises(OfficialMarketAdapterError, match="root-owned"):
        _read_pinned_file(
            str(control),
            hashlib.sha256(raw).hexdigest(),
            label="authority",
            require_root_read_only=True,
        )
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(OfficialMarketAdapterError, match="non-root"):
        _require_non_root()


def test_clock_regression_is_rejected(tmp_path: Path) -> None:
    auth, auth_sha = authority()
    current = _market_url(TICKER_A, source_tier="current")
    samples = iter(
        [
            (utc_ns("2026-07-23T12:00:00Z"), 100),
            (utc_ns("2026-07-23T12:00:01Z"), 200),
            (utc_ns("2026-07-23T12:00:00Z"), 199),
        ]
    )
    with pytest.raises(OfficialMarketAdapterError, match="minimum"):
        capture_markets(
            auth,
            authority_raw_sha256=auth_sha,
            expected_code_sha256=adapter_code_sha256(),
            output_dir=tmp_path / "capture",
            transport=FakeTransport(
                {current: (200, market_response(TICKER_A))}
            ),
            clock=lambda: next(samples),
        )


def test_wrong_ticker_and_wrong_envelope_are_schema_drift() -> None:
    with pytest.raises(OfficialMarketAdapterError, match="ticker"):
        _parse_final_market_response(
            market_response(TICKER_B),
            expected_ticker=TICKER_A,
        )
    with pytest.raises(OfficialMarketAdapterError, match="keys mismatch"):
        _parse_final_market_response(
            canonical_json_bytes(
                {
                    "market": json.loads(
                        market_response(TICKER_A)
                    )["market"],
                    "cursor": None,
                }
            ),
            expected_ticker=TICKER_A,
        )
