"""Strict exact-series Kalshi public REST catalog and market sampler."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from .common import (
    AppendOnlyLedger,
    event_time_summary,
    sha256_bytes,
    utc_iso_from_ns,
)


PUBLIC_REST_BASE = "https://external-api.kalshi.com/trade-api/v2"
PUBLIC_REST_HOST = "external-api.kalshi.com"
MAX_HTTP_BODY_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class SeriesPolicy:
    ticker: str
    title: str
    category: str
    asset_label: str
    market_title: str
    settlement_source_name: str = "CF Benchmarks"
    settlement_source_url: str = "https://www.cfbenchmarks.com/"


SERIES_POLICIES: Mapping[str, SeriesPolicy] = {
    "KXBTC15M": SeriesPolicy(
        ticker="KXBTC15M",
        title="Bitcoin price up down",
        category="Crypto",
        asset_label="BTC",
        market_title="BTC price up in next 15 mins?",
    ),
    "KXETH15M": SeriesPolicy(
        ticker="KXETH15M",
        title="ETH 15M price up down",
        category="Crypto",
        asset_label="ETH",
        market_title="ETH price up in next 15 mins?",
    ),
}


def _source_exact(value: Any, policy: SeriesPolicy) -> bool:
    return value == [
        {
            "name": policy.settlement_source_name,
            "url": policy.settlement_source_url,
        }
    ]


def validate_series(payload: Any, policy: SeriesPolicy) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict) or not isinstance(payload.get("series"), dict):
        return ["SERIES_PAYLOAD_SHAPE"]
    series = payload["series"]
    exact = {
        "ticker": policy.ticker,
        "title": policy.title,
        "category": policy.category,
    }
    for field, expected in exact.items():
        if series.get(field) != expected:
            errors.append(f"SERIES_{field.upper()}_MISMATCH")
    if not _source_exact(series.get("settlement_sources"), policy):
        errors.append("SERIES_SETTLEMENT_SOURCES_MISMATCH")
    if series.get("frequency") != "fifteen_min":
        errors.append("SERIES_FREQUENCY_MISMATCH")
    for field in ("contract_url", "contract_terms_url"):
        value = series.get(field)
        if not isinstance(value, str) or not value.startswith("https://assets.kalshi.com/"):
            errors.append(f"SERIES_{field.upper()}_INVALID")
    return errors


def validate_event(payload: Any, event_ticker: str, policy: SeriesPolicy) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict) or not isinstance(payload.get("event"), dict):
        return ["EVENT_PAYLOAD_SHAPE"]
    event = payload["event"]
    exact = {
        "event_ticker": event_ticker,
        "series_ticker": policy.ticker,
        "category": policy.category,
    }
    for field, expected in exact.items():
        if event.get(field) != expected:
            errors.append(f"EVENT_{field.upper()}_MISMATCH")
    if not _source_exact(event.get("settlement_sources"), policy):
        errors.append("EVENT_SETTLEMENT_SOURCES_MISMATCH")
    title = event.get("title")
    title_pattern = rf"^{re.escape(policy.asset_label)} 15 min · \$[0-9][0-9,]*\.[0-9]{{2}} target$"
    if not isinstance(title, str) or re.fullmatch(title_pattern, title) is None:
        errors.append("EVENT_TITLE_MISMATCH")
    markets = event.get("markets")
    if not isinstance(markets, list):
        errors.append("EVENT_MARKETS_MISSING")
    return errors


def validate_market(
    payload: Any,
    market_ticker: str,
    event_ticker: str,
    policy: SeriesPolicy,
) -> List[str]:
    errors: List[str] = []
    if not isinstance(payload, dict) or not isinstance(payload.get("market"), dict):
        return ["MARKET_PAYLOAD_SHAPE"]
    market = payload["market"]
    exact = {
        "ticker": market_ticker,
        "event_ticker": event_ticker,
        "title": policy.market_title,
        "market_type": "binary",
        "strike_type": "greater_or_equal",
    }
    for field, expected in exact.items():
        if market.get(field) != expected:
            errors.append(f"MARKET_{field.upper()}_MISMATCH")
    for field in ("open_time", "close_time", "updated_time"):
        summary = event_time_summary([market.get(field)])
        if summary["event_time_valid_count"] != 1:
            errors.append(f"MARKET_{field.upper()}_INVALID")
    for field in ("rules_primary", "rules_secondary"):
        if not isinstance(market.get(field), str) or not market[field].strip():
            errors.append(f"MARKET_{field.upper()}_MISSING")
    return errors


def validate_orderbook(payload: Any) -> List[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("orderbook_fp"), dict):
        return ["ORDERBOOK_PAYLOAD_SHAPE"]
    book = payload["orderbook_fp"]
    errors: List[str] = []
    for side in ("yes_dollars", "no_dollars"):
        levels = book.get(side)
        if levels is None:
            continue
        if not isinstance(levels, list):
            errors.append(f"ORDERBOOK_{side.upper()}_SHAPE")
            continue
        for level in levels:
            if (
                not isinstance(level, list)
                or len(level) != 2
                or not all(isinstance(value, str) for value in level)
            ):
                errors.append(f"ORDERBOOK_{side.upper()}_LEVEL_SHAPE")
                break
    return errors


def allowed_public_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    base_ok = (
        parsed.scheme == "https"
        and parsed.hostname == PUBLIC_REST_HOST
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
    )
    if not base_ok:
        return False
    path = parsed.path
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    ticker = r"[A-Z0-9._-]+"
    if re.fullmatch(rf"/trade-api/v2/series/{ticker}", path):
        return not query
    if re.fullmatch(rf"/trade-api/v2/events/{ticker}", path):
        return query in ({}, {"with_nested_markets": ["true"]})
    if path == "/trade-api/v2/markets":
        return (
            set(query) == {"series_ticker", "status", "limit"}
            and query.get("series_ticker", [None])[0] in SERIES_POLICIES
            and query.get("status") == ["open"]
            and query.get("limit") == ["1000"]
        )
    if re.fullmatch(rf"/trade-api/v2/markets/{ticker}", path):
        return not query
    if re.fullmatch(rf"/trade-api/v2/markets/{ticker}/orderbook", path):
        return query == {"depth": ["100"]}
    return False


class PublicGetError(RuntimeError):
    pass


class KalshiCollector:
    def __init__(
        self,
        ledger: AppendOnlyLedger,
        interval_seconds: float = 5.0,
        catalog_refresh_seconds: float = 300.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        if interval_seconds < 1.0:
            raise ValueError("interval_seconds must be >= 1")
        if catalog_refresh_seconds < interval_seconds:
            raise ValueError("catalog_refresh_seconds must be >= interval_seconds")
        self.ledger = ledger
        self.interval_seconds = interval_seconds
        self.catalog_refresh_seconds = catalog_refresh_seconds
        self.timeout_seconds = timeout_seconds
        self._series_valid: Dict[str, bool] = {}
        self._event_members: Dict[Tuple[str, str], set] = {}
        self.metrics: MutableMapping[str, int] = {
            "http_responses": 0,
            "http_errors": 0,
            "mapping_refusals": 0,
            "catalog_cycles": 0,
            "market_cycles": 0,
            "market_snapshots": 0,
            "orderbook_snapshots": 0,
        }

    def _get_json(
        self,
        path: str,
        stream: str,
        endpoint_kind: str,
        binding: Mapping[str, Any],
    ) -> Any:
        url = PUBLIC_REST_BASE + path
        if not allowed_public_url(url):
            raise ValueError(f"refusing non-public Kalshi URL: {url}")
        send_wall_ns = time.time_ns()
        send_mono_ns = time.monotonic_ns()
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "alpha-sprint-h3-readonly/0.1",
            },
        )
        status: Optional[int] = None
        response_headers: Dict[str, Optional[str]] = {}
        body = b""
        error: Optional[BaseException] = None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = response.status
                response_headers = {
                    key: response.headers.get(key)
                    for key in ("Date", "Content-Type", "Content-Length", "ETag")
                }
                body = response.read(MAX_HTTP_BODY_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            response_headers = {
                key: exc.headers.get(key) if exc.headers else None
                for key in ("Date", "Content-Type", "Content-Length", "ETag")
            }
            body = exc.read(MAX_HTTP_BODY_BYTES + 1)
            error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            error = exc
        recv_wall_ns = time.time_ns()
        recv_mono_ns = time.monotonic_ns()
        if len(body) > MAX_HTTP_BODY_BYTES:
            error = PublicGetError("response exceeded 20 MiB capture limit")
            body = body[:MAX_HTTP_BODY_BYTES]
        raw_text: Optional[str]
        parsed: Any = None
        parse_error: Optional[str] = None
        if body:
            try:
                raw_text = body.decode("utf-8", errors="strict")
                parsed = json.loads(raw_text)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raw_text = body.decode("utf-8", errors="replace")
                parse_error = f"{exc.__class__.__name__}:{exc}"
        else:
            raw_text = None
        record = {
            "record_type": "kalshi_public_rest_response",
            "source": "kalshi_public_rest",
            "endpoint_kind": endpoint_kind,
            "request_method": "GET",
            "request_url": url,
            "request_binding": dict(binding),
            "request_send_wall_ns": send_wall_ns,
            "request_send_wall_utc": utc_iso_from_ns(send_wall_ns),
            "request_send_monotonic_ns": send_mono_ns,
            "local_receive_wall_ns": recv_wall_ns,
            "local_receive_wall_utc": utc_iso_from_ns(recv_wall_ns),
            "local_receive_monotonic_ns": recv_mono_ns,
            "round_trip_monotonic_ns": recv_mono_ns - send_mono_ns,
            "http_status": status,
            "response_headers": response_headers,
            "raw_payload_sha256": sha256_bytes(body),
            "raw_json": raw_text,
            "json_parse_error": parse_error,
            "authentication_used": False,
        }
        self.ledger.append(stream, record)
        self.metrics["http_responses"] += 1
        if error is not None or status != 200 or parse_error is not None:
            self.metrics["http_errors"] += 1
            self.ledger.receipt(
                "KALSHI_PUBLIC_GET_FAILED",
                {
                    "source": "kalshi_public_rest",
                    "endpoint_kind": endpoint_kind,
                    "request_url": url,
                    "http_status": status,
                    "raw_payload_sha256": sha256_bytes(body),
                    "error_type": error.__class__.__name__ if error else None,
                    "error": str(error)[:1000] if error else parse_error,
                },
            )
            raise PublicGetError(f"GET failed for {endpoint_kind}: {status} {error}")
        return parsed

    def _refuse(self, scope: str, binding: Mapping[str, Any], errors: Sequence[str]) -> None:
        self.metrics["mapping_refusals"] += 1
        self.ledger.receipt(
            "KALSHI_MAPPING_REFUSED",
            {
                "source": "kalshi_public_rest",
                "mapping_scope": scope,
                "binding": dict(binding),
                "errors": list(errors),
            },
        )

    def refresh_catalog(self) -> None:
        self.metrics["catalog_cycles"] += 1
        for ticker, policy in SERIES_POLICIES.items():
            try:
                payload = self._get_json(
                    f"/series/{ticker}",
                    "kalshi_catalog",
                    "series",
                    {"series_ticker": ticker},
                )
            except PublicGetError:
                self._series_valid[ticker] = False
                continue
            errors = validate_series(payload, policy)
            self._series_valid[ticker] = not errors
            if errors:
                self._refuse("series", {"series_ticker": ticker}, errors)
            else:
                self.ledger.receipt(
                    "KALSHI_SERIES_VALIDATED",
                    {
                        "source": "kalshi_public_rest",
                        "series_ticker": ticker,
                        "category": policy.category,
                        "title": policy.title,
                        "settlement_source": {
                            "name": policy.settlement_source_name,
                            "url": policy.settlement_source_url,
                        },
                    },
                )

    def _validate_event_for_market(
        self,
        ticker: str,
        event_ticker: str,
        market_ticker: str,
        policy: SeriesPolicy,
    ) -> bool:
        key = (ticker, event_ticker)
        cached_members = self._event_members.get(key)
        if cached_members is not None:
            if market_ticker in cached_members:
                return True
            self._refuse(
                "event",
                {
                    "series_ticker": ticker,
                    "event_ticker": event_ticker,
                    "market_ticker": market_ticker,
                },
                ["EVENT_MARKET_MEMBERSHIP_MISMATCH"],
            )
            return False
        try:
            payload = self._get_json(
                (
                    f"/events/{urllib.parse.quote(event_ticker, safe='')}"
                    "?with_nested_markets=true"
                ),
                "kalshi_catalog",
                "event",
                {
                    "series_ticker": ticker,
                    "event_ticker": event_ticker,
                    "discovered_market_ticker": market_ticker,
                },
            )
        except PublicGetError:
            return False
        errors = validate_event(payload, event_ticker, policy)
        event = payload.get("event", {}) if isinstance(payload, dict) else {}
        markets = event.get("markets", []) if isinstance(event, dict) else []
        members = {
            item.get("ticker")
            for item in markets
            if isinstance(item, dict) and isinstance(item.get("ticker"), str)
        }
        if market_ticker not in members:
            errors.append("EVENT_MARKET_MEMBERSHIP_MISMATCH")
        valid = not errors
        if valid:
            # Only successes are cached. A transiently incomplete or drifting
            # response remains blocked but is re-checked on the next poll.
            self._event_members[key] = members
        if errors:
            self._refuse(
                "event",
                {
                    "series_ticker": ticker,
                    "event_ticker": event_ticker,
                    "market_ticker": market_ticker,
                },
                errors,
            )
        else:
            self.ledger.receipt(
                "KALSHI_EVENT_MAPPING_VALIDATED",
                {
                    "source": "kalshi_public_rest",
                    "series_ticker": ticker,
                    "event_ticker": event_ticker,
                    "market_ticker": market_ticker,
                },
            )
        return valid

    def sample_markets(self) -> None:
        self.metrics["market_cycles"] += 1
        for ticker, policy in SERIES_POLICIES.items():
            if not self._series_valid.get(ticker, False):
                self._refuse(
                    "market_discovery",
                    {"series_ticker": ticker},
                    ["SERIES_NOT_CURRENTLY_VALID"],
                )
                continue
            query = urllib.parse.urlencode(
                {"series_ticker": ticker, "status": "open", "limit": 1000}
            )
            try:
                discovery = self._get_json(
                    f"/markets?{query}",
                    "kalshi_catalog",
                    "market_discovery",
                    {"series_ticker": ticker, "status": "open"},
                )
            except PublicGetError:
                continue
            markets = discovery.get("markets") if isinstance(discovery, dict) else None
            if not isinstance(markets, list):
                self._refuse(
                    "market_discovery",
                    {"series_ticker": ticker},
                    ["MARKETS_PAYLOAD_SHAPE"],
                )
                continue
            for discovered in markets:
                if not isinstance(discovered, dict):
                    self._refuse(
                        "market_discovery",
                        {"series_ticker": ticker},
                        ["DISCOVERED_MARKET_NOT_OBJECT"],
                    )
                    continue
                market_ticker = discovered.get("ticker")
                event_ticker = discovered.get("event_ticker")
                if not isinstance(market_ticker, str) or not isinstance(event_ticker, str):
                    self._refuse(
                        "market_discovery",
                        {"series_ticker": ticker},
                        ["DISCOVERED_MARKET_IDENTITY_MISSING"],
                    )
                    continue
                if not self._validate_event_for_market(
                    ticker, event_ticker, market_ticker, policy
                ):
                    continue
                quoted_market = urllib.parse.quote(market_ticker, safe="")
                try:
                    market_payload = self._get_json(
                        f"/markets/{quoted_market}",
                        "kalshi_market",
                        "market_snapshot",
                        {
                            "series_ticker": ticker,
                            "event_ticker": event_ticker,
                            "market_ticker": market_ticker,
                        },
                    )
                except PublicGetError:
                    continue
                errors = validate_market(
                    market_payload, market_ticker, event_ticker, policy
                )
                if errors:
                    self._refuse(
                        "market",
                        {
                            "series_ticker": ticker,
                            "event_ticker": event_ticker,
                            "market_ticker": market_ticker,
                        },
                        errors,
                    )
                    continue
                self.metrics["market_snapshots"] += 1
                try:
                    orderbook_payload = self._get_json(
                        f"/markets/{quoted_market}/orderbook?depth=100",
                        "kalshi_market",
                        "orderbook_snapshot",
                        {
                            "series_ticker": ticker,
                            "event_ticker": event_ticker,
                            "market_ticker": market_ticker,
                            "depth": 100,
                        },
                    )
                except PublicGetError:
                    continue
                book_errors = validate_orderbook(orderbook_payload)
                if book_errors:
                    self._refuse(
                        "orderbook",
                        {
                            "series_ticker": ticker,
                            "event_ticker": event_ticker,
                            "market_ticker": market_ticker,
                        },
                        book_errors,
                    )
                else:
                    self.metrics["orderbook_snapshots"] += 1

    def run(self, stop: threading.Event) -> Mapping[str, int]:
        next_catalog = 0.0
        next_market = 0.0
        while not stop.is_set():
            now = time.monotonic()
            if now >= next_catalog:
                self.refresh_catalog()
                next_catalog = now + self.catalog_refresh_seconds
            if now >= next_market:
                self.sample_markets()
                next_market = now + self.interval_seconds
            wait_for = min(next_catalog, next_market) - time.monotonic()
            stop.wait(max(0.05, min(wait_for, 1.0)))
        self.ledger.receipt(
            "KALSHI_STOPPED",
            {"source": "kalshi_public_rest", "metrics": dict(self.metrics)},
        )
        return dict(self.metrics)
