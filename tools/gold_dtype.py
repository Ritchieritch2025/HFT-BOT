"""GoldRecord numpy mirror (PLAN_GOLD_DATA_CONTRACT §2.1) — W1.

Byte-identical to include/trading/gold_record.hpp; parity is enforced by
tests/test_gold_dtype.py against the committed canonical layout JSON
(tests/fixtures/gold_layout.json, written by ./build/test_gold_layout --dump).
Little-endian, 512 bytes, zero-copy mmap-able via np.memmap(..., dtype=GOLD_DTYPE).

Also carries the Python half of FNV-1a-64 for trade_id_hash (R decision
2026-07-06: no new dependencies; V8 collision reconciliation is mandatory).
stdlib + numpy only.
"""
import numpy as np

DEPTH = 16
GOLD_LAYOUT_VERSION = 1

EVENT_TYPE = {"BOOK_SNAPSHOT": 1, "BOOK_DELTA": 2, "TRADE": 3,
              "L1_TICKER": 4, "HEARTBEAT": 5}
FLAGS = {"F_BOOK_VALID": 1 << 0, "F_BOOK_COVERED": 1 << 1, "F_CROSSED": 1 << 2,
         "F_FROM_SNAPSHOT": 1 << 3, "F_TRADE_RACED": 1 << 4}

# Explicit offsets + itemsize — never inferred packing. Pads and the reserved
# tail are intentionally absent (dead bytes); offsets mirror the C++ struct.
GOLD_DTYPE = np.dtype({
    "names": [
        "ts_us", "stream_seq", "market_id", "event_type", "flags",
        "book_seq",
        "trade_yes_price_e4", "taker_side", "trade_qty_e4", "trade_id_hash",
        "bid_px_e4", "ask_px_e4", "bid_qty_e4", "ask_qty_e4",
        "bid_rest_qty_e4", "ask_rest_qty_e4", "bid_nlevels", "ask_nlevels",
    ],
    "formats": [
        "<u8", "<u8", "<u4", "<u2", "<u2",
        "<u8",
        "<i4", "u1", "<i8", "<u8",
        ("<i4", (DEPTH,)), ("<i4", (DEPTH,)), ("<i8", (DEPTH,)), ("<i8", (DEPTH,)),
        "<i8", "<i8", "<u2", "<u2",
    ],
    "offsets": [
        0, 8, 16, 20, 22,
        24,
        32, 36, 40, 48,
        56, 120, 184, 312,
        440, 448, 456, 458,
    ],
    "itemsize": 512,
})

_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK = 0xFFFFFFFFFFFFFFFF


def fnv1a64(s):
    """FNV-1a-64 over bytes/str — bit-identical to trading::fnv1a64."""
    if isinstance(s, str):
        s = s.encode("utf-8")
    h = _FNV_OFFSET
    for b in s:
        h = ((h ^ b) * _FNV_PRIME) & _MASK
    return h


def layout():
    """{field: {offset, size}} for parity checks."""
    return {name: {"offset": GOLD_DTYPE.fields[name][1],
                   "size": GOLD_DTYPE.fields[name][0].itemsize}
            for name in GOLD_DTYPE.names}
