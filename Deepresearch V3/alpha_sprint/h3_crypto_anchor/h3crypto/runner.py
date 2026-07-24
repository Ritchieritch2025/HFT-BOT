"""Finite-duration coordinator for the two public read-only collectors."""

from __future__ import annotations

import platform
import signal
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping

from .coinbase import (
    CoinbaseCollector,
    DEFAULT_SUBSCRIPTION_CHANNELS,
    PRODUCTS,
    PUBLIC_WS_URL,
)
from .common import AppendOnlyLedger
from .kalshi import KalshiCollector, PUBLIC_REST_BASE, SERIES_POLICIES


def run_capture(
    output_root: Path,
    duration_seconds: float,
    sources: str = "all",
    coinbase_channels: tuple = DEFAULT_SUBSCRIPTION_CHANNELS,
    kalshi_interval_seconds: float = 5.0,
    kalshi_catalog_refresh_seconds: float = 300.0,
    max_data_bytes: int = 50_000_000_000,
) -> Dict[str, Any]:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be > 0")
    if sources not in {"all", "coinbase", "kalshi"}:
        raise ValueError("sources must be all, coinbase, or kalshi")
    stop = threading.Event()
    worker_results: Dict[str, Mapping[str, int]] = {}
    worker_errors: Dict[str, str] = {}
    threads = []

    run_start_mono_ns = time.monotonic_ns()
    with AppendOnlyLedger(
        Path(output_root), max_data_bytes=max_data_bytes
    ) as ledger:
        ledger.receipt(
            "RUN_STARTED",
            {
                "sources": sources,
                "duration_seconds": duration_seconds,
                "coinbase_endpoint": PUBLIC_WS_URL,
                "coinbase_products": list(PRODUCTS),
                "coinbase_channels": list(coinbase_channels),
                "kalshi_endpoint": PUBLIC_REST_BASE,
                "kalshi_series_tickers": sorted(SERIES_POLICIES),
                "authentication_used": False,
                "financial_actions": False,
                "http_methods_allowed": ["GET"],
                "max_data_bytes": max_data_bytes,
                "python": platform.python_version(),
            },
        )

        def launch(name: str, collector: Any) -> None:
            def target() -> None:
                try:
                    worker_results[name] = collector.run(stop)
                except BaseException as exc:
                    worker_errors[name] = f"{exc.__class__.__name__}:{exc}"
                    ledger.receipt(
                        "WORKER_FATAL",
                        {
                            "worker": name,
                            "error_type": exc.__class__.__name__,
                            "error": str(exc)[:1000],
                        },
                    )
                    stop.set()

            thread = threading.Thread(
                target=target, name=f"h3-{name}", daemon=False
            )
            thread.start()
            threads.append(thread)

        if sources in {"all", "coinbase"}:
            launch("coinbase", CoinbaseCollector(ledger, channels=coinbase_channels))
        if sources in {"all", "kalshi"}:
            launch(
                "kalshi",
                KalshiCollector(
                    ledger,
                    interval_seconds=kalshi_interval_seconds,
                    catalog_refresh_seconds=kalshi_catalog_refresh_seconds,
                ),
            )

        old_handlers: Dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda _s, _f: stop.set())
        try:
            deadline = time.monotonic() + duration_seconds
            while not stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stop.set()
                    break
                stop.wait(min(0.25, remaining))
            for thread in threads:
                thread.join(timeout=15.0)
            stuck = [thread.name for thread in threads if thread.is_alive()]
            if stuck:
                worker_errors["coordinator"] = "workers did not stop: " + ",".join(stuck)
        finally:
            stop.set()
            for signum, old_handler in old_handlers.items():
                signal.signal(signum, old_handler)

        elapsed_seconds = max(
            (time.monotonic_ns() - run_start_mono_ns) / 1_000_000_000, 1e-9
        )
        byte_counts = dict(ledger.bytes_written)
        data_bytes = sum(
            value for name, value in byte_counts.items() if name != "receipts"
        )
        summary = {
            "run_id": ledger.run_id,
            "run_dir": str(ledger.run_dir),
            "elapsed_seconds": elapsed_seconds,
            "worker_results": dict(worker_results),
            "worker_errors": dict(worker_errors),
            "record_counts_before_final_receipt": dict(ledger.counts),
            "bytes_before_final_receipt": byte_counts,
            "data_bytes_before_final_receipt": data_bytes,
            "measured_data_bytes_per_second": data_bytes / elapsed_seconds,
            "projected_24h_data_bytes_at_measured_rate": int(
                data_bytes / elapsed_seconds * 86_400
            ),
            "max_data_bytes": ledger.max_data_bytes,
        }
        ledger.receipt("RUN_STOPPED", summary)
        summary["record_counts"] = dict(ledger.counts)
        summary["bytes_written"] = dict(ledger.bytes_written)
        return summary
