#!/usr/bin/env python3
import importlib.util
import os
import sys


os.environ["Z3_POSTFILL_LEGACY_DIAGNOSTIC"] = (
    ".codex_tmp/z3_postfill_stopping_diagnostic.py"
)
os.environ["Z3_PRICE_BASE_SCRIPT"] = (
    "tmp/crypto_mm_canary_20260726/z3_price_allocation_train.py"
)
os.environ["ROUND3_CAUSAL_CONTRACT"] = (
    "tmp/crypto_mm_canary_20260726/round3/causal_replay_contract.py"
)
spec = importlib.util.spec_from_file_location(
    "receive_clock", ".codex_tmp/z3_postfill_stopping_receive_clock.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class Writer:
    def __init__(self):
        self.rows = []

    def emit(self, row):
        self.rows.append(row)


def market():
    return module.CausalTrackedMarket(
        "SYNTH",
        10_000_000_000,
        "yes",
        [module.P3],
        Writer(),
    )


def book_fields(wall_ns, mono_ns, stable_id):
    return {
        "recv_wall_ns": wall_ns,
        "recv_mono_ns": mono_ns,
        "local_recv_ts_us": wall_ns // 1000,
        "book_stable_id": stable_id,
    }


assert module.C.synthetic_contract_test()["status"] == (
    "CAUSAL_CONTRACT_SYNTHETIC_OK"
)


# Canonical minimal vector: exchange timestamps would put delta first, but the
# receive envelope puts snapshot first and reconstructs 100 -> 0.
events = [
    {
        "kind": "snapshot",
        "exchange": 110,
        "key": module.causal_key(100_000, 10, 0, 2, "snapshot"),
    },
    {
        "kind": "delta",
        "exchange": 99,
        "key": module.causal_key(101_000, 11, 0, 3, "delta"),
    },
]
assert [row["kind"] for row in sorted(events, key=lambda row: row["exchange"])] == [
    "delta",
    "snapshot",
]
assert [row["kind"] for row in sorted(events, key=lambda row: row["key"])] == [
    "snapshot",
    "delta",
]
value = market()
value.on_book_causal(
    100,
    "snapshot",
    None,
    None,
    None,
    [[3000, 100]],
    [[6900, 100]],
    7,
    2,
    book_fields(100_000, 10, "snapshot"),
)
value.on_book_causal(
    101,
    "delta",
    "yes",
    3000,
    -100,
    None,
    None,
    7,
    3,
    book_fields(101_000, 11, "delta"),
)
assert 3000 not in value.books["y"]
assert value.audit["negative_level_failures"] == 0
assert value.reconstructor.audit["zero_level_deletes"] == 1


# One receive envelope is deterministic: every book event precedes trade.
book_key = module.causal_key(500_000, 42, 0, 9, "book")
trade_key = module.causal_key(
    500_000, 42, 1, module.TRADE_SEQ_SENTINEL, "trade"
)
assert book_key < trade_key


# Clock identity and all fail-closed reconstruction guards.
assert module.assert_receive_clock(123_999, 1, 123, "ok") == 123
try:
    module.assert_receive_clock(123_999, 1, 124, "bad")
except RuntimeError as exc:
    assert "mismatch" in str(exc)
else:
    raise AssertionError("clock mismatch did not fail")

unanchored = market()
try:
    unanchored.on_book_causal(
        101,
        "delta",
        "yes",
        3000,
        -1,
        None,
        None,
        7,
        3,
        book_fields(101_000, 11, "delta"),
    )
except RuntimeError as exc:
    assert "delta lacks valid snapshot/sid/sequence anchor" in str(exc)
else:
    raise AssertionError("delta before snapshot did not fail")

negative = market()
negative.on_book_causal(
    100,
    "snapshot",
    None,
    None,
    None,
    [[3000, 100]],
    [[6900, 100]],
    7,
    2,
    book_fields(100_000, 10, "snapshot"),
)
try:
    negative.on_book_causal(
        101,
        "delta",
        "yes",
        3000,
        -101,
        None,
        None,
        7,
        3,
        book_fields(101_000, 11, "delta"),
    )
except RuntimeError as exc:
    assert "negative reconstructed level" in str(exc)
else:
    raise AssertionError("negative reconstructed level did not fail")

sid_change = market()
sid_change.on_book_causal(
    100,
    "snapshot",
    None,
    None,
    None,
    [[3000, 100]],
    [[6900, 100]],
    7,
    2,
    book_fields(100_000, 10, "snapshot"),
)
try:
    sid_change.on_book_causal(
        101,
        "delta",
        "yes",
        3000,
        1,
        None,
        None,
        8,
        3,
        book_fields(101_000, 11, "delta"),
    )
except RuntimeError as exc:
    assert "delta lacks valid snapshot/sid/sequence anchor" in str(exc)
else:
    raise AssertionError("sid change outside snapshot did not fail")

print("RECEIVE_CLOCK_SYNTHETIC_OK")
