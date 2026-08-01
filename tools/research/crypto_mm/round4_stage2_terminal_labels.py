#!/usr/bin/env python3
"""Create a raw, auditable terminal-label receipt for ROUND4 Stage 2.

This tool performs only unauthenticated public GET requests for an already
sealed ticker roster.  Every HTTP response body is preserved byte-for-byte.
The fetched market values are post-hoc labels/evaluation evidence: no value
from this capture is authorized as a decision-time feature, a reconstructed
historical lifecycle receipt, a candidate-selection input, or a live input.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple
import urllib.error
import urllib.request


ROSTER_SCHEMA = "round4-stage2-terminal-label-roster-v1"
RECEIPT_SCHEMA = "round4-stage2-terminal-label-receipt-v1"
ROSTER_STATUS = "PRE_DOWNLOAD_ROSTER_SEALED"
COMPLETE_STATUS = "COMPLETE_LABEL_EVIDENCE_ONLY"
INCOMPLETE_STATUS = "INCOMPLETE_FAIL_CLOSED"
LABEL_CONTRACT_VERSION = "ROUND4_STAGE2_TERMINAL_LABEL_EVIDENCE_V1"
REJECTED_CONTEXT_CONTRACT = "ROUND4_POSTFILL_PUBLIC_PROXY_V4_1"
EXPERIMENT_ID = "KXBTC15M-ROUND4-TWO-STAGE-HAZARD-V1"

OFFICIAL_ORIGIN = "https://api.elections.kalshi.com"
MARKET_PATH = "/trade-api/v2/markets/"
USER_AGENT = "round4-stage2-terminal-label-receipt/1"
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MIN_REQUEST_INTERVAL_SECONDS = 0.25
RETRYABLE_STATUSES = frozenset((429, 500, 502, 503, 504))
EXPECTED_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
EXPECTED_SOURCE_SHA256 = {
    "2026-07-20": (
        "ade3558dd5b967c47f7d1cce795f5d963265954fb97148fd9a57e6c9db643d80"
    ),
    "2026-07-21": (
        "d5bf34f3136a1e3c57525ab07e0889678fed68c81841a879c6b09b5a63389966"
    ),
    "2026-07-22": (
        "97c063c7b57d09502e64c831d0cdc6d7d922b5585d5c8b92e28164bdf8d56042"
    ),
}
EXPECTED_LOCAL_CROSSCHECK_SHA256 = (
    "150e23c971b147858cc36e9635820fee2d051b74f366fbfc4ea12426c1e3a033"
)
EXPECTED_PRICE_RANGES = [
    {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
    {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
    {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
]

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TICKER_RE = re.compile(
    r"^KXBTC15M-[0-9]{2}"
    r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"[0-9]{6}-15$"
)
UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)

DEFAULT_ROSTER = (
    Path(__file__).resolve().parents[3]
    / "tmp"
    / "crypto_mm_canary_20260726"
    / "round4"
    / "round4_stage2_terminal_label_roster_20260726.json"
)


class TerminalLabelError(RuntimeError):
    """A roster, source response, or create-once receipt failed closed."""


@dataclass(frozen=True)
class RosterEntry:
    source_date_utc: str
    market_ticker: str


@dataclass(frozen=True)
class HttpResult:
    status: int
    final_url: str
    body: bytes
    retry_after: Optional[str] = None


class Transport(Protocol):
    def __call__(self, url: str, timeout_seconds: int) -> HttpResult:
        ...


class Clock(Protocol):
    def __call__(self) -> Tuple[int, int]:
        """Return wall-clock and monotonic nanoseconds."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_from_ns(value: int) -> str:
    return (
        datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _parse_utc(label: str, value: object) -> Tuple[str, datetime]:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        raise TerminalLabelError(f"{label} must be an exact UTC timestamp")
    base = value[:-1]
    if "." in base:
        whole, fraction = base.split(".", 1)
        normalized = whole + "." + (fraction + "000000")[:6] + "+00:00"
    else:
        normalized = base + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TerminalLabelError(f"{label} is not a valid timestamp") from exc
    return value, parsed


def _require_sha(label: str, value: object) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise TerminalLabelError(f"{label} must be lowercase SHA256")
    return value


def _flatten_markets(value: Mapping[str, object]) -> List[RosterEntry]:
    dates = value.get("allowed_source_dates_utc")
    if dates != list(EXPECTED_DATES):
        raise TerminalLabelError("roster allowed dates drifted")
    grouped = value.get("markets_by_source_date_utc")
    if not isinstance(grouped, Mapping) or set(grouped) != set(EXPECTED_DATES):
        raise TerminalLabelError("roster date partitions are not exact")
    entries: List[RosterEntry] = []
    for source_date in EXPECTED_DATES:
        tickers = grouped[source_date]
        if not isinstance(tickers, list) or len(tickers) != 24:
            raise TerminalLabelError(
                f"{source_date}: roster must contain exactly 24 markets"
            )
        if tickers != sorted(tickers):
            raise TerminalLabelError(
                f"{source_date}: roster markets must be sorted"
            )
        for ticker in tickers:
            if not isinstance(ticker, str) or TICKER_RE.fullmatch(ticker) is None:
                raise TerminalLabelError(f"invalid Stage-2 ticker {ticker!r}")
            entries.append(RosterEntry(source_date, ticker))
    if len(entries) != 72 or len({row.market_ticker for row in entries}) != 72:
        raise TerminalLabelError("roster must contain 72 unique tickers")
    return entries


def validate_roster(value: object) -> List[RosterEntry]:
    if not isinstance(value, Mapping):
        raise TerminalLabelError("roster must be an object")
    if value.get("schema") != ROSTER_SCHEMA:
        raise TerminalLabelError("roster schema drifted")
    if value.get("status") != ROSTER_STATUS:
        raise TerminalLabelError("roster was not sealed before download")
    if value.get("experiment_id") != EXPERIMENT_ID:
        raise TerminalLabelError("roster experiment id drifted")
    if value.get("terminal_label_contract_version") != LABEL_CONTRACT_VERSION:
        raise TerminalLabelError("terminal-label contract drifted")
    context = value.get("audited_contract_context")
    if (
        not isinstance(context, Mapping)
        or context.get("contract") != REJECTED_CONTEXT_CONTRACT
        or context.get("status") != "REJECTED_BY_INDEPENDENT_AUDIT"
        or context.get("context_only") is not True
        or context.get("commit_or_extraction_binding_authorized") is not False
        or context.get("future_contract_requires_explicit_new_binding")
        is not True
    ):
        raise TerminalLabelError("rejected-contract firewall drifted")

    authority = value.get("selection_authority")
    if not isinstance(authority, Mapping):
        raise TerminalLabelError("missing roster selection authority")
    if (
        authority.get("kind")
        != "EXACT_DISTINCT_MARKETS_IN_PREEXISTING_L2_FACT_PARTITIONS"
        or authority.get("terminal_or_outcome_field_used") is not False
        or authority.get("constructed_before_terminal_api_download") is not True
    ):
        raise TerminalLabelError("roster selection is not pre-terminal")
    sources = authority.get("source_fact_partitions")
    if not isinstance(sources, list) or len(sources) != 3:
        raise TerminalLabelError("roster must pin three L2 fact partitions")
    seen_sources: Dict[str, str] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            raise TerminalLabelError("source fact receipt must be an object")
        source_date = source.get("source_date_utc")
        source_hash = _require_sha("source fact hash", source.get("sha256"))
        if not isinstance(source_date, str) or source_date in seen_sources:
            raise TerminalLabelError("source fact dates are invalid/duplicated")
        if source_hash != EXPECTED_SOURCE_SHA256.get(source_date):
            raise TerminalLabelError("source fact partition hash drifted")
        path = source.get("path")
        if not isinstance(path, str) or f"date={source_date}/" not in path:
            raise TerminalLabelError("source fact path/date binding failed")
        seen_sources[source_date] = source_hash
    if set(seen_sources) != set(EXPECTED_DATES):
        raise TerminalLabelError("source fact date coverage is incomplete")

    crosscheck = authority.get("independent_local_crosscheck")
    if (
        not isinstance(crosscheck, Mapping)
        or crosscheck.get("sha256") != EXPECTED_LOCAL_CROSSCHECK_SHA256
        or crosscheck.get("exact_roster_match") is not True
    ):
        raise TerminalLabelError("independent roster crosscheck is unpinned")

    policy = value.get("terminal_field_policy")
    if (
        not isinstance(policy, Mapping)
        or policy.get("data_role") != "LABEL_AND_EVALUATION_ONLY"
        or policy.get(
            "all_posthoc_get_values_forbidden_from_decision_features"
        )
        is not True
        or policy.get("settlement_ts_is_not_historical_receipt_time")
        is not True
        or policy.get(
            "join_may_occur_only_after_decision_and_action_spines_are_sealed"
        )
        is not True
        or policy.get("candidate_selection_authorized") is not False
        or policy.get("live_authorized") is not False
    ):
        raise TerminalLabelError("terminal label-only boundary drifted")

    entries = _flatten_markets(value)
    expected_count = value.get("market_count")
    counts = value.get("market_count_by_source_date_utc")
    if expected_count != 72 or counts != {day: 24 for day in EXPECTED_DATES}:
        raise TerminalLabelError("declared roster counts drifted")
    canonical_entries = [
        {
            "source_date_utc": entry.source_date_utc,
            "market_ticker": entry.market_ticker,
        }
        for entry in entries
    ]
    expected_hash = _sha256(canonical_json_bytes(canonical_entries))
    if value.get("roster_markets_sha256") != expected_hash:
        raise TerminalLabelError("roster market-list hash mismatch")
    return entries


def load_roster(path: Path) -> Tuple[Mapping[str, object], List[RosterEntry], str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalLabelError("roster is not valid UTF-8 JSON") from exc
    return value, validate_roster(value), _sha256(raw)


def parse_terminal_market_response(
    raw: bytes, *, expected_ticker: str
) -> Dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalLabelError("market response is not UTF-8 JSON") from exc
    if not isinstance(value, Mapping) or set(value) != {"market"}:
        raise TerminalLabelError("market response envelope schema drifted")
    market = value["market"]
    if not isinstance(market, Mapping):
        raise TerminalLabelError("market response market must be an object")
    required = {
        "ticker",
        "status",
        "result",
        "settlement_value_dollars",
        "settlement_ts",
        "price_level_structure",
        "price_ranges",
        "open_time",
        "close_time",
    }
    missing = sorted(required - set(market))
    if missing:
        raise TerminalLabelError(f"market response missing fields: {missing}")
    if market["ticker"] != expected_ticker:
        raise TerminalLabelError("market response ticker mismatch")
    if market["status"] != "finalized":
        raise TerminalLabelError("market response is not finalized")
    result = market["result"]
    if result not in ("yes", "no"):
        raise TerminalLabelError("finalized result must be yes/no")
    expected_payout = "1.0000" if result == "yes" else "0.0000"
    if market["settlement_value_dollars"] != expected_payout:
        raise TerminalLabelError("result and settlement payout disagree")
    if market["price_level_structure"] != "tapered_deci_cent":
        raise TerminalLabelError("price level structure drifted")
    if market["price_ranges"] != EXPECTED_PRICE_RANGES:
        raise TerminalLabelError("price_ranges drifted")
    open_text, open_dt = _parse_utc("market.open_time", market["open_time"])
    close_text, close_dt = _parse_utc(
        "market.close_time", market["close_time"]
    )
    settlement_text, settlement_dt = _parse_utc(
        "market.settlement_ts", market["settlement_ts"]
    )
    if not open_dt < close_dt <= settlement_dt:
        raise TerminalLabelError(
            "open/close/settlement timestamp order is invalid"
        )
    return {
        "market_ticker": expected_ticker,
        "market_status": "FINALIZED",
        "official_result": result.upper(),
        "yes_settlement_value_dollars": expected_payout,
        "open_time": open_text,
        "close_time": close_text,
        "settlement_ts": settlement_text,
        "price_level_structure": market["price_level_structure"],
        "price_ranges": market["price_ranges"],
    }


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Mapping[str, str],
        newurl: str,
    ) -> None:
        del request, fp, code, msg, headers, newurl
        return None


def _read_bounded(response: Any) -> bytes:
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise TerminalLabelError("public response exceeds byte limit")
        chunks.append(chunk)


def _retry_after(headers: Any) -> Optional[str]:
    if headers is None:
        return None
    values = headers.get_all("Retry-After", [])
    if len(values) > 1:
        raise TerminalLabelError("multiple Retry-After headers")
    return values[0] if values else None


def urllib_transport(url: str, timeout_seconds: int) -> HttpResult:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RejectRedirect()
    )
    try:
        response = opener.open(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as exc:
        try:
            body = _read_bounded(exc)
            return HttpResult(
                int(exc.code), exc.geturl(), body, _retry_after(exc.headers)
            )
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TerminalLabelError(
            "public GET failed without an HTTP response"
        ) from exc
    try:
        return HttpResult(
            int(response.getcode()),
            response.geturl(),
            _read_bounded(response),
            _retry_after(response.headers),
        )
    finally:
        response.close()


def system_clock() -> Tuple[int, int]:
    return time.time_ns(), time.monotonic_ns()


def _market_url(ticker: str) -> str:
    if TICKER_RE.fullmatch(ticker) is None:
        raise TerminalLabelError("unsafe market ticker in request URL")
    return OFFICIAL_ORIGIN + MARKET_PATH + ticker


def _write_create_once(path: Path, payload: bytes) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(path), flags, 0o400)
    except OSError as exc:
        raise TerminalLabelError(f"output is not create-once: {path}") from exc
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise TerminalLabelError("short artifact write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return _sha256(payload)


def _prepare_output_dir(path: Path) -> Path:
    path = path.resolve()
    if path.exists() or path.is_symlink():
        raise TerminalLabelError("output directory is create-once")
    if not path.parent.exists() or not path.parent.is_dir():
        raise TerminalLabelError("output parent must already exist")
    path.mkdir(mode=0o700)
    raw = path / "raw"
    raw.mkdir(mode=0o700)
    if stat.S_ISLNK(path.lstat().st_mode) or stat.S_ISLNK(raw.lstat().st_mode):
        raise TerminalLabelError("output directories may not be symlinks")
    return path


def _retry_delay(result: HttpResult, attempt_number: int) -> float:
    if result.status == 429 and result.retry_after is not None:
        if not result.retry_after.isdigit():
            raise TerminalLabelError("Retry-After must be integer seconds")
        return float(min(30, max(1, int(result.retry_after))))
    return float(min(4, 2 ** (attempt_number - 1)))


def capture_terminal_labels(
    roster_path: Path,
    output_dir: Path,
    *,
    transport: Transport = urllib_transport,
    clock: Clock = system_clock,
    sleeper: Callable[[float], None] = time.sleep,
    max_attempts: int = 3,
) -> Tuple[Mapping[str, object], Path, Path]:
    if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 5:
        raise TerminalLabelError("max_attempts must be in [1,5]")
    roster, entries, roster_raw_sha256 = load_roster(roster_path)
    output_dir = _prepare_output_dir(output_dir)
    raw_dir = output_dir / "raw"
    capture_start_wall, capture_start_mono = clock()
    previous_after_mono: Optional[int] = None
    all_attempts: List[Dict[str, object]] = []
    markets: List[Dict[str, object]] = []
    errors: List[Dict[str, object]] = []

    for market_index, entry in enumerate(entries):
        url = _market_url(entry.market_ticker)
        attempts: List[Dict[str, object]] = []
        parsed: Optional[Dict[str, object]] = None
        pending_retry_delay = 0.0
        for attempt_number in range(1, max_attempts + 1):
            required_delay = max(
                MIN_REQUEST_INTERVAL_SECONDS, pending_retry_delay
            )
            if previous_after_mono is not None:
                now_wall, now_mono = clock()
                del now_wall
                elapsed = (now_mono - previous_after_mono) / 1_000_000_000
                if elapsed < required_delay:
                    sleeper(required_delay - elapsed)
            before_wall, before_mono = clock()
            transport_error: Optional[str] = None
            try:
                response = transport(url, TIMEOUT_SECONDS)
            except TerminalLabelError as exc:
                response = None
                transport_error = str(exc)
            after_wall, after_mono = clock()
            if after_wall < before_wall or after_mono < before_mono:
                raise TerminalLabelError("request clocks regressed")
            if (
                previous_after_mono is not None
                and after_mono < previous_after_mono
            ):
                raise TerminalLabelError("batch monotonic clock regressed")
            previous_after_mono = after_mono

            attempt: Dict[str, object] = {
                "attempt_number": attempt_number,
                "request_url": url,
                "request_started_wall_ns": before_wall,
                "request_started_at_utc": _utc_from_ns(before_wall),
                "request_finished_wall_ns": after_wall,
                "request_finished_at_utc": _utc_from_ns(after_wall),
                "request_started_mono_ns": before_mono,
                "request_finished_mono_ns": after_mono,
                "http_status": None,
                "raw_response_path": None,
                "raw_response_bytes": None,
                "raw_response_sha256": None,
                "transport_error": transport_error,
            }
            if response is not None:
                if response.final_url != url:
                    raise TerminalLabelError("public response URL changed")
                raw_name = (
                    f"{market_index:04d}.{attempt_number:02d}."
                    "market.response.json"
                )
                raw_relative = f"raw/{raw_name}"
                raw_hash = _write_create_once(
                    raw_dir / raw_name, response.body
                )
                attempt.update(
                    {
                        "http_status": response.status,
                        "raw_response_path": raw_relative,
                        "raw_response_bytes": len(response.body),
                        "raw_response_sha256": raw_hash,
                        "retry_after": response.retry_after,
                    }
                )
            attempts.append(attempt)
            all_attempts.append(
                {
                    "source_date_utc": entry.source_date_utc,
                    "market_ticker": entry.market_ticker,
                    **attempt,
                }
            )

            if response is not None and response.status == 200:
                try:
                    parsed = parse_terminal_market_response(
                        response.body, expected_ticker=entry.market_ticker
                    )
                    if (
                        str(parsed["close_time"])[:10]
                        != entry.source_date_utc
                    ):
                        raise TerminalLabelError(
                            "terminal close date differs from sealed "
                            "source-date roster"
                        )
                except TerminalLabelError as exc:
                    errors.append(
                        {
                            "source_date_utc": entry.source_date_utc,
                            "market_ticker": entry.market_ticker,
                            "error": str(exc),
                            "attempt_number": attempt_number,
                        }
                    )
                break
            retryable = (
                response is None or response.status in RETRYABLE_STATUSES
            )
            if not retryable or attempt_number == max_attempts:
                errors.append(
                    {
                        "source_date_utc": entry.source_date_utc,
                        "market_ticker": entry.market_ticker,
                        "error": transport_error
                        or f"HTTP {response.status if response else 'NONE'}",
                        "attempt_number": attempt_number,
                    }
                )
                break
            pending_retry_delay = (
                1.0
                if response is None
                else _retry_delay(response, attempt_number)
            )

        market_record: Dict[str, object] = {
            "source_date_utc": entry.source_date_utc,
            "market_ticker": entry.market_ticker,
            "attempts": attempts,
            "selected_attempt_number": (
                attempts[-1]["attempt_number"] if parsed is not None else None
            ),
            "label": parsed,
        }
        markets.append(market_record)

    capture_finish_wall, capture_finish_mono = clock()
    if (
        capture_finish_wall < capture_start_wall
        or capture_finish_mono < capture_start_mono
    ):
        raise TerminalLabelError("capture batch clocks regressed")
    labels = [
        market["label"]
        for market in markets
        if isinstance(market.get("label"), Mapping)
    ]
    result_counts = {
        "YES": sum(label["official_result"] == "YES" for label in labels),
        "NO": sum(label["official_result"] == "NO" for label in labels),
    }
    close_times = sorted(str(label["close_time"]) for label in labels)
    settlement_times = sorted(str(label["settlement_ts"]) for label in labels)
    complete = (
        not errors
        and len(markets) == 72
        and len(labels) == 72
        and result_counts["YES"] + result_counts["NO"] == 72
    )
    raw_content_leaves = [
        {
            "source_date_utc": attempt["source_date_utc"],
            "market_ticker": attempt["market_ticker"],
            "attempt_number": attempt["attempt_number"],
            "http_status": attempt["http_status"],
            "raw_response_bytes": attempt["raw_response_bytes"],
            "raw_response_sha256": attempt["raw_response_sha256"],
        }
        for attempt in all_attempts
        if attempt["raw_response_sha256"] is not None
    ]
    selected_label_leaves = [
        {
            "source_date_utc": market["source_date_utc"],
            "market_ticker": market["market_ticker"],
            "selected_raw_response_sha256": market["attempts"][-1][
                "raw_response_sha256"
            ],
            "label": market["label"],
        }
        for market in markets
        if isinstance(market.get("label"), Mapping)
    ]
    policy = dict(roster["terminal_field_policy"])  # type: ignore[index]
    receipt: Dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "status": COMPLETE_STATUS if complete else INCOMPLETE_STATUS,
        "complete": complete,
        "experiment_id": EXPERIMENT_ID,
        "terminal_label_contract_version": LABEL_CONTRACT_VERSION,
        "audited_contract_context": {
            "contract": REJECTED_CONTEXT_CONTRACT,
            "status": "REJECTED_BY_INDEPENDENT_AUDIT",
            "context_only": True,
            "commit_or_extraction_binding_authorized": False,
            "future_contract_requires_explicit_new_binding": True,
        },
        "data_role": "LABEL_AND_EVALUATION_ONLY",
        "capture_asof_semantics": "OBSERVED_AT_FETCH_NOT_HISTORICAL_AS_OF",
        "terminal_field_policy": policy,
        "explicit_feature_firewall": {
            "all_values_from_this_capture_forbidden_from_decision_features": True,
            "result_is_label_only": True,
            "settlement_ts_is_label_metadata_not_historical_receipt_time": True,
            "close_time_is_not_sourced_from_this_capture_for_decisions": True,
            "price_ranges_are_not_sourced_from_this_capture_for_decisions": True,
            "join_keys_only": ["source_date_utc", "market_ticker"],
            "join_after_decision_and_action_spines_sealed": True,
        },
        "candidate_selection_authorized": False,
        "shadow_authorized": False,
        "live_authorized": False,
        "request_method": "GET",
        "official_origin": OFFICIAL_ORIGIN,
        "authentication_used": False,
        "roster_path": str(roster_path),
        "roster_raw_sha256": roster_raw_sha256,
        "roster_receipt_sha256": roster_raw_sha256,
        "roster_markets_sha256": roster["roster_markets_sha256"],
        "all_raw_body_content_root_sha256": _sha256(
            canonical_json_bytes(raw_content_leaves)
        ),
        "selected_label_content_root_sha256": _sha256(
            canonical_json_bytes(selected_label_leaves)
        ),
        "capture_started_wall_ns": capture_start_wall,
        "capture_started_at_utc": _utc_from_ns(capture_start_wall),
        "capture_finished_wall_ns": capture_finish_wall,
        "capture_finished_at_utc": _utc_from_ns(capture_finish_wall),
        "capture_started_mono_ns": capture_start_mono,
        "capture_finished_mono_ns": capture_finish_mono,
        "market_count_expected": 72,
        "market_count_captured": len(labels),
        "raw_response_count": sum(
            attempt["raw_response_path"] is not None
            for attempt in all_attempts
        ),
        "result_counts": result_counts,
        "close_time_min": close_times[0] if close_times else None,
        "close_time_max": close_times[-1] if close_times else None,
        "settlement_ts_min": (
            settlement_times[0] if settlement_times else None
        ),
        "settlement_ts_max": (
            settlement_times[-1] if settlement_times else None
        ),
        "price_level_structures": sorted(
            {str(label["price_level_structure"]) for label in labels}
        ),
        "price_ranges_variants": sorted(
            {
                canonical_json_bytes(label["price_ranges"]).decode("utf-8")
                for label in labels
            }
        ),
        "errors": errors,
        "markets": markets,
        "tool_code_sha256": _sha256(Path(__file__).read_bytes()),
    }
    receipt_path = output_dir / "terminal_label_receipt.json"
    receipt_raw = canonical_json_bytes(receipt) + b"\n"
    receipt_sha256 = _write_create_once(receipt_path, receipt_raw)
    manifest_lines: List[str] = [
        f"{receipt_sha256}  {receipt_path.name}\n"
    ]
    for raw_path in sorted(raw_dir.iterdir()):
        payload = raw_path.read_bytes()
        manifest_lines.append(
            f"{_sha256(payload)}  raw/{raw_path.name}\n"
        )
    sums_path = output_dir / "SHA256SUMS"
    _write_create_once(sums_path, "".join(manifest_lines).encode("ascii"))
    return receipt, receipt_path, sums_path


def verify_terminal_label_capture(
    receipt_path: Path, sums_path: Optional[Path] = None
) -> Mapping[str, object]:
    """Re-read a capture from disk and independently recompute its roots."""

    receipt_path = receipt_path.resolve()
    capture_dir = receipt_path.parent
    receipt_raw = receipt_path.read_bytes()
    try:
        receipt = json.loads(receipt_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalLabelError("receipt is not valid UTF-8 JSON") from exc
    if not isinstance(receipt, Mapping):
        raise TerminalLabelError("receipt must be an object")
    if receipt.get("schema") != RECEIPT_SCHEMA:
        raise TerminalLabelError("receipt schema drifted")
    if receipt.get("terminal_label_contract_version") != LABEL_CONTRACT_VERSION:
        raise TerminalLabelError("receipt label contract drifted")
    if (
        receipt.get("data_role") != "LABEL_AND_EVALUATION_ONLY"
        or receipt.get("candidate_selection_authorized") is not False
        or receipt.get("shadow_authorized") is not False
        or receipt.get("live_authorized") is not False
    ):
        raise TerminalLabelError("receipt label-only authorization drifted")
    context = receipt.get("audited_contract_context")
    if (
        not isinstance(context, Mapping)
        or context.get("contract") != REJECTED_CONTEXT_CONTRACT
        or context.get("status") != "REJECTED_BY_INDEPENDENT_AUDIT"
        or context.get("commit_or_extraction_binding_authorized") is not False
        or context.get("future_contract_requires_explicit_new_binding")
        is not True
    ):
        raise TerminalLabelError("receipt rejected-contract firewall drifted")
    firewall = receipt.get("explicit_feature_firewall")
    if (
        not isinstance(firewall, Mapping)
        or firewall.get(
            "all_values_from_this_capture_forbidden_from_decision_features"
        )
        is not True
        or firewall.get("result_is_label_only") is not True
        or firewall.get("join_after_decision_and_action_spines_sealed")
        is not True
    ):
        raise TerminalLabelError("receipt feature firewall drifted")

    roster_path_value = receipt.get("roster_path")
    if not isinstance(roster_path_value, str):
        raise TerminalLabelError("receipt lacks roster path")
    roster_path = Path(roster_path_value)
    if not roster_path.is_absolute():
        raise TerminalLabelError("receipt roster path must be absolute")
    roster, roster_entries, roster_hash = load_roster(roster_path)
    if (
        receipt.get("roster_raw_sha256") != roster_hash
        or receipt.get("roster_receipt_sha256") != roster_hash
        or receipt.get("roster_markets_sha256")
        != roster["roster_markets_sha256"]
    ):
        raise TerminalLabelError("receipt roster binding mismatch")

    markets = receipt.get("markets")
    if not isinstance(markets, list) or len(markets) != 72:
        raise TerminalLabelError("receipt market roster is incomplete")
    receipt_entries: List[RosterEntry] = []
    raw_leaves: List[Dict[str, object]] = []
    selected_leaves: List[Dict[str, object]] = []
    raw_paths: List[str] = []
    labels: List[Mapping[str, object]] = []
    for market_index, market in enumerate(markets):
        if not isinstance(market, Mapping):
            raise TerminalLabelError("receipt market row must be an object")
        source_date = market.get("source_date_utc")
        ticker = market.get("market_ticker")
        if not isinstance(source_date, str) or not isinstance(ticker, str):
            raise TerminalLabelError("receipt market identity is invalid")
        receipt_entries.append(RosterEntry(source_date, ticker))
        attempts = market.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            raise TerminalLabelError("receipt market has no HTTP attempt")
        for attempt_index, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, Mapping):
                raise TerminalLabelError("HTTP attempt must be an object")
            if attempt.get("attempt_number") != attempt_index:
                raise TerminalLabelError("HTTP attempt numbering is not exact")
            started_wall = attempt.get("request_started_wall_ns")
            finished_wall = attempt.get("request_finished_wall_ns")
            started_mono = attempt.get("request_started_mono_ns")
            finished_mono = attempt.get("request_finished_mono_ns")
            if (
                isinstance(started_wall, bool)
                or not isinstance(started_wall, int)
                or isinstance(finished_wall, bool)
                or not isinstance(finished_wall, int)
                or isinstance(started_mono, bool)
                or not isinstance(started_mono, int)
                or isinstance(finished_mono, bool)
                or not isinstance(finished_mono, int)
                or finished_wall < started_wall
                or finished_mono < started_mono
            ):
                raise TerminalLabelError("HTTP attempt clocks are invalid")
            raw_relative = attempt.get("raw_response_path")
            raw_hash = attempt.get("raw_response_sha256")
            raw_size = attempt.get("raw_response_bytes")
            if raw_relative is None:
                if raw_hash is not None or raw_size is not None:
                    raise TerminalLabelError("no-response attempt has raw data")
                continue
            if (
                not isinstance(raw_relative, str)
                or not re.fullmatch(
                    r"raw/[0-9]{4}\.[0-9]{2}\.market\.response\.json",
                    raw_relative,
                )
                or raw_relative
                != (
                    f"raw/{market_index:04d}.{attempt_index:02d}."
                    "market.response.json"
                )
            ):
                raise TerminalLabelError("raw response path is not canonical")
            raw_path = capture_dir / raw_relative
            if raw_path.is_symlink() or not raw_path.is_file():
                raise TerminalLabelError("raw response is missing or symlinked")
            raw = raw_path.read_bytes()
            if (
                _require_sha("raw response hash", raw_hash) != _sha256(raw)
                or raw_size != len(raw)
            ):
                raise TerminalLabelError("raw response hash/size mismatch")
            raw_paths.append(raw_relative)
            raw_leaves.append(
                {
                    "source_date_utc": source_date,
                    "market_ticker": ticker,
                    "attempt_number": attempt_index,
                    "http_status": attempt.get("http_status"),
                    "raw_response_bytes": raw_size,
                    "raw_response_sha256": raw_hash,
                }
            )
        label = market.get("label")
        selected_number = market.get("selected_attempt_number")
        if label is None:
            if selected_number is not None:
                raise TerminalLabelError(
                    "unlabeled market has selected HTTP attempt"
                )
            continue
        if (
            not isinstance(label, Mapping)
            or isinstance(selected_number, bool)
            or not isinstance(selected_number, int)
            or not 1 <= selected_number <= len(attempts)
        ):
            raise TerminalLabelError("selected label attempt is invalid")
        selected = attempts[selected_number - 1]
        selected_relative = selected.get("raw_response_path")
        if not isinstance(selected_relative, str):
            raise TerminalLabelError("selected label has no raw response")
        selected_raw = (capture_dir / selected_relative).read_bytes()
        reparsed = parse_terminal_market_response(
            selected_raw, expected_ticker=ticker
        )
        if dict(label) != reparsed:
            raise TerminalLabelError("stored label differs from raw response")
        if str(label["close_time"])[:10] != source_date:
            raise TerminalLabelError(
                "terminal close date differs from sealed source-date roster"
            )
        labels.append(label)
        selected_leaves.append(
            {
                "source_date_utc": source_date,
                "market_ticker": ticker,
                "selected_raw_response_sha256": selected[
                    "raw_response_sha256"
                ],
                "label": label,
            }
        )
    if receipt_entries != roster_entries:
        raise TerminalLabelError("receipt markets differ from sealed roster")
    raw_root = _sha256(canonical_json_bytes(raw_leaves))
    selected_root = _sha256(canonical_json_bytes(selected_leaves))
    if receipt.get("all_raw_body_content_root_sha256") != raw_root:
        raise TerminalLabelError("all-raw content root mismatch")
    if receipt.get("selected_label_content_root_sha256") != selected_root:
        raise TerminalLabelError("selected-label content root mismatch")
    if receipt.get("raw_response_count") != len(raw_leaves):
        raise TerminalLabelError("raw response count mismatch")
    if receipt.get("market_count_captured") != len(labels):
        raise TerminalLabelError("captured market count mismatch")
    result_counts = {
        "YES": sum(label["official_result"] == "YES" for label in labels),
        "NO": sum(label["official_result"] == "NO" for label in labels),
    }
    if receipt.get("result_counts") != result_counts:
        raise TerminalLabelError("result counts mismatch")
    declared_complete = receipt.get("complete")
    recomputed_complete = (
        len(labels) == 72
        and not receipt.get("errors")
        and result_counts["YES"] + result_counts["NO"] == 72
    )
    if (
        declared_complete is not recomputed_complete
        or receipt.get("status")
        != (COMPLETE_STATUS if recomputed_complete else INCOMPLETE_STATUS)
    ):
        raise TerminalLabelError("receipt completion status mismatch")

    if sums_path is None:
        sums_path = capture_dir / "SHA256SUMS"
    else:
        sums_path = sums_path.resolve()
    lines = sums_path.read_text(encoding="ascii").splitlines()
    manifest: Dict[str, str] = {}
    for line in lines:
        matched = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_./-]+)", line)
        if matched is None or matched.group(2) in manifest:
            raise TerminalLabelError("SHA256SUMS syntax/uniqueness failed")
        manifest[matched.group(2)] = matched.group(1)
    expected_paths = {"terminal_label_receipt.json", *raw_paths}
    if set(manifest) != expected_paths:
        raise TerminalLabelError("SHA256SUMS inventory mismatch")
    if manifest["terminal_label_receipt.json"] != _sha256(receipt_raw):
        raise TerminalLabelError("receipt manifest hash mismatch")
    for raw_relative in raw_paths:
        if manifest[raw_relative] != _sha256(
            (capture_dir / raw_relative).read_bytes()
        ):
            raise TerminalLabelError("raw manifest hash mismatch")
    return {
        "status": "VERIFIED",
        "complete": recomputed_complete,
        "receipt_sha256": _sha256(receipt_raw),
        "roster_receipt_sha256": roster_hash,
        "market_count": len(labels),
        "raw_response_count": len(raw_leaves),
        "result_counts": result_counts,
        "all_raw_body_content_root_sha256": raw_root,
        "selected_label_content_root_sha256": selected_root,
        "sha256sums_sha256": _sha256(sums_path.read_bytes()),
        "v4_1_commit_authorized": False,
        "future_contract_binding_authorized": False,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output-dir", type=Path)
    mode.add_argument("--verify-receipt", type=Path)
    parser.add_argument("--sha256sums", type=Path)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args(argv)
    if args.verify_receipt is not None:
        try:
            verification = verify_terminal_label_capture(
                args.verify_receipt, args.sha256sums
            )
        except TerminalLabelError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(verification, sort_keys=True))
        return 0
    try:
        receipt, receipt_path, sums_path = capture_terminal_labels(
            args.roster.resolve(),
            args.output_dir,
            max_attempts=args.max_attempts,
        )
    except TerminalLabelError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": receipt["status"],
                "complete": receipt["complete"],
                "market_count_captured": receipt["market_count_captured"],
                "result_counts": receipt["result_counts"],
                "receipt_path": str(receipt_path),
                "receipt_sha256": _sha256(receipt_path.read_bytes()),
                "sha256sums_path": str(sums_path),
            },
            sort_keys=True,
        )
    )
    return 0 if receipt["complete"] is True else 3


if __name__ == "__main__":
    raise SystemExit(main())
