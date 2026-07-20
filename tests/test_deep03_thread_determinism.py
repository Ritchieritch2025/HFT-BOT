#!/usr/bin/env python3
"""Thread-count invariance proof for the Deep03 bounded stage chains.

Release .12 raises the production DuckDB session thread count from 2 to 8 as
a physical-parallelism-only capacity fix, while the runner pins the L2 stage
chain to ``L2_PINNED_THREADS`` (the .11 value) because ``l2_exact_atlas``
aggregates DOUBLE columns and parallel float reduction is order-dependent
(measured 2026-07-20: unpinned threads=8 changed ``total_dwell_us`` bytes and
was not self-reproducible run to run).

The pin is ``L2_PINNED_THREADS = 1`` on a dedicated fresh session
(``_l2_pinned_connection``), because measurement (2026-07-20) showed there is
no such thing as stable "threads=2 bytes" to preserve: the .11 production
regime itself produced differing ``l2_exact_atlas`` payload bytes across
identical runs (1 of 4 diverged), threads=1 was byte-identical across every
run, and a runtime ``SET threads`` inside a wider session yielded yet another
reduction order.  Every OTHER L2 stage is thread-count invariant, so the
partitions the live store already completed are reused byte-as-written.

These tests are the safety proof for that design, on a shared multi-market,
multi-row-group fixture under the exact production DuckDB session
configuration (``deep03_v3_runner._configure_duckdb``, including
``preserve_insertion_order=false``) and the exact production pin mechanism
(``deep03_fullscope_runner._l2_pinned_connection``):

1. The full L2 chain (physical -> replay -> episodes -> atlas -> matches)
   run the .11 way (a plain startup-threads=2 session) and run the .12 way
   (an 8-thread session opened and closed first, then the pinned session)
   produces byte-identical stores on every stage except ``l2_exact_atlas``,
   whose rows must be exactly equal on every column except the DOUBLE
   summation ``total_dwell_us``, which must agree within float reassociation
   tolerance -- .11 itself cannot reproduce that column byte-for-byte.
2. Two independent runs of the .12 mechanism are FULLY byte-identical
   (self-reproducibility, which the .11 regime lacked).
3. The bounded B-stage reduce at threads 2 versus 8 produces exactly equal
   statistical results; its checkpoint payloads may differ only by intra-file
   row order (the documented, pre-existing threads=2 run-to-run status quo of
   the scatter stages -- payload row order was never a stable property of the
   store), never by row content.

Any divergence outside these proven-tight bounds means order-dependence
survived and the thread raise is unsafe: stop the release.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))
sys.path.insert(0, str(ROOT / "tests"))

import deep03_v3_l2 as l2  # noqa: E402
from deep03_fullscope_runner import (  # noqa: E402
    L2_PINNED_THREADS,
    _l2_pinned_connection,
)
from deep03_v3_methods import execute_all_bounded  # noqa: E402
from deep03_v3_runner import _configure_duckdb  # noqa: E402

import test_deep03_v3_bounded_reducers as reducer_fixtures  # noqa: E402


# The .11 production session value and the .12 candidate session value.
THREAD_SETTINGS = (2, 8)
MARKETS_PER_DATE = 12
DELTA_EVENTS_PER_MARKET = 120
# Small parquet row groups force real multi-row-group parallel scans even on
# a test-sized fixture, so an order-dependent stage has a genuine chance to
# interleave differently at different thread counts.
SOURCE_ROW_GROUP_SIZE = 512


def _production_connection(scratch_root: Path, threads: int, tag: str):
    """One DuckDB session configured exactly like the fullscope runner."""
    con = duckdb.connect()
    _configure_duckdb(
        con, scratch_root / f"duckdb-scratch-{tag}", "16GB", threads
    )
    return con


def _clean_quality_receipt(date: str) -> dict[str, object]:
    return {
        "date": date,
        "lines": 100,
        "parse_errors": 0,
        "seq_gap_events": 0,
        "seq_missed_total": 0,
        "seq_regressions": 0,
        "markers_lost_frames": 0,
        "no_l2_files": False,
        "recorder_markers": {},
    }


def _l2_market_rows(date: str, date_index: int, market_index: int) -> list[tuple]:
    """A deterministic depletion/refill stream for one market on one date."""
    market = f"M-{date_index}-{market_index:02d}"
    event = f"E-{date_index}-{market_index % 4}"
    base = (
        1_800_000_000_000_000_000
        + date_index * 3_600_000_000_000
        + market_index * 7_000_000
    )
    rows: list[tuple] = [
        (
            date, base // 1000, base, base + 1, market, event,
            "Baseball", f"S-{date_index}", "snapshot", None, None, None,
            "[[4000,20000],[3900,15000],[3800,10000]]",
            "[[5000,20000],[5100,15000],[5200,10000]]",
            7, 1,
        )
    ]
    for step in range(DELTA_EVENTS_PER_MARKET):
        clock = base + (step + 1) * 1_000_000 + (market_index % 5) * 137
        cycle = step % 4
        if cycle == 0:
            side, price, delta = "yes", 4000, -12_000
        elif cycle == 1:
            side, price, delta = "yes", 4000, 9_000
        elif cycle == 2:
            side, price, delta = "no", 5000, -11_000
        else:
            side, price, delta = "no", 5000, 8_500
        rows.append(
            (
                date, clock // 1000, clock, clock + 1, market, event,
                "Baseball", f"S-{date_index}", "delta", side, price, delta,
                None, None, 7, step + 2,
            )
        )
    return rows


def _write_l2_fixture(base_dir: Path) -> dict[str, object]:
    """A multi-market L2 manifest shared verbatim by every thread setting."""
    objects: list[dict[str, object]] = []
    releases = []
    for date_index, date in enumerate(l2.L2_SCOPE_DATES):
        release_id = f"{date}__v3ref__threadproof"
        releases.append({"date": date, "release_id": release_id})
        if date in l2.L2_ABSENT_DATES:
            continue
        rows: list[tuple] = []
        for market_index in range(MARKETS_PER_DATE):
            rows.extend(_l2_market_rows(date, date_index, market_index))
        # Interleave markets by receive clock the way live capture does.
        rows.sort(key=lambda row: (row[2], row[4]))
        fact = (
            base_dir
            / f"facts/orderbooks_full/category=Sports/date={date}/part.parquet"
        )
        fact.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        try:
            con.execute(
                """
                CREATE TABLE source(
                  date DATE,local_recv_ts_us BIGINT,recv_wall_ns BIGINT,
                  recv_mono_ns BIGINT,market_ticker VARCHAR,
                  event_ticker VARCHAR,subcategory VARCHAR,
                  series_ticker VARCHAR,msg_type VARCHAR,side VARCHAR,
                  price_e4 BIGINT,delta_e4 BIGINT,yes_levels VARCHAR,
                  no_levels VARCHAR,ws_sid BIGINT,ws_seq BIGINT
                )
                """
            )
            con.executemany(
                "INSERT INTO source VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            escaped = str(fact).replace("'", "''")
            con.execute(
                f"COPY source TO '{escaped}' "
                f"(FORMAT PARQUET,ROW_GROUP_SIZE {SOURCE_ROW_GROUP_SIZE})"
            )
        finally:
            con.close()
        fact_payload = fact.read_bytes()
        objects.append(
            {
                "release_id": release_id,
                "date": date,
                "kind": "facts",
                "channel": "orderbooks_full",
                "logical_key": (
                    "warehouse/facts/orderbooks_full/category=Sports/"
                    f"date={date}/part.parquet"
                ),
                "local_path": str(fact),
                "source_version_id": f"fact-version-{date}",
                "sha256": hashlib.sha256(fact_payload).hexdigest(),
                "size": len(fact_payload),
                "row_count": len(rows),
            }
        )
        quality = base_dir / f"quality/date={date}/l2_gaps.json"
        quality.parent.mkdir(parents=True, exist_ok=True)
        quality.write_text(json.dumps(_clean_quality_receipt(date)) + "\n")
        quality_payload = quality.read_bytes()
        objects.append(
            {
                "release_id": release_id,
                "date": date,
                "kind": "l2_quality_receipt",
                "channel": None,
                "logical_key": f"control/quality/v1/date={date}/l2_gaps.json",
                "local_path": str(quality),
                "source_version_id": f"quality-version-{date}",
                "sha256": hashlib.sha256(quality_payload).hexdigest(),
                "size": len(quality_payload),
                "row_count": None,
            }
        )
    return {
        "release_ids": [row["release_id"] for row in releases],
        "release_dates": list(l2.L2_SCOPE_DATES),
        "releases": releases,
        "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "objects": objects,
    }


def _checkpoint_tree_bytes(root: Path) -> dict[str, bytes]:
    """Every durable checkpoint byte, keyed by path relative to the store."""
    tree: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".partial/") or relative == ".CHECKPOINT_WRITER.lock":
            continue
        tree[relative] = path.read_bytes()
    return tree


# The one column where .11 itself is not byte-reproducible: a parallel SUM
# over DOUBLE values.  Differences between reduction orders are pure float
# reassociation noise and must stay within this tolerance.
ATLAS_FLOAT_SUM_COLUMNS = frozenset({"total_dwell_us"})
FLOAT_REASSOCIATION_REL_TOL = 1e-9


def _canonical_rows(
    path: Path, *, exclude_from_order: frozenset[str] = frozenset()
) -> tuple[list[str], list[tuple]]:
    """Schema plus deterministically sorted rows of one parquet payload."""
    con = duckdb.connect()
    try:
        escaped = str(path).replace("'", "''")
        columns = [
            row[0]
            for row in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"
            ).fetchall()
        ]
        order = ",".join(
            f'"{column}"'
            for column in columns
            if column not in exclude_from_order
        )
        rows = con.execute(
            f"SELECT * FROM read_parquet('{escaped}') ORDER BY {order}"
        ).fetchall()
        return columns, rows
    finally:
        con.close()


def _assert_atlas_rows_reassociation_equal(
    first_path: Path, second_path: Path, relative: str
) -> None:
    """Exact equality everywhere except the known float-sum columns."""
    first = _canonical_rows(
        first_path, exclude_from_order=ATLAS_FLOAT_SUM_COLUMNS
    )
    second = _canonical_rows(
        second_path, exclude_from_order=ATLAS_FLOAT_SUM_COLUMNS
    )
    assert first[0] == second[0], f"{relative}: schema drift"
    assert len(first[1]) == len(second[1]), f"{relative}: row count drift"
    for row_a, row_b in zip(first[1], second[1]):
        for column, value_a, value_b in zip(first[0], row_a, row_b):
            if value_a == value_b:
                continue
            assert column in ATLAS_FLOAT_SUM_COLUMNS, (
                f"{relative}: VALUE-LEVEL divergence outside the known "
                f"float-sum columns: {column}: {value_a!r} vs {value_b!r}"
            )
            assert (
                isinstance(value_a, float)
                and isinstance(value_b, float)
                and math.isclose(
                    value_a,
                    value_b,
                    rel_tol=FLOAT_REASSOCIATION_REL_TOL,
                    abs_tol=0.0,
                )
            ), (
                f"{relative}: {column} differs beyond float reassociation "
                f"tolerance: {value_a!r} vs {value_b!r}"
            )


def _assert_byte_identical(
    trees: dict[str, dict[str, bytes]], label: str
) -> None:
    tags = list(trees)
    baseline = trees[tags[0]]
    for tag in tags[1:]:
        candidate = trees[tag]
        missing = sorted(set(baseline) - set(candidate))
        extra = sorted(set(candidate) - set(baseline))
        assert not missing and not extra, (
            f"{label}: checkpoint file sets diverge between {tags[0]} and "
            f"{tag}: missing={missing} extra={extra}"
        )
        differing = sorted(
            path for path in baseline if baseline[path] != candidate[path]
        )
        assert not differing, (
            f"{label}: ORDER-DEPENDENT OUTPUT -- byte-differing checkpoint "
            f"files between {tags[0]} and {tag}: {differing}"
        )


def _normalized_json(value: Any, roots: list[Path]) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    for index, root in enumerate(roots):
        text = text.replace(str(root), f"<ROOT-{index}>")
    return text


def _run_l2(
    tmp_path: Path,
    manifest: dict[str, object],
    binding: str,
    tag: str,
    *,
    mechanism: str,
) -> tuple[Path, dict[str, bytes], dict[str, Any]]:
    """Run the L2 chain the .11 way or the shipped .12 way.

    ``mechanism="dot11"``: one plain production session configured at
    startup with threads=2 (exactly how the live .11 run computes L2).
    ``mechanism="dot12"``: first an 8-thread production session is opened,
    exercised and closed (as the runner does for B01-B04 and the graph),
    then the L2 chain runs on ``_l2_pinned_connection`` -- the exact .12
    production sequence.
    """
    root = tmp_path / f"checkpoints-l2-{tag}"
    if mechanism == "dot11":
        con = _production_connection(tmp_path, 2, f"l2-{tag}")
    elif mechanism == "dot12":
        session = _production_connection(tmp_path, 8, f"session-{tag}")
        try:
            # Give the full-thread session real parallel work, as the B and
            # graph stages would, before it closes.
            session.execute(
                "SELECT sum(cast(range AS DOUBLE)) FROM range(1000000)"
            ).fetchone()
        finally:
            session.close()
        con = _l2_pinned_connection(
            tmp_path / f"duckdb-scratch-pinned-{tag}", "16GB"
        )
    else:  # pragma: no cover - defensive
        raise ValueError(mechanism)
    try:
        expected_threads = 2 if mechanism == "dot11" else L2_PINNED_THREADS
        assert (
            int(con.execute("SELECT current_setting('threads')").fetchone()[0])
            == expected_threads
        )
        store = l2.BoundedCheckpointStore(root, binding)
        try:
            result = l2.execute_l2_snbd_bounded(
                con, manifest, store, market_buckets=4
            )
        finally:
            store.close()
    finally:
        con.close()
    assert result["state"] == "COMPLETE_WITH_DATA_QUALITY_EXCLUSIONS"
    for stage in ("l2_physical", "l2_replay", "l2_episodes", "l2_exact_atlas"):
        assert result["activity"][stage]["written"] > 0, stage
    return root, _checkpoint_tree_bytes(root), result


def _masked_atlas_result(result: dict[str, Any], root: Path) -> str:
    """The result JSON with the atlas stage-manifest digest neutralized.

    The atlas payload digest tracks the float-sum bytes, which .11 itself
    cannot reproduce run to run; every other result field must be exactly
    equal across regimes.
    """
    masked = copy.deepcopy(result)
    for stage_row in masked.get("stages", []):
        if stage_row.get("stage") == "l2_exact_atlas":
            stage_row["manifest_sha256"] = "<FLOAT-SUM-REASSOCIATION>"
    return _normalized_json(masked, [root])


def test_l2_pinned_mechanism_matches_dot11_within_reassociation(
    tmp_path: Path,
) -> None:
    """Check 1: .12 pinned L2 equals the .11 regime on every proven-stable byte.

    Every stage except l2_exact_atlas must be byte-identical; the atlas rows
    must be exactly equal on all columns except the DOUBLE summation, which
    the .11 regime itself cannot reproduce byte-for-byte across runs.
    """
    manifest = _write_l2_fixture(tmp_path / "input")
    binding = l2.bounded_source_binding(manifest)
    roots: dict[str, Path] = {}
    trees: dict[str, dict[str, bytes]] = {}
    results: dict[str, dict[str, Any]] = {}
    for tag, mechanism in (("dot11", "dot11"), ("dot12", "dot12")):
        roots[tag], trees[tag], results[tag] = _run_l2(
            tmp_path, manifest, binding, tag, mechanism=mechanism
        )
    # The fixture must be large enough to span several parquet row groups,
    # otherwise a single-threaded scan would make this proof vacuous.
    fact_rows = sum(
        obj["row_count"]
        for obj in manifest["objects"]
        if obj["kind"] == "facts"
    )
    assert fact_rows > 4 * SOURCE_ROW_GROUP_SIZE
    atlas_payloads = [
        path
        for path in trees["dot11"]
        if path.startswith("l2_exact_atlas/") and path.endswith(".parquet")
    ]
    assert atlas_payloads, "atlas stage produced no payloads"

    baseline, candidate = trees["dot11"], trees["dot12"]
    assert set(baseline) == set(candidate)
    for path in sorted(baseline):
        if baseline[path] == candidate[path]:
            continue
        stage = path.split("/", 1)[0]
        assert stage == "l2_exact_atlas", (
            "ORDER-DEPENDENT OUTPUT outside the known float-sum stage: "
            f"{path} differs between the .11 and .12 regimes -- unsafe"
        )
        if path.endswith(".parquet"):
            _assert_atlas_rows_reassociation_equal(
                roots["dot11"] / path, roots["dot12"] / path, path
            )
    assert _masked_atlas_result(
        results["dot11"], roots["dot11"]
    ) == _masked_atlas_result(results["dot12"], roots["dot12"])


def test_l2_pinned_mechanism_is_self_reproducible(tmp_path: Path) -> None:
    """Check 2: two independent runs of the .12 mechanism are byte-identical.

    This is the property the .11 threads=2 regime measurably lacked (1 of 4
    identical runs produced different atlas float-sum bytes).
    """
    manifest = _write_l2_fixture(tmp_path / "input")
    binding = l2.bounded_source_binding(manifest)
    roots: dict[str, Path] = {}
    trees: dict[str, dict[str, bytes]] = {}
    results: dict[str, dict[str, Any]] = {}
    for tag in ("dot12-a", "dot12-b"):
        roots[tag], trees[tag], results[tag] = _run_l2(
            tmp_path, manifest, binding, tag, mechanism="dot12"
        )
    _assert_byte_identical(trees, "L2 chain, .12 pinned session, run A vs B")
    assert _normalized_json(results["dot12-a"], [roots["dot12-a"]]) == (
        _normalized_json(results["dot12-b"], [roots["dot12-b"]])
    )


def test_bounded_b_stage_reduce_values_are_thread_count_invariant(
    tmp_path: Path,
) -> None:
    """Check 3: B reduce results exactly equal; payload diffs row-order only.

    Intra-payload row order of the scatter stages was measured to vary run
    to run already at the .11 production threads=2 (documented status quo:
    the store's reuse contract pins each payload's bytes at write time via
    its receipt, it never promised cross-run byte reproducibility).  Row
    CONTENT and every reduced statistical value must be exactly equal.
    """
    manifest = reducer_fixtures._end_to_end_manifest(tmp_path / "input")
    trees: dict[int, dict[str, bytes]] = {}
    outputs: dict[int, str] = {}
    roots = {
        threads: tmp_path / f"checkpoints-b-t{threads}"
        for threads in THREAD_SETTINGS
    }
    for threads in THREAD_SETTINGS:
        con = _production_connection(tmp_path, threads, f"b-t{threads}")
        try:
            capabilities, methods, receipt = execute_all_bounded(
                con, manifest, roots[threads], market_buckets=2
            )
        finally:
            con.close()
        assert receipt["state"] == "COMPLETE"
        trees[threads] = _checkpoint_tree_bytes(roots[threads])
        outputs[threads] = _normalized_json(
            {"capabilities": capabilities, "methods": methods},
            [roots[threads]],
        )
    # The reduced statistical outputs must be EXACTLY equal -- floats
    # included.  This is the semantic go/no-go for running B at 8 threads.
    assert outputs[THREAD_SETTINGS[0]] == outputs[THREAD_SETTINGS[1]]

    baseline, candidate = (trees[threads] for threads in THREAD_SETTINGS)
    assert set(baseline) == set(candidate)
    order_only_stages: set[str] = set()
    for path in sorted(baseline):
        if baseline[path] == candidate[path]:
            continue
        if path.endswith(".parquet"):
            first = _canonical_rows(roots[THREAD_SETTINGS[0]] / path)
            second = _canonical_rows(roots[THREAD_SETTINGS[1]] / path)
            assert first == second, (
                f"B-stage VALUE-LEVEL divergence in {path}: sorted canonical "
                "rows differ between thread counts -- unsafe"
            )
            order_only_stages.add(path.split("/", 1)[0])
    # Receipts and stage manifests are allowed to differ only as trackers of
    # an order-only payload (they record the payload sha256); any receipt
    # diff outside such a stage would be an unexplained divergence.
    for path in sorted(baseline):
        if baseline[path] == candidate[path] or path.endswith(".parquet"):
            continue
        stage = path.split("/", 1)[0]
        assert stage in order_only_stages, (
            f"B-stage non-payload checkpoint file diverged outside any "
            f"order-only stage: {path}"
        )
