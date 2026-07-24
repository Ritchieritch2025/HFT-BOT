"""Coinbase Advanced Trade public WebSocket recorder."""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Set

from .common import (
    AppendOnlyLedger,
    CaptureSizeLimit,
    SequenceTracker,
    event_time_summary,
    sha256_bytes,
    utc_iso_from_ns,
)


PUBLIC_WS_URL = "wss://advanced-trade-ws.coinbase.com"
PRODUCTS = ("BTC-USD", "ETH-USD")
ALLOWED_SUBSCRIPTION_CHANNELS = {
    "market_trades",
    "ticker",
    "level2",
    "heartbeats",
}
DEFAULT_SUBSCRIPTION_CHANNELS = ("market_trades", "ticker", "heartbeats")
DATA_CHANNELS = {"market_trades", "l2_data", "ticker"}
KNOWN_CHANNELS = DATA_CHANNELS | {"heartbeats", "subscriptions", "status"}


def _products_from_message(payload: Mapping[str, Any]) -> List[str]:
    products: Set[str] = set()
    events = payload.get("events")
    if not isinstance(events, list):
        return []
    for event in events:
        if not isinstance(event, dict):
            continue
        product = event.get("product_id")
        if isinstance(product, str):
            products.add(product)
        trades = event.get("trades")
        if isinstance(trades, list):
            for trade in trades:
                if isinstance(trade, dict) and isinstance(trade.get("product_id"), str):
                    products.add(trade["product_id"])
        tickers = event.get("tickers")
        if isinstance(tickers, list):
            for ticker in tickers:
                if isinstance(ticker, dict) and isinstance(ticker.get("product_id"), str):
                    products.add(ticker["product_id"])
    return sorted(products)


def _event_times_from_message(payload: Mapping[str, Any]) -> List[Any]:
    values: List[Any] = [payload.get("timestamp")]
    events = payload.get("events")
    if not isinstance(events, list):
        return values
    for event in events:
        if not isinstance(event, dict):
            continue
        trades = event.get("trades")
        if isinstance(trades, list):
            values.extend(
                trade.get("time")
                for trade in trades
                if isinstance(trade, dict)
            )
        updates = event.get("updates")
        if isinstance(updates, list):
            values.extend(
                update.get("event_time")
                for update in updates
                if isinstance(update, dict)
            )
    return values


class CoinbaseMessageValidator:
    def __init__(self) -> None:
        self._l2_snapshot_seen: Set[str] = set()

    def validate(self, payload: Any) -> Dict[str, Any]:
        errors: List[str] = []
        if not isinstance(payload, dict):
            return {
                "valid": False,
                "errors": ["PAYLOAD_NOT_OBJECT"],
                "products": [],
                "event_times": event_time_summary([]),
                "l2_snapshot_seen": {},
            }
        channel = payload.get("channel")
        if not isinstance(channel, str):
            errors.append("CHANNEL_MISSING_OR_NOT_STRING")
        elif channel not in KNOWN_CHANNELS:
            errors.append("UNKNOWN_CHANNEL")
        products = _products_from_message(payload)
        unexpected = sorted(set(products) - set(PRODUCTS))
        if unexpected:
            errors.append("UNEXPECTED_PRODUCT:" + ",".join(unexpected))
        sequence = payload.get("sequence_num")
        if channel in DATA_CHANNELS and (
            isinstance(sequence, bool) or not isinstance(sequence, int)
        ):
            errors.append("DATA_SEQUENCE_MISSING_OR_NOT_INTEGER")
        times = event_time_summary(_event_times_from_message(payload))
        if channel in DATA_CHANNELS and times["event_time_count"] == 0:
            errors.append("DATA_EVENT_TIME_MISSING")
        if times["event_time_invalid_count"]:
            errors.append("INVALID_EVENT_TIME")

        if channel == "l2_data":
            events = payload.get("events")
            if not isinstance(events, list) or not events:
                errors.append("L2_EVENTS_MISSING")
            else:
                for event in events:
                    if not isinstance(event, dict):
                        errors.append("L2_EVENT_NOT_OBJECT")
                        continue
                    kind = event.get("type")
                    product = event.get("product_id")
                    if kind not in {"snapshot", "update"}:
                        errors.append("L2_EVENT_TYPE_INVALID")
                    if product not in PRODUCTS:
                        errors.append("L2_PRODUCT_INVALID")
                    if kind == "snapshot" and product in PRODUCTS:
                        self._l2_snapshot_seen.add(product)
                    if kind == "update" and product in PRODUCTS and product not in self._l2_snapshot_seen:
                        errors.append(f"L2_UPDATE_BEFORE_SNAPSHOT:{product}")
                    updates = event.get("updates")
                    if not isinstance(updates, list):
                        errors.append("L2_UPDATES_MISSING")
                    else:
                        for update in updates:
                            if not isinstance(update, dict):
                                errors.append("L2_UPDATE_NOT_OBJECT")
                                break
                            required = {"side", "event_time", "price_level", "new_quantity"}
                            if not required.issubset(update):
                                errors.append("L2_UPDATE_FIELDS_MISSING")
                                break
        return {
            "valid": not errors,
            "errors": sorted(set(errors)),
            "products": products,
            "event_times": times,
            "l2_snapshot_seen": {
                product: product in self._l2_snapshot_seen for product in PRODUCTS
            },
        }


class CoinbaseCollector:
    def __init__(
        self,
        ledger: AppendOnlyLedger,
        channels: Iterable[str] = DEFAULT_SUBSCRIPTION_CHANNELS,
    ) -> None:
        selected = tuple(channels)
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("Coinbase channels must be non-empty and unique")
        unexpected = sorted(set(selected) - ALLOWED_SUBSCRIPTION_CHANNELS)
        if unexpected:
            raise ValueError("unsupported Coinbase channels: " + ",".join(unexpected))
        if "market_trades" not in selected:
            raise ValueError("market_trades is mandatory for H3")
        if not ({"ticker", "level2"} & set(selected)):
            raise ValueError("either ticker or level2 is mandatory for H3")
        self.ledger = ledger
        self.channels = selected
        self.sequence_tracker = SequenceTracker()
        self.metrics: MutableMapping[str, int] = {
            "connections": 0,
            "messages": 0,
            "parse_errors": 0,
            "sequence_gaps": 0,
            "out_of_order": 0,
            "reconnects": 0,
        }

    @staticmethod
    def _load_websocket_module() -> Any:
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "websocket-client==1.8.0 is required; install requirements.txt"
            ) from exc
        return websocket

    def _record_raw(
        self,
        raw_text: str,
        connection_id: str,
        recv_wall_ns: int,
        recv_mono_ns: int,
        validator: CoinbaseMessageValidator,
    ) -> None:
        raw_bytes = raw_text.encode("utf-8", errors="strict")
        try:
            payload = json.loads(raw_text)
            parse_error: Optional[str] = None
        except json.JSONDecodeError as exc:
            payload = None
            parse_error = f"{exc.__class__.__name__}:{exc.msg}"
            self.metrics["parse_errors"] += 1
        validation = validator.validate(payload)
        products = validation["products"]
        channel = payload.get("channel") if isinstance(payload, dict) else None
        sequence_num = (
            payload.get("sequence_num") if isinstance(payload, dict) else None
        )
        checks: List[Dict[str, Any]] = []
        if (
            isinstance(channel, str)
            and isinstance(sequence_num, int)
            and not isinstance(sequence_num, bool)
        ):
            # The live Advanced Trade endpoint emits one monotone sequence over
            # the multiplexed connection (including control/heartbeat frames).
            # Track that observed wire stream, while retaining channel/products
            # on the envelope for downstream per-product checks.
            checks = self.sequence_tracker.observe(
                connection_id,
                "__CONNECTION_STREAM__",
                ["__ALL_ENVELOPES__"],
                sequence_num,
            )
        for check in checks:
            if check["status"] == "GAP":
                self.metrics["sequence_gaps"] += 1
                self.ledger.receipt(
                    "COINBASE_SEQUENCE_GAP",
                    {
                        "source": "coinbase_public_ws",
                        "gap": check,
                        "raw_payload_sha256": sha256_bytes(raw_bytes),
                    },
                )
            elif check["status"] == "OUT_OF_ORDER":
                self.metrics["out_of_order"] += 1
                self.ledger.receipt(
                    "COINBASE_SEQUENCE_OUT_OF_ORDER",
                    {
                        "source": "coinbase_public_ws",
                        "sequence": check,
                        "raw_payload_sha256": sha256_bytes(raw_bytes),
                    },
                )
        record = {
            "record_type": "coinbase_ws_envelope",
            "source": "coinbase_public_ws",
            "endpoint": PUBLIC_WS_URL,
            "connection_id": connection_id,
            "local_receive_wall_ns": recv_wall_ns,
            "local_receive_wall_utc": utc_iso_from_ns(recv_wall_ns),
            "local_receive_monotonic_ns": recv_mono_ns,
            "channel": channel,
            "sequence_num": sequence_num,
            "products": products,
            "source_envelope_time": (
                payload.get("timestamp") if isinstance(payload, dict) else None
            ),
            "event_time_summary": validation["event_times"],
            "sequence_checks": checks,
            "validation": {
                "valid": validation["valid"] and parse_error is None,
                "errors": validation["errors"],
                "json_parse_error": parse_error,
                "l2_snapshot_seen": validation["l2_snapshot_seen"],
            },
            "raw_payload_sha256": sha256_bytes(raw_bytes),
            "raw_json": raw_text,
        }
        self.ledger.append("coinbase", record)
        self.metrics["messages"] += 1

    def run(self, stop: threading.Event) -> Mapping[str, int]:
        websocket = self._load_websocket_module()
        attempt = 0
        had_connection = False
        while not stop.is_set():
            connection_id = "cb-" + uuid.uuid4().hex
            validator = CoinbaseMessageValidator()
            ws = None
            try:
                ws = websocket.create_connection(
                    PUBLIC_WS_URL,
                    timeout=10,
                    suppress_origin=True,
                    enable_multithread=False,
                )
                ws.settimeout(1.0)
                self.metrics["connections"] += 1
                if had_connection:
                    self.metrics["reconnects"] += 1
                had_connection = True
                attempt = 0
                self.ledger.receipt(
                    "COINBASE_CONNECTED",
                    {
                        "source": "coinbase_public_ws",
                        "connection_id": connection_id,
                        "endpoint": PUBLIC_WS_URL,
                        "reconnect_uncertainty": self.metrics["connections"] > 1,
                    },
                )
                subscriptions = []
                for channel in self.channels:
                    message: Dict[str, Any] = {
                        "type": "subscribe",
                        "channel": channel,
                    }
                    if channel != "heartbeats":
                        message["product_ids"] = list(PRODUCTS)
                    ws.send(json.dumps(message, separators=(",", ":")))
                    subscriptions.append(message)
                self.ledger.receipt(
                    "COINBASE_SUBSCRIBED",
                    {
                        "source": "coinbase_public_ws",
                        "connection_id": connection_id,
                        "subscriptions": subscriptions,
                        "authentication_used": False,
                    },
                )
                while not stop.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if raw is None:
                        raise ConnectionError("Coinbase WebSocket returned EOF")
                    recv_wall_ns = time.time_ns()
                    recv_mono_ns = time.monotonic_ns()
                    if isinstance(raw, bytes):
                        raw_text = raw.decode("utf-8", errors="strict")
                    elif isinstance(raw, str):
                        raw_text = raw
                    else:
                        raise TypeError(f"unsupported WebSocket frame: {type(raw)!r}")
                    self._record_raw(
                        raw_text,
                        connection_id,
                        recv_wall_ns,
                        recv_mono_ns,
                        validator,
                    )
            except CaptureSizeLimit:
                self.ledger.receipt(
                    "CAPTURE_DATA_BYTE_CAP_REACHED",
                    {
                        "source": "coinbase_public_ws",
                        "connection_id": connection_id,
                        "max_data_bytes": self.ledger.max_data_bytes,
                    },
                )
                raise
            except Exception as exc:
                if stop.is_set():
                    break
                attempt += 1
                self.ledger.receipt(
                    "COINBASE_DISCONNECTED",
                    {
                        "source": "coinbase_public_ws",
                        "connection_id": connection_id,
                        "error_type": exc.__class__.__name__,
                        "error": str(exc)[:1000],
                        "will_retry": True,
                        "reconnect_uncertainty": had_connection,
                    },
                )
                delay = min(30.0, 0.5 * (2 ** min(attempt - 1, 6)))
                delay += random.uniform(0.0, min(0.5, delay / 4))
                stop.wait(delay)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        self.ledger.receipt(
            "COINBASE_STOPPED",
            {"source": "coinbase_public_ws", "metrics": dict(self.metrics)},
        )
        return dict(self.metrics)
