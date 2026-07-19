#!/usr/bin/env python3
"""Offline tests for the W09 durable read-only V3 cache synchronizer."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import inspect
import json
from pathlib import Path
import py_compile
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
W09 = ROOT / "deploy" / "w09"
for candidate in (TOOLS, W09):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import research_cache_sync as sync  # noqa: E402


NOW = dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc)


def run_id(suffix: str) -> str:
    return f"20260718T120000.000000Z-123-{suffix}"


def release_id(date: str, suffix: str) -> str:
    return f"{date}__v3ref__seal-aaaaaaaa__pub-{suffix:0>16}"


def manifest(date: str, suffix: str, *, published: str | None = None,
             corrections: int = 0, rfq: bool = False) -> tuple[str, bytes]:
    rid = release_id(date, suffix)
    value = {
        "release_id": rid,
        "date": date,
        "published_at_utc": published or f"{date}T04:00:00Z",
        "corrections": {
            "included_files": corrections,
            "ledger_day_entries": 0,
        },
        "reference_set_sha256": (suffix[-1] if suffix else "a") * 64,
        "publication_state_sha256": (suffix[0] if suffix else "b") * 64,
        "evidence_tier": "SEALED_DEGRADED_EVIDENCE",
        "rfq_included": rfq,
    }
    raw = json.dumps(value, sort_keys=True).encode()
    return rid, raw


def validator(value: dict, rid: str) -> dict:
    assert value["release_id"] == rid
    return {
        "schema": sync.reference.SCHEMA,
        "storage_mode": sync.reference.STORAGE_MODE,
        "release_id": rid,
        "date": value["date"],
        "published_at_utc": value["published_at_utc"],
        "corrections": value["corrections"],
        "reference_set_sha256": value["reference_set_sha256"],
        "publication_state_sha256": value["publication_state_sha256"],
        "evidence_tier": value["evidence_tier"],
        "rfq_included": value["rfq_included"],
    }


class FixtureStore:
    def __init__(self, releases: list[tuple[str, bytes]], extras=None):
        self.releases = dict(releases)
        self.extras = list(extras or [])
        self.list_calls: list[str] = []
        self.get_calls: list[str] = []

    def list(self, prefix=""):
        self.list_calls.append(prefix)
        rows = [
            (f"releases/{rid}/MANIFEST.json", len(raw))
            for rid, raw in self.releases.items()
        ]
        return sorted(rows + self.extras)

    def get_bytes_with_version(self, key):
        self.get_calls.append(key)
        rid = key.split("/")[1]
        raw = self.releases[rid]
        return raw, "fixture-version-" + hashlib.sha256(raw).hexdigest()[:20]


def marker_for(row: dict) -> dict:
    return {
        "schema": "research-reference-verified-v1",
        "storage_mode": "REFERENCE_V3",
        "version_binding_mode": sync.reference.STORAGE_MODE,
        "release_id": row["selected_release_id"],
        "date": row["date"],
        "manifest_sha256": row["manifest_sha256"],
        "manifest_version_id": row["manifest_version_id"],
        "reference_set_sha256": row["reference_set_sha256"],
        "publication_state_sha256": row["publication_state_sha256"],
        "rfq_status": (
            "NOT_FETCHED_OPT_IN"
            if row["rfq_included_in_manifest"]
            else "ABSENT_FROM_RELEASE"
        ),
    }


class FixtureFetcher:
    def __init__(self, failures: set[str] | None = None):
        self.failures = failures if failures is not None else set()
        self.calls: list[tuple[str, str]] = []

    def __call__(self, _store, cache_root: Path, row: dict):
        self.calls.append((row["date"], row["rfq_fetch_policy"]))
        if row["date"] in self.failures:
            raise OSError("offline fixture exact GET failed")
        marker = marker_for(row)
        path = (
            cache_root
            / "releases"
            / row["selected_release_id"]
            / ".VERIFIED.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marker), encoding="utf-8")
        return marker


def read_state(cache: Path) -> dict:
    return json.loads(
        (cache / ".research-cache-sync" / "STATE.json").read_text()
    )


def test_discovery_reads_only_strict_v3_manifest_paths_and_selects_latest():
    older = manifest(
        "2026-07-17", "1", published="2026-07-18T04:00:00Z"
    )
    correction = manifest(
        "2026-07-17",
        "2",
        published="2026-07-18T04:00:00Z",
        corrections=1,
    )
    store = FixtureStore(
        [older, correction],
        extras=[
            ("releases/not-v3/MANIFEST.json", 10),
            (f"releases/{older[0]}/payload.bin", 99),
            ("other-prefix/secret", 12),
        ],
    )

    selected, errors, stats = sync.discover_releases(store, validator)

    assert store.list_calls == ["releases/"]
    assert store.get_calls == [
        f"releases/{older[0]}/MANIFEST.json",
        f"releases/{correction[0]}/MANIFEST.json",
    ]
    assert selected["2026-07-17"]["release_id"] == correction[0]
    assert errors == {}
    assert stats["ignored_non_v3_or_non_manifest"] == 3


def test_continuity_finds_real_internal_gap_but_not_prepublication_dates(tmp_path):
    store = FixtureStore([
        manifest("2026-07-10", "1"),
        manifest("2026-07-12", "2"),
    ])
    result = sync.sync_once(
        store=store,
        cache_root=tmp_path / "cache",
        lookback_days=35,
        validator=validator,
        fetcher=FixtureFetcher(),
        now=NOW,
        run_id=run_id("11111111"),
    )

    assert result["outcome"] == "RETRY_REQUIRED"
    assert result["continuity"]["start_date"] == "2026-07-10"
    assert result["continuity"]["missing_dates"] == ["2026-07-11"]


def test_historical_failure_remains_backlog_outside_lookback_and_retries(tmp_path):
    cache = tmp_path / "cache"
    store = FixtureStore([
        manifest("2026-01-01", "1"),
        manifest("2026-07-17", "2"),
    ])
    fetcher = FixtureFetcher({"2026-01-01"})
    first = sync.sync_once(
        store=store,
        cache_root=cache,
        lookback_days=2,
        validator=validator,
        fetcher=fetcher,
        now=NOW,
        run_id=run_id("22222222"),
    )
    assert first["backlog_cursor"] == {
        "oldest_pending_date": "2026-01-01",
        "pending_dates": ["2026-01-01"],
        "pending_count": 1,
    }
    assert read_state(cache)["dates"]["2026-01-01"]["status"] == "RETRY"

    fetcher.failures.clear()
    second = sync.sync_once(
        store=store,
        cache_root=cache,
        lookback_days=2,
        validator=validator,
        fetcher=fetcher,
        now=NOW + dt.timedelta(hours=1),
        run_id=run_id("33333333"),
    )
    assert second["backlog_cursor"]["pending_count"] == 0
    assert fetcher.calls.count(("2026-01-01", "OFF")) == 2
    # The large calendar gap is explicit continuity evidence, not a reason to
    # discard the now-verified historical date.
    assert "2026-01-01" in read_state(cache)["dates"]


def test_work_limit_never_drops_unattempted_backlog(tmp_path):
    cache = tmp_path / "cache"
    store = FixtureStore([
        manifest("2026-07-15", "1"),
        manifest("2026-07-16", "2"),
        manifest("2026-07-17", "3"),
    ])
    fetcher = FixtureFetcher()
    result = sync.sync_once(
        store=store,
        cache_root=cache,
        max_releases_per_run=1,
        validator=validator,
        fetcher=fetcher,
        now=NOW,
        run_id=run_id("44444444"),
    )
    assert result["backlog_cursor"]["pending_dates"] == [
        "2026-07-16",
        "2026-07-17",
    ]
    assert sorted(read_state(cache)["dates"]) == [
        "2026-07-15",
        "2026-07-16",
        "2026-07-17",
    ]


def test_verified_release_is_idempotent_and_missing_marker_reopens(tmp_path):
    cache = tmp_path / "cache"
    one = manifest("2026-07-17", "1")
    store = FixtureStore([one])
    fetcher = FixtureFetcher()
    sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW,
        run_id=run_id("55555555"),
    )
    sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW + dt.timedelta(hours=1),
        run_id=run_id("66666666"),
    )
    assert fetcher.calls == [("2026-07-17", "OFF")]

    marker_path = cache / "releases" / one[0] / ".VERIFIED.json"
    marker_path.unlink()
    sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW + dt.timedelta(hours=2),
        run_id=run_id("77777777"),
    )
    assert fetcher.calls == [
        ("2026-07-17", "OFF"),
        ("2026-07-17", "OFF"),
    ]


def test_new_correction_reopens_date_and_preserves_superseded_audit(tmp_path):
    cache = tmp_path / "cache"
    first = manifest("2026-07-17", "1")
    store = FixtureStore([first])
    fetcher = FixtureFetcher()
    sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW,
        run_id=run_id("88888888"),
    )
    correction = manifest(
        "2026-07-17", "2", corrections=1,
        published="2026-07-18T05:00:00Z",
    )
    store.releases[correction[0]] = correction[1]
    sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW + dt.timedelta(hours=1),
        run_id=run_id("99999999"),
    )
    row = read_state(cache)["dates"]["2026-07-17"]
    assert row["selected_release_id"] == correction[0]
    assert row["superseded_releases"][0]["selected_release_id"] == first[0]
    assert fetcher.calls == [
        ("2026-07-17", "OFF"),
        ("2026-07-17", "OFF"),
    ]


def test_latest_selection_never_regresses_when_newer_manifest_disappears(tmp_path):
    cache = tmp_path / "cache"
    older = manifest("2026-07-17", "1")
    newer = manifest(
        "2026-07-17", "2", corrections=1,
        published="2026-07-18T05:00:00Z",
    )
    store = FixtureStore([older, newer])
    fetcher = FixtureFetcher()
    sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW,
        run_id=run_id("abababab"),
    )
    del store.releases[newer[0]]

    result = sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW + dt.timedelta(hours=1),
        run_id=run_id("acacacac"),
    )

    row = read_state(cache)["dates"]["2026-07-17"]
    assert row["selected_release_id"] == newer[0]
    assert result["outcome"] == "RETRY_REQUIRED"
    assert fetcher.calls == [("2026-07-17", "OFF")]


def test_known_date_cannot_disappear_from_inventory_silently(tmp_path):
    cache = tmp_path / "cache"
    one = manifest("2026-07-17", "1")
    store = FixtureStore([one])
    fetcher = FixtureFetcher()
    sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW,
        run_id=run_id("adadadad"),
    )
    store.releases.clear()

    result = sync.sync_once(
        store=store,
        cache_root=cache,
        validator=validator,
        fetcher=fetcher,
        now=NOW + dt.timedelta(hours=1),
        run_id=run_id("aeaeaeae"),
    )

    assert result["outcome"] == "RETRY_REQUIRED"
    state = read_state(cache)
    assert state["dates"]["2026-07-17"]["status"] == "VERIFIED"
    assert "SELECTED_RELEASE_ABSENT" in state["discovery_errors"][one[0]]["error"]


def test_invalid_v3_is_loud_and_does_not_block_valid_fetch(tmp_path):
    good = manifest("2026-07-17", "1")
    bad_rid = release_id("2026-07-16", "2")
    bad = (bad_rid, b'{"release_id":"wrong"}')
    fetcher = FixtureFetcher()
    result = sync.sync_once(
        store=FixtureStore([good, bad]),
        cache_root=tmp_path / "cache",
        validator=validator,
        fetcher=fetcher,
        now=NOW,
        run_id=run_id("aaaaaaaa"),
    )
    assert result["outcome"] == "RETRY_REQUIRED"
    assert fetcher.calls == [("2026-07-17", "OFF")]
    state = read_state(tmp_path / "cache")
    assert bad_rid in state["discovery_errors"]


def test_receipt_is_immutable_and_status_binds_its_sha(tmp_path):
    cache = tmp_path / "cache"
    rid = run_id("bbbbbbbb")
    result = sync.sync_once(
        store=FixtureStore([manifest("2026-07-17", "1")]),
        cache_root=cache,
        validator=validator,
        fetcher=FixtureFetcher(),
        now=NOW,
        run_id=rid,
    )
    receipt = Path(result["receipt"])
    status = json.loads(
        (cache / ".research-cache-sync" / "STATUS.json").read_text()
    )
    assert status["receipt_sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert json.loads(receipt.read_text())["lookback_is_not_retention"] is True
    with pytest.raises(sync.CacheSyncError, match="immutable receipt"):
        sync.sync_once(
            store=FixtureStore([manifest("2026-07-17", "1")]),
            cache_root=cache,
            validator=validator,
            fetcher=FixtureFetcher(),
            now=NOW,
            run_id=rid,
        )


def test_corrupt_state_fails_closed_without_reset(tmp_path):
    cache = tmp_path / "cache"
    root = sync._sync_root(cache)
    state = root / "STATE.json"
    state.write_text('{"schema":"wrong","dates":{}}', encoding="utf-8")
    before = state.read_bytes()
    with pytest.raises(sync.CacheSyncError, match="schema"):
        sync.sync_once(
            store=FixtureStore([manifest("2026-07-17", "1")]),
            cache_root=cache,
            validator=validator,
            fetcher=FixtureFetcher(),
            now=NOW,
            run_id=run_id("cccccccc"),
        )
    assert state.read_bytes() == before


def test_exclusive_lock_refuses_overlap(tmp_path):
    root = sync._sync_root(tmp_path / "cache")
    with sync.exclusive_lock(root):
        with pytest.raises(sync.AlreadyRunning):
            with sync.exclusive_lock(root):
                pass


def test_canonical_fetcher_forces_rfq_off(monkeypatch, tmp_path):
    row = {
        "date": "2026-07-17",
        "selected_release_id": release_id("2026-07-17", "1"),
        "manifest_sha256": "1" * 64,
        "manifest_version_id": "manifest-version",
        "reference_set_sha256": "2" * 64,
        "publication_state_sha256": "3" * 64,
    }
    observed = {}

    def fake_fetch(store, cache, rid, with_rfq, allow_legacy=False):
        observed.update({
            "store": store,
            "cache": cache,
            "rid": rid,
            "with_rfq": with_rfq,
            "allow_legacy": allow_legacy,
        })
        marker = marker_for({**row, "rfq_included_in_manifest": True})
        path = Path(cache) / "releases" / rid / ".VERIFIED.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(marker))
        return 0

    monkeypatch.setattr(sync.rd, "cmd_fetch", fake_fetch)
    store = object()
    marker = sync.canonical_fetcher(store, tmp_path / "cache", row)
    assert observed["with_rfq"] is False
    assert observed["allow_legacy"] is False
    assert marker["rfq_status"] == "NOT_FETCHED_OPT_IN"


def test_offline_real_canonical_fixture_uses_exact_versions_and_skips_rfq(tmp_path):
    """Exercise discovery + the existing canonical fetch without AWS/network."""
    helper_path = ROOT / "tests" / "test_research_reference_consumer.py"
    spec = importlib.util.spec_from_file_location(
        "research_reference_consumer_fixture_for_sync", helper_path
    )
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    release = helper.build_release(with_rfq=True)
    store = helper._store_for(release)
    cache = tmp_path / "cache"

    result = sync.sync_once(
        store=store,
        cache_root=cache,
        lookback_days=35,
        now=NOW,
        run_id=run_id("dddddddd"),
    )

    assert result["outcome"] == "PASS"
    rfq_keys = {
        item["source_key"]
        for item in release[1]["objects"]
        if item["kind"] == "rfq"
    }
    assert all(call[1] not in rfq_keys for call in store.source_calls)
    assert all(call in release[2] for call in store.source_calls)
    marker_value = json.loads(
        (cache / "releases" / release[0] / ".VERIFIED.json").read_text()
    )
    assert marker_value["rfq_status"] == "NOT_FETCHED_OPT_IN"


def test_production_script_py_compiles_and_canonical_fetch_signature_is_exact(tmp_path):
    py_compile.compile(
        str(W09 / "research_cache_sync.py"),
        cfile=str(tmp_path / "research_cache_sync.pyc"),
        doraise=True,
    )
    assert str(inspect.signature(sync.rd.cmd_fetch)) == (
        "(store, cache, rid, with_rfq, allow_legacy=False)"
    )


def test_systemd_unit_is_hourly_persistent_read_only_and_payload_is_pinned():
    service = (W09 / "w09-research-cache-sync.service").read_text()
    timer = (W09 / "w09-research-cache-sync.timer").read_text()
    assert "research_cache_sync.py" in service
    assert (
        "ExecCondition=/usr/bin/sha256sum -c "
        "/etc/w09/research_cache_sync.sha256"
    ) in service
    assert "--lookback-days 35" in service
    assert "--with-rfq" not in service
    assert "AWS_ACCESS_KEY_ID" not in service
    assert "ReadWritePaths=/srv/w09-research/cache" in service
    assert "NoNewPrivileges=yes" in service
    assert "OnUnitInactiveSec=1h" in timer
    assert "Persistent=true" in timer

    rows = {}
    for line in (W09 / "research_cache_sync_payload.sha256").read_text().splitlines():
        digest, relative = line.split("  ", 1)
        rows[relative] = digest
    expected = {
        "deploy/w09/research_cache_sync.py",
        "deploy/w09/w09-research-cache-sync.service",
        "deploy/w09/w09-research-cache-sync.timer",
    }
    assert set(rows) == expected
    for relative, digest in rows.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest


def test_install_wiring_verifies_and_installs_but_does_not_enable_sync():
    install = (W09 / "install_on_host.sh").read_text()
    push = (W09 / "push_and_install.sh").read_text()

    for script in (W09 / "install_on_host.sh", W09 / "push_and_install.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)

    assert "research_cache_sync_payload.sha256" in install
    assert "research cache sync payload mismatch" in install
    assert (
        'install -m 0755 "$PAYLOAD_ROOT/deploy/w09/research_cache_sync.py"'
        in install
    )
    assert "/etc/systemd/system/w09-research-cache-sync.service" in install
    assert "/etc/systemd/system/w09-research-cache-sync.timer" in install
    assert "> /etc/w09/research_cache_sync.sha256" in install
    assert "sha256sum -c /etc/w09/research_cache_sync.sha256" in install
    assert "python" in install and "py_compile.compile" in install
    assert "inspect.signature(rd.cmd_fetch)" in install
    assert "systemctl daemon-reload" in install
    assert "systemctl disable --now w09-research-cache-sync.timer" in install
    assert "systemctl enable --now w09-research-cache-sync.timer" not in install
    assert "systemctl start w09-research-cache-sync" not in install
    assert (
        "systemctl is-enabled w09-research-cache-sync.timer" in install
    )

    assert 'CACHE_SYNC_MANIFEST="$HERE/research_cache_sync_payload.sha256"' in push
    assert 'cp "$CACHE_SYNC_MANIFEST" "$tmp/deploy/w09/"' in push
    assert "research_cache_sync.py" in push
    assert "w09-research-cache-sync.service" in push
    assert "w09-research-cache-sync.timer" in push
    assert "py_compile.compile" in push
    assert "inspect.signature(rd.cmd_fetch)" in push
