#!/usr/bin/env python3
"""Finite, public, read-only capture CLI for Alpha Sprint H3."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from h3crypto.coinbase import (
    ALLOWED_SUBSCRIPTION_CHANNELS,
    DEFAULT_SUBSCRIPTION_CHANNELS,
)
from h3crypto.runner import run_capture


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture public Coinbase BTC/ETH WS and exact-series Kalshi public "
            "REST snapshots. This program has no trading or credential path."
        )
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(__file__).resolve().parent / "captures",
        help="parent directory; each invocation creates a new immutable run directory",
    )
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=60.0,
        help="finite capture duration; must be positive",
    )
    parser.add_argument(
        "--sources",
        choices=("all", "coinbase", "kalshi"),
        default="all",
        help="all is the research default; single-source modes are for smoke tests",
    )
    parser.add_argument(
        "--coinbase-channels",
        default=",".join(DEFAULT_SUBSCRIPTION_CHANNELS),
        help=(
            "comma-separated public channels; default is bounded "
            "market_trades,ticker,heartbeats; level2 is explicit opt-in"
        ),
    )
    parser.add_argument(
        "--kalshi-interval-seconds",
        type=float,
        default=5.0,
        help="REST market/orderbook sampling interval, minimum 1 second",
    )
    parser.add_argument(
        "--kalshi-catalog-refresh-seconds",
        type=float,
        default=300.0,
        help="series metadata refresh interval",
    )
    parser.add_argument(
        "--max-data-bytes",
        type=int,
        default=50_000_000_000,
        help="hard cap across raw data streams; receipts remain writable",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    channels = tuple(
        value.strip() for value in args.coinbase_channels.split(",") if value.strip()
    )
    unexpected = sorted(set(channels) - ALLOWED_SUBSCRIPTION_CHANNELS)
    if unexpected:
        raise SystemExit(
            "unsupported --coinbase-channels: " + ",".join(unexpected)
        )
    summary = run_capture(
        output_root=args.output_root,
        duration_seconds=args.duration_seconds,
        sources=args.sources,
        coinbase_channels=channels,
        kalshi_interval_seconds=args.kalshi_interval_seconds,
        kalshi_catalog_refresh_seconds=args.kalshi_catalog_refresh_seconds,
        max_data_bytes=args.max_data_bytes,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 2 if summary["worker_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
