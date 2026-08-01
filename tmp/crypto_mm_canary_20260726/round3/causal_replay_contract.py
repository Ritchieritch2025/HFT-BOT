#!/usr/bin/env python3
"""Shared causal receive-clock and L2 reconstruction contract for ROUND3.

This module is deliberately strategy-free.  Consumers use the same merge key,
clock identity check, raw L2 quality gate, and snapshot/signed-delta state
machine.  Corrupt state raises ``CausalContractError``; quantities are never
clamped or silently repaired.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ALLOWED_DATES = ("2026-07-20", "2026-07-21", "2026-07-22")
FORBIDDEN_DATE = "2026-07-23"
TRADE_SEQ_SENTINEL = (1 << 63) - 1
BOOK_PRIORITY = 0
TRADE_PRIORITY = 1

GAP_TOOL_PATH = "/home/ubuntu/hft-bot/tools/l2_gap_check.py"
GAP_TOOL_SHA256 = (
    "8a65b4764e1d70eaf653c27cc9a253ed626bc6cf9bb2dd6f431545907ae29de9"
)
INGEST_PATH = "/home/ubuntu/hft-bot/tools/ingest.py"
INGEST_SHA256 = (
    "7a6be65a20f9e6240e53396afcaa81047df69d736e59054c196b9297c2e5cc8e"
)
GAP_RECEIPT_PATH = (
    "/home/ubuntu/hft-bot/work/event_packs/l2_gaps_{date}.json"
)
GAP_RECEIPT_SHA256 = {
    "2026-07-20": (
        "93fa2cbaae0e600cfca0a8537c8dfa3c1d9bbd26498809000b6b55dd1c401eab"
    ),
    "2026-07-21": (
        "222e1e9b83d82da247c3315f4eebf8809027a2437f48b6ba3eb42cbe494a710f"
    ),
    "2026-07-22": (
        "87815f64e8085f045d779abd3378edb58e1cdec77df5a9128d29e1a82e492a6f"
    ),
}
REQUIRED_ZERO_RECEIPT_FIELDS = (
    "sids_with_seq_gaps",
    "seq_gap_events",
    "seq_missed_total",
    "seq_regressions",
    "stream_restarts",
    "markers_lost_frames",
    "parse_errors",
)


class CausalContractError(RuntimeError):
    """An input cannot participate in a causal fail-closed replay."""


def sha256_path(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def causal_key(
    recv_wall_ns,
    recv_mono_ns,
    channel_priority,
    ws_seq_or_sentinel,
    stable_id,
):
    """Canonical total order; BOOK=0 precedes TRADE=1 in one envelope."""
    return (
        int(recv_wall_ns),
        int(recv_mono_ns),
        int(channel_priority),
        int(ws_seq_or_sentinel),
        str(stable_id),
    )


def book_key(recv_wall_ns, recv_mono_ns, ws_seq, stable_id=""):
    return causal_key(
        recv_wall_ns,
        recv_mono_ns,
        BOOK_PRIORITY,
        ws_seq,
        stable_id,
    )


def trade_key(recv_wall_ns, recv_mono_ns, trade_id):
    return causal_key(
        recv_wall_ns,
        recv_mono_ns,
        TRADE_PRIORITY,
        TRADE_SEQ_SENTINEL,
        trade_id,
    )


def assert_receive_clock(
    recv_wall_ns,
    recv_mono_ns,
    local_recv_ts_us,
    label,
):
    if (
        recv_wall_ns is None
        or recv_mono_ns is None
        or local_recv_ts_us is None
    ):
        raise CausalContractError(f"{label}: missing receive clock")
    expected = int(recv_wall_ns) // 1_000
    if int(local_recv_ts_us) != expected:
        raise CausalContractError(
            f"{label}: local_recv_ts_us mismatch "
            f"got={local_recv_ts_us} expected={expected}"
        )
    return expected


def assert_allowed_date_path(path, date):
    if date not in ALLOWED_DATES:
        raise CausalContractError(f"date outside discovery allowlist: {date}")
    text = str(path)
    if FORBIDDEN_DATE in text:
        raise CausalContractError(f"forbidden date path resolved: {text}")
    if f"date={date}" not in text and f"_{date}." not in text:
        raise CausalContractError(
            f"path not positively bound to {date}: {text}"
        )


def verify_pinned_contract_tools():
    actual_ingest = sha256_path(INGEST_PATH)
    actual_gap = sha256_path(GAP_TOOL_PATH)
    if actual_ingest != INGEST_SHA256:
        raise CausalContractError(
            f"ingest contract SHA mismatch: {actual_ingest}"
        )
    if actual_gap != GAP_TOOL_SHA256:
        raise CausalContractError(
            f"L2 gap tool SHA mismatch: {actual_gap}"
        )
    ingest_lines = Path(INGEST_PATH).read_text().splitlines()
    required_fragments = (
        "legacy-compat only",
        "never a tradable replay clock",
        "local_recv_ts_us recv_wall_ns // 1000",
    )
    joined = "\n".join(ingest_lines)
    missing = [text for text in required_fragments if text not in joined]
    if missing:
        raise CausalContractError(
            f"ingest causal-clock contract text missing: {missing}"
        )
    evidence = []
    for line_no, line in enumerate(ingest_lines, 1):
        if any(fragment in line for fragment in required_fragments):
            evidence.append({"line": line_no, "text": line.strip()})
    return {
        "ingest_path": INGEST_PATH,
        "ingest_sha256": actual_ingest,
        "gap_tool_path": GAP_TOOL_PATH,
        "gap_tool_sha256": actual_gap,
        "ingest_contract_lines": evidence,
    }


def load_gap_receipt(date):
    if date not in ALLOWED_DATES:
        raise CausalContractError(f"receipt date not allowed: {date}")
    path = GAP_RECEIPT_PATH.format(date=date)
    assert_allowed_date_path(path, date)
    actual_sha = sha256_path(path)
    expected_sha = GAP_RECEIPT_SHA256[date]
    if actual_sha != expected_sha:
        raise CausalContractError(
            f"{date}: L2 gap receipt SHA mismatch {actual_sha}"
        )
    receipt = json.loads(Path(path).read_text())
    if receipt.get("schema_version") != "l2-gap-receipt-v1":
        raise CausalContractError(f"{date}: L2 receipt schema mismatch")
    if receipt.get("date") != date:
        raise CausalContractError(f"{date}: L2 receipt date mismatch")
    if receipt.get("no_l2_files") is not False:
        raise CausalContractError(f"{date}: no_l2_files gate failed")
    failures = {
        field: receipt.get(field)
        for field in REQUIRED_ZERO_RECEIPT_FIELDS
        if receipt.get(field) != 0
    }
    marker_keys = set((receipt.get("recorder_markers") or {}).keys())
    unexpected = marker_keys - {"transport_close"}
    if unexpected:
        failures["unexpected_recorder_markers"] = sorted(unexpected)
    if failures:
        raise CausalContractError(
            f"{date}: L2 receipt gate failed {failures}"
        )
    return {
        "path": path,
        "sha256": actual_sha,
        "schema_version": receipt["schema_version"],
        "date": date,
        "required_zero": {
            field: receipt[field]
            for field in REQUIRED_ZERO_RECEIPT_FIELDS
        },
        "recorder_markers": receipt.get("recorder_markers") or {},
        "snapshot_re_anchors_total": receipt.get(
            "snapshot_re_anchors_total"
        ),
        "sids_total": receipt.get("sids_total"),
        "files": receipt.get("files") or [],
    }


class BookReconstructor:
    """Per-market snapshot + signed-delta FSM with sequence/source guards."""

    def __init__(self, market):
        self.market = str(market)
        self.books = {"yes": {}, "no": {}}
        self.anchored = False
        self.sid = None
        self.seq = None
        self.last_key = None
        self.audit = {
            "book_events": 0,
            "snapshots": 0,
            "snapshot_reanchors": 0,
            "stream_resets": 0,
            "deltas": 0,
            "zero_level_deletes": 0,
        }

    @staticmethod
    def _plain_int(value, label):
        if type(value) is not int:
            raise CausalContractError(f"{label}: expected integer")
        return value

    def _event_key(
        self,
        recv_wall_ns,
        recv_mono_ns,
        local_recv_ts_us,
        ws_seq,
        stable_id,
    ):
        assert_receive_clock(
            recv_wall_ns,
            recv_mono_ns,
            local_recv_ts_us,
            f"{self.market}/book",
        )
        key = book_key(
            recv_wall_ns,
            recv_mono_ns,
            ws_seq,
            stable_id,
        )
        if self.last_key is not None and key <= self.last_key:
            raise CausalContractError(
                f"{self.market}: non-increasing receive book key"
            )
        self.last_key = key
        self.audit["book_events"] += 1
        return key

    def snapshot(
        self,
        yes_levels,
        no_levels,
        ws_sid,
        ws_seq,
        recv_wall_ns,
        recv_mono_ns,
        local_recv_ts_us,
        stable_id="snapshot",
    ):
        sid = self._plain_int(ws_sid, f"{self.market}/snapshot sid")
        seq = self._plain_int(ws_seq, f"{self.market}/snapshot seq")
        if sid < 0 or seq < 0:
            raise CausalContractError(
                f"{self.market}: negative snapshot sid/seq"
            )
        self._event_key(
            recv_wall_ns,
            recv_mono_ns,
            local_recv_ts_us,
            seq,
            stable_id,
        )
        rebuilt = {}
        for side, levels in (("yes", yes_levels), ("no", no_levels)):
            if not isinstance(levels, list):
                raise CausalContractError(
                    f"{self.market}: malformed {side} snapshot"
                )
            target = {}
            for level in levels:
                if not isinstance(level, (list, tuple)) or len(level) != 2:
                    raise CausalContractError(
                        f"{self.market}: malformed {side} snapshot level"
                    )
                price = self._plain_int(
                    level[0], f"{self.market}/{side} snapshot price"
                )
                quantity = self._plain_int(
                    level[1], f"{self.market}/{side} snapshot quantity"
                )
                if not 0 < price < 10_000 or quantity <= 0:
                    raise CausalContractError(
                        f"{self.market}: invalid {side} snapshot "
                        f"price/quantity {price}/{quantity}"
                    )
                if price in target:
                    raise CausalContractError(
                        f"{self.market}: duplicate {side} snapshot price {price}"
                    )
                target[price] = quantity
            rebuilt[side] = target
        if self.anchored:
            self.audit["snapshot_reanchors"] += 1
            if sid != self.sid or seq <= self.seq:
                self.audit["stream_resets"] += 1
        self.books = rebuilt
        self.anchored = True
        self.sid = sid
        self.seq = seq
        self.audit["snapshots"] += 1

    def delta(
        self,
        side,
        price_e4,
        delta_e4,
        ws_sid,
        ws_seq,
        recv_wall_ns,
        recv_mono_ns,
        local_recv_ts_us,
        stable_id="delta",
    ):
        sid = self._plain_int(ws_sid, f"{self.market}/delta sid")
        seq = self._plain_int(ws_seq, f"{self.market}/delta seq")
        self._event_key(
            recv_wall_ns,
            recv_mono_ns,
            local_recv_ts_us,
            seq,
            stable_id,
        )
        if (
            not self.anchored
            or sid != self.sid
            or self.seq is None
            or seq <= self.seq
        ):
            raise CausalContractError(
                f"{self.market}: delta lacks valid snapshot/sid/sequence anchor"
            )
        if side not in ("yes", "no"):
            raise CausalContractError(
                f"{self.market}: invalid delta side {side!r}"
            )
        price = self._plain_int(
            price_e4, f"{self.market}/{side} delta price"
        )
        change = self._plain_int(
            delta_e4, f"{self.market}/{side} delta quantity"
        )
        if not 0 < price < 10_000 or change == 0:
            raise CausalContractError(
                f"{self.market}: invalid delta price/change {price}/{change}"
            )
        book = self.books[side]
        before = book.get(price, 0)
        after = before + change
        if after < 0:
            raise CausalContractError(
                f"{self.market}: negative reconstructed level side={side} "
                f"price={price} before={before} delta={change} after={after}"
            )
        if after == 0:
            book.pop(price, None)
            self.audit["zero_level_deletes"] += 1
        else:
            book[price] = after
        self.seq = seq
        self.audit["deltas"] += 1


def synthetic_contract_test():
    """Canonical exchange-vs-receive and hard-failure vectors."""
    events = [
        {
            "kind": "snapshot",
            "exchange": 110,
            "key": book_key(100_000, 10, 2, "snapshot"),
        },
        {
            "kind": "delta",
            "exchange": 99,
            "key": book_key(101_000, 11, 3, "delta"),
        },
    ]
    assert [
        row["kind"] for row in sorted(events, key=lambda row: row["exchange"])
    ] == ["delta", "snapshot"]
    assert [
        row["kind"] for row in sorted(events, key=lambda row: row["key"])
    ] == ["snapshot", "delta"]
    value = BookReconstructor("SYNTH")
    value.snapshot(
        [[3_000, 100]],
        [[6_900, 100]],
        7,
        2,
        100_000,
        10,
        100,
    )
    value.delta(
        "yes",
        3_000,
        -100,
        7,
        3,
        101_000,
        11,
        101,
    )
    assert 3_000 not in value.books["yes"]
    assert book_key(500_000, 42, 9) < trade_key(
        500_000, 42, "trade"
    )
    assert assert_receive_clock(123_999, 1, 123, "ok") == 123
    expected_failures = 0
    for action in ("clock", "unanchored", "negative", "sid"):
        try:
            if action == "clock":
                assert_receive_clock(123_999, 1, 124, "bad")
            elif action == "unanchored":
                BookReconstructor("U").delta(
                    "yes", 3_000, -1, 7, 3, 101_000, 11, 101
                )
            else:
                candidate = BookReconstructor(action)
                candidate.snapshot(
                    [[3_000, 100]],
                    [[6_900, 100]],
                    7,
                    2,
                    100_000,
                    10,
                    100,
                )
                candidate.delta(
                    "yes",
                    3_000,
                    -101 if action == "negative" else 1,
                    7 if action == "negative" else 8,
                    3,
                    101_000,
                    11,
                    101,
                )
        except CausalContractError:
            expected_failures += 1
    assert expected_failures == 4
    return {
        "status": "CAUSAL_CONTRACT_SYNTHETIC_OK",
        "expected_failures_observed": expected_failures,
        "same_envelope_order": "BOOK<TRADE",
        "zero_delete": value.audit["zero_level_deletes"],
    }
