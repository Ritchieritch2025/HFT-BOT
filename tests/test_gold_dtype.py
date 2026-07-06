"""W1: Python side of the GoldRecord layout parity contract (V11 half).

Asserts that tools/gold_dtype.py's numpy structured dtype has byte-identical
offsets/sizes to the committed canonical layout JSON written by the C++ test
(tests/fixtures/gold_layout.json), that everything is little-endian, and —
per audit G2 — that every fixture file under tests/fixtures/ is actually
git-tracked (the global *.ndjson/*.parquet/*.csv.gz ignores must never
silently swallow a fixture again).
"""
import json
import os
import subprocess

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANONICAL = os.path.join(ROOT, "tests", "fixtures", "gold_layout.json")


def _layout():
    with open(CANONICAL) as f:
        return json.load(f)


def test_canonical_layout_committed():
    assert os.path.exists(CANONICAL), (
        "tests/fixtures/gold_layout.json missing — build and run "
        "./build/test_gold_layout --dump, then commit the file")


def test_dtype_matches_canonical_layout():
    from tools.gold_dtype import GOLD_DTYPE
    lay = _layout()
    assert GOLD_DTYPE.itemsize == lay["size"] == 512
    fields = lay["fields"]
    for name, want in fields.items():
        assert name in GOLD_DTYPE.names, "dtype missing field %s" % name
        dt, off = GOLD_DTYPE.fields[name][:2]
        assert off == want["offset"], (
            "%s offset: dtype=%d canonical=%d" % (name, off, want["offset"]))
        assert dt.itemsize == want["size"], (
            "%s size: dtype=%d canonical=%d" % (name, dt.itemsize, want["size"]))
    # both directions: dtype must not carry extra data fields either
    extra = set(GOLD_DTYPE.names) - set(fields)
    assert not extra, "dtype has fields absent from canonical layout: %s" % extra


def test_dtype_is_little_endian():
    # numpy canonicalizes explicit "<" to native "=" on little-endian
    # platforms, so the correct equivalent-strength assertion is: the
    # platform IS little-endian, and no field carries big-endian order.
    import sys
    assert sys.byteorder == "little", "gold format is little-endian only"
    from tools.gold_dtype import GOLD_DTYPE
    for name in GOLD_DTYPE.names:
        base = GOLD_DTYPE.fields[name][0].base
        assert base.byteorder in ("<", "|", "="), (
            "%s is big-endian: %r" % (name, base.byteorder))


def test_depth_constant_matches():
    from tools.gold_dtype import DEPTH
    assert DEPTH == _layout()["depth"] == 16


def test_record_roundtrip_through_bytes():
    from tools.gold_dtype import GOLD_DTYPE
    rec = np.zeros(1, dtype=GOLD_DTYPE)
    rec["ts_us"] = 1_783_300_000_000_000
    rec["market_id"] = 42
    rec["bid_px_e4"][0][0] = 90        # $0.0090 — sub-penny survives (D5)
    rec["trade_qty_e4"] = 51_190_000   # 5119.00 contracts fractional-exact
    back = np.frombuffer(rec.tobytes(), dtype=GOLD_DTYPE)
    assert back["ts_us"][0] == 1_783_300_000_000_000
    assert back["market_id"][0] == 42
    assert back["bid_px_e4"][0][0] == 90
    assert back["trade_qty_e4"][0] == 51_190_000


def test_all_fixture_files_are_git_tracked():
    """Audit G2: the repo's global ignore patterns (*.ndjson, *.parquet,
    *.csv.gz) must never silently swallow a fixture."""
    fdir = os.path.join(ROOT, "tests", "fixtures")
    if not os.path.isdir(fdir):
        pytest.skip("no fixtures directory yet")
    on_disk = set()
    for dp, _, fs in os.walk(fdir):
        for f in fs:
            if f == ".DS_Store":
                continue
            on_disk.add(os.path.relpath(os.path.join(dp, f), ROOT))
    tracked = set(subprocess.run(
        ["git", "ls-files", "tests/fixtures"], cwd=ROOT,
        capture_output=True, text=True).stdout.splitlines())
    untracked = on_disk - tracked
    assert not untracked, (
        "fixture files exist on disk but are NOT git-tracked (gitignore "
        "swallowed them?): %s" % sorted(untracked))
