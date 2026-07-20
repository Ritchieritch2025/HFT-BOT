#!/usr/bin/env python3
"""Byte-identity and crash-safety proof for the .13 L2 episode prewarm.

Release .13 parallelizes the L2 replay/episodes bottleneck ACROSS partitions:
``deep03_fullscope_runner.prewarm_l2_episode_partitions`` computes pending
partitions in worker processes (each a fresh DuckDB session pinned to
``L2_PINNED_THREADS`` with its own scratch subdir) BEFORE the sequential
pinned-session chain runs; the chain then validates and reuses them through
its normal resume path.  ``deep03_v3_l2.py`` and ``deep03_v3_methods.py`` are
byte-unchanged (their sha256s are stage-ABI-pinned), so the live
source-bound namespace's completed partitions remain reusable and the
per-partition compute path is literally the same ``_replay_one_partition``.

Proofs, on the shared multi-market fixture from
``test_deep03_thread_determinism``:

1. Determinism: a fully sequential .12-mechanism run versus a
   physical-resume + 4-worker-parallel prewarm + sequential finish produces a
   byte-identical checkpoint store on EVERY stage (atlas included: it is
   still computed only by the sequential pinned session in both regimes).
2. Resume/mix: partial sequential progress (some replay/episode partitions
   already published) finished by the parallel prewarm yields a store
   byte-identical to the fully sequential one, with the pre-existing
   partitions skipped, not recomputed.
3. Crash-safety: a worker failing mid-run (forced via a payload the store
   refuses to validate) aborts the prewarm without any corrupt partition
   becoming reusable; crash remnants (payload without receipt, replay
   receipt without its episode receipt, ``.partial`` junk) are recomputed
   deterministically on rerun and the finished store is byte-identical.

The prewarm dispatches work only for dates the exact sequential rule admits
and only from COMPLETE physical shards, so a fresh namespace (no physical
stage yet) is a fast no-op and the sequential chain stays the authority.
"""

from __future__ import annotations

import json
import resource
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "research"))
sys.path.insert(0, str(ROOT / "tests"))

import deep03_v3_l2 as l2  # noqa: E402
import deep03_fullscope_runner as runner  # noqa: E402
from deep03_fullscope_runner import _l2_pinned_connection  # noqa: E402
from deep03_v3_common import Deep03InputError  # noqa: E402

import test_deep03_thread_determinism as thread_proof  # noqa: E402


FIXTURE_MARKET_BUCKETS = 4
# 3 clean analysis dates x (4 hash buckets + the NULL-market bucket).
EXPECTED_REPLAY_PARTITIONS = 3 * (FIXTURE_MARKET_BUCKETS + 1)


def _sequential_ground_truth(tmp_path: Path):
    """One fully sequential .12-mechanism run: the byte-identity baseline."""
    manifest = thread_proof._write_l2_fixture(tmp_path / "input")
    binding = l2.bounded_source_binding(manifest)
    root, tree, result = thread_proof._run_l2(
        tmp_path, manifest, binding, "seq", mechanism="dot12"
    )
    return manifest, binding, root, tree, result


def _stage_keys(tree: dict[str, bytes], stage: str) -> list[str]:
    return sorted(
        path[len(f"{stage}/receipts/") : -len(".json")]
        for path in tree
        if path.startswith(f"{stage}/receipts/") and path.endswith(".json")
    )


def _copy_stage(
    source_root: Path,
    target_root: Path,
    stage: str,
    keys: list[str],
    *,
    include_manifest: bool,
) -> None:
    for key in keys:
        for sub, suffix in (("data", ".parquet"), ("receipts", ".json")):
            source = source_root / stage / sub / f"{key}{suffix}"
            target = target_root / stage / sub / f"{key}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    if include_manifest:
        for name in ("MANIFEST.json", "MANIFEST.sha256"):
            shutil.copyfile(
                source_root / stage / name, target_root / stage / name
            )


def _resume_root_with_physical(
    tmp_path: Path, sequential_root: Path, sequential_tree: dict[str, bytes], tag: str
) -> Path:
    """A namespace in the production resume state: physical complete, rest absent."""
    target = tmp_path / f"checkpoints-{tag}"
    target.mkdir(parents=True)
    _copy_stage(
        sequential_root,
        target,
        "l2_physical",
        _stage_keys(sequential_tree, "l2_physical"),
        include_manifest=True,
    )
    return target


def _prewarm(
    tmp_path: Path,
    manifest: dict[str, Any],
    binding: str,
    root: Path,
    tag: str,
    *,
    workers: int = runner.L2_EPISODE_WORKERS,
) -> dict[str, Any]:
    return runner.prewarm_l2_episode_partitions(
        checkpoint_namespace=root,
        source_binding=binding,
        input_manifest=manifest,
        market_buckets=FIXTURE_MARKET_BUCKETS,
        memory_limit="16GB",
        scratch_root=tmp_path / f"prewarm-scratch-{tag}",
        workers=workers,
    )


def _sequential_finish(
    tmp_path: Path, manifest: dict[str, Any], binding: str, root: Path, tag: str
) -> dict[str, Any]:
    """The exact production sequential chain (pinned fresh session)."""
    con = _l2_pinned_connection(tmp_path / f"scratch-pinned-{tag}", "16GB")
    try:
        store = l2.BoundedCheckpointStore(root, binding)
        try:
            result = l2.execute_l2_snbd_bounded(
                con, manifest, store, market_buckets=FIXTURE_MARKET_BUCKETS
            )
        finally:
            store.close()
    finally:
        con.close()
    assert result["state"] == "COMPLETE_WITH_DATA_QUALITY_EXCLUSIONS"
    return result


def test_parallel_prewarm_store_is_byte_identical_to_sequential(tmp_path):
    """Proof 1: 4-worker prewarm + sequential finish == sequential, byte-wise."""
    manifest, binding, seq_root, seq_tree, seq_result = _sequential_ground_truth(
        tmp_path
    )
    par_root = _resume_root_with_physical(tmp_path, seq_root, seq_tree, "par")

    children_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    prewarm = _prewarm(tmp_path, manifest, binding, par_root, "par")
    children_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss

    assert prewarm["state"] == "COMPLETE"
    assert prewarm["workers_used"] == runner.L2_EPISODE_WORKERS
    assert prewarm["pinned_threads"] == runner.L2_PINNED_THREADS
    assert prewarm["eligible_dates"] == list(l2.L2_ANALYSIS_DATES)
    assert prewarm["already_complete"] == []
    assert prewarm["reused_by_worker"] == []
    assert len(prewarm["computed"]) == EXPECTED_REPLAY_PARTITIONS
    # The prewarm scratch tree must not survive a successful prewarm.
    assert not (tmp_path / "prewarm-scratch-par").exists()
    # Fixture-scale worker memory documentation: the per-partition fixture
    # peak (max child RSS) stays far below the production ~10GB/partition
    # observation that sets L2_EPISODE_WORKERS=4; this guards against a
    # pathological fixed overhead in the worker path itself.
    peak_child_bytes = max(0, children_after - children_before)
    if sys.platform != "darwin":  # ru_maxrss is KiB on Linux, bytes on macOS
        peak_child_bytes *= 1024
    assert peak_child_bytes < 4 * 1024**3

    result = _sequential_finish(tmp_path, manifest, binding, par_root, "par")
    # The sequential chain must have REUSED every prewarmed partition.
    for stage in ("l2_replay", "l2_episodes"):
        assert result["activity"][stage] == {
            "written": 0,
            "reused": EXPECTED_REPLAY_PARTITIONS,
        }
    assert result["activity"]["l2_physical"]["written"] == 0

    par_tree = thread_proof._checkpoint_tree_bytes(par_root)
    thread_proof._assert_byte_identical(
        {"sequential": seq_tree, "parallel": par_tree},
        "L2 chain, sequential vs 4-worker prewarm",
    )
    # The execution receipts must agree on everything except the
    # written-vs-reused activity ledger, which legitimately differs between a
    # fresh run and a resume (asserted exactly above).
    masked_seq = {key: value for key, value in seq_result.items() if key != "activity"}
    masked_par = {key: value for key, value in result.items() if key != "activity"}
    assert thread_proof._normalized_json(masked_seq, [seq_root]) == (
        thread_proof._normalized_json(masked_par, [par_root])
    )


def test_partial_sequential_then_parallel_matches_sequential(tmp_path):
    """Proof 2: mixed resume — sequential progress finished in parallel."""
    manifest, binding, seq_root, seq_tree, _seq_result = _sequential_ground_truth(
        tmp_path
    )
    mix_root = _resume_root_with_physical(tmp_path, seq_root, seq_tree, "mix")
    # Simulate an interrupted sequential run: the first four replay+episode
    # partitions are already COMPLETE (no stage manifest yet — the sequential
    # loop finalizes only after every partition).
    done_keys = _stage_keys(seq_tree, "l2_replay")[:4]
    for stage in ("l2_replay", "l2_episodes"):
        _copy_stage(seq_root, mix_root, stage, done_keys, include_manifest=False)

    prewarm = _prewarm(tmp_path, manifest, binding, mix_root, "mix")
    assert prewarm["state"] == "COMPLETE"
    assert prewarm["already_complete"] == sorted(done_keys)
    assert len(prewarm["computed"]) == EXPECTED_REPLAY_PARTITIONS - len(done_keys)

    _sequential_finish(tmp_path, manifest, binding, mix_root, "mix")
    mix_tree = thread_proof._checkpoint_tree_bytes(mix_root)
    thread_proof._assert_byte_identical(
        {"sequential": seq_tree, "mixed": mix_tree},
        "L2 chain, sequential vs partial-sequential-then-parallel",
    )


def test_worker_failure_aborts_without_accepting_corruption(tmp_path):
    """Proof 3a: a failing worker aborts the prewarm; nothing corrupt is kept."""
    manifest, binding, seq_root, seq_tree, _seq_result = _sequential_ground_truth(
        tmp_path
    )
    crash_root = _resume_root_with_physical(tmp_path, seq_root, seq_tree, "crash")
    # Corrupt ONE physical payload after its receipt was published.  The
    # worker's fail-closed validate refuses it (payload hash mismatch), which
    # both proves no replay is ever computed from an unvalidated shard and
    # forces a mid-run pool abort (remaining workers are terminated
    # mid-partition — a genuine crash).
    victim = _stage_keys(seq_tree, "l2_replay")[0]
    victim_payload = crash_root / "l2_physical" / "data" / f"{victim}.parquet"
    original = victim_payload.read_bytes()
    victim_payload.write_bytes(original + b"CORRUPTION")

    with pytest.raises(Deep03InputError, match="prewarm worker failed"):
        _prewarm(tmp_path, manifest, binding, crash_root, "crash-a")

    # The corrupted shard gained no replay/episode receipt: the store never
    # accepted anything derived from it.
    for stage in ("l2_replay", "l2_episodes"):
        assert not (crash_root / stage / "receipts" / f"{victim}.json").exists()

    # Repair the shard and rerun: the prewarm completes (reusing whatever the
    # terminated workers had already atomically published) and the finished
    # store is byte-identical to the sequential baseline.
    victim_payload.write_bytes(original)
    prewarm = _prewarm(tmp_path, manifest, binding, crash_root, "crash-b")
    assert prewarm["state"] == "COMPLETE"
    survivors = len(prewarm["already_complete"]) + len(prewarm["reused_by_worker"])
    assert survivors + len(prewarm["computed"]) == EXPECTED_REPLAY_PARTITIONS

    _sequential_finish(tmp_path, manifest, binding, crash_root, "crash")
    crash_tree = thread_proof._checkpoint_tree_bytes(crash_root)
    thread_proof._assert_byte_identical(
        {"sequential": seq_tree, "crashed-then-rerun": crash_tree},
        "L2 chain, sequential vs worker-crash rerun",
    )


def test_crash_remnants_are_recomputed_not_reused(tmp_path):
    """Proof 3b: every mid-partition crash state is refused and recomputed."""
    manifest, binding, seq_root, seq_tree, _seq_result = _sequential_ground_truth(
        tmp_path
    )
    remnant_root = _resume_root_with_physical(
        tmp_path, seq_root, seq_tree, "remnant"
    )
    keys = _stage_keys(seq_tree, "l2_replay")
    # Crash state A: replay payload promoted, worker died before its receipt.
    # The store treats a receiptless payload as an unpublished crash remnant.
    orphan = keys[0]
    orphan_data = remnant_root / "l2_replay" / "data" / f"{orphan}.parquet"
    orphan_data.parent.mkdir(parents=True, exist_ok=True)
    orphan_data.write_bytes(b"NOT A COMPLETE PARTITION")
    # Crash state B: worker died between the replay publish and the episode
    # publish — replay COMPLETE, episode absent.
    half = keys[1]
    _copy_stage(seq_root, remnant_root, "l2_replay", [half], include_manifest=False)
    # Crash state C: an abandoned scatter temporary under .partial/.
    partial = remnant_root / ".partial" / "l2_replay-abandoned-deadbeef.parquet"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"ABANDONED TEMPORARY")

    prewarm = _prewarm(tmp_path, manifest, binding, remnant_root, "remnant")
    assert prewarm["state"] == "COMPLETE"
    # The half-published partition is NOT skippable (episode receipt missing)
    # and is recomputed by a worker, whose replay write then validates and
    # reuses the already-COMPLETE replay receipt rather than recomputing it.
    assert prewarm["already_complete"] == []
    assert orphan in prewarm["computed"]
    assert half in prewarm["reused_by_worker"] or half in prewarm["computed"]
    # The orphan payload was recomputed and republished with a receipt.
    receipt_path = remnant_root / "l2_replay" / "receipts" / f"{orphan}.json"
    assert receipt_path.is_file()
    published = json.loads(receipt_path.read_bytes())
    assert published["data"]["size_bytes"] == orphan_data.stat().st_size
    assert orphan_data.read_bytes() != b"NOT A COMPLETE PARTITION"

    _sequential_finish(tmp_path, manifest, binding, remnant_root, "remnant")
    remnant_tree = thread_proof._checkpoint_tree_bytes(remnant_root)
    thread_proof._assert_byte_identical(
        {"sequential": seq_tree, "remnant-rerun": remnant_tree},
        "L2 chain, sequential vs crash-remnant rerun",
    )


def test_prewarm_is_noop_on_fresh_namespace(tmp_path):
    """No physical stage yet -> fast no-op; sequential stays the authority."""
    manifest = thread_proof._write_l2_fixture(tmp_path / "input")
    binding = l2.bounded_source_binding(manifest)
    fresh_root = tmp_path / "checkpoints-fresh"
    fresh_root.mkdir()
    prewarm = _prewarm(tmp_path, manifest, binding, fresh_root, "fresh")
    assert prewarm["state"] == "NOOP_NOTHING_PENDING"
    assert prewarm["workers_used"] == 0
    assert prewarm["computed"] == []
    assert prewarm["skipped_dates"] == {
        date: "physical_stage_incomplete" for date in l2.L2_ANALYSIS_DATES
    }
    # Nothing may have been created in the store.
    assert sorted(path.name for path in fresh_root.iterdir()) == []


def test_worker_count_is_memory_bounded_and_stage_names_match_l2():
    """The .13 capacity constants and the fail-closed literal pins."""
    # 6 workers x the observed ~10GB/partition production RSS would exceed
    # both the ~45GB planning bound and MemoryMax=52G; 4 x ~10GB ~= 40GB
    # fits.  A raise above 4 requires a new production memory measurement.
    assert runner.L2_EPISODE_WORKERS == 4
    assert runner.L2_PINNED_THREADS == 1
    service = (
        ROOT / "deploy" / "w09" / "w09-exploratory-autoresearch.service"
    ).read_text()
    assert "MemoryMax=52G" in service
    assert "MemoryHigh=40G" in service
    # The runner's stage/semantic literals must match the ones inside
    # deep03_v3_l2.execute_l2_snbd_bounded (unexportable without an ABI
    # re-version).  Drift is fail-closed at runtime; this pins it at test
    # time against the module SOURCE, which is itself sha-pinned.
    source = (ROOT / "tools" / "research" / "deep03_v3_l2.py").read_text()
    for stage, semantic in (
        runner._L2_PHYSICAL_STAGE,
        runner._L2_REPLAY_STAGE,
        runner._L2_EPISODE_STAGE,
    ):
        assert f'"{stage}"' in source
        assert f'"{semantic}"' in source
