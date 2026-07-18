"""Catalog/dim generation, lock, rollback, and tamper contracts."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import build_classification  # noqa: E402
import catalog_sync  # noqa: E402
import dim_snapshot  # noqa: E402
import canonical_receipts as cr  # noqa: E402
import forward_canonical_receipts as fcr  # noqa: E402
import publication_generation as pg  # noqa: E402


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _install_manifest(warehouse, manifest):
    path = pg.manifest_path(
        str(warehouse), manifest["group"], manifest["date"])
    payload = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    pg._atomic_manifest_bytes(path, payload)


def test_atomic_manifest_keeps_publisher_read_mask(tmp_path):
    path = tmp_path / "warehouse" / pg.MANIFEST_DIR / "catalog.json"
    pg._atomic_manifest_bytes(str(path), b"{}\n")

    assert path.stat().st_mode & 0o777 == 0o640
    assert path.parent.stat().st_mode & 0o777 == 0o750


def test_transaction_failure_restores_old_complete_generation(tmp_path):
    warehouse = tmp_path / "warehouse"
    paths = ["catalog/a/data", "catalog/b/data"]
    _write(warehouse / paths[0], b"old-a")
    _write(warehouse / paths[1], b"old-b")
    old = pg.build_manifest(
        "catalog", pg.attest_files(str(warehouse), paths))
    _install_manifest(warehouse, old)

    stage = tmp_path / "stage"
    _write(stage / paths[0], b"new-a")
    _write(stage / paths[1], b"new-b")
    new = pg.build_manifest("catalog", pg.attest_files(str(stage), paths))
    calls = []

    def fail_second(src, dst):
        calls.append((src, dst))
        if len(calls) == 2:
            raise OSError("injected second replace failure")
        os.replace(src, dst)

    with pytest.raises(OSError, match="second replace"):
        pg.publish_transaction(
            str(warehouse),
            {rel: str(stage / rel) for rel in paths}, new,
            replace_func=fail_second)

    assert (warehouse / paths[0]).read_bytes() == b"old-a"
    assert (warehouse / paths[1]).read_bytes() == b"old-b"
    assert pg.load_manifest(
        str(warehouse), "catalog", expected_paths=paths) == old


def test_successful_transaction_publishes_manifest_last(tmp_path):
    warehouse = tmp_path / "warehouse"
    stage = tmp_path / "stage"
    paths = ["catalog/a/data", "catalog/b/data"]
    for index, rel in enumerate(paths):
        _write(stage / rel, ("new-%d" % index).encode())
    manifest = pg.build_manifest(
        "catalog", pg.attest_files(str(stage), paths))
    with pg.generation_locks(
            str(warehouse), {"catalog": "exclusive"}, timeout=0):
        pg.publish_transaction(
            str(warehouse),
            {rel: str(stage / rel) for rel in paths}, manifest)
    assert pg.load_manifest(
        str(warehouse), "catalog", expected_paths=paths) == manifest


def test_transaction_rejects_unmanifested_staged_member(tmp_path):
    warehouse = tmp_path / "warehouse"
    stage = tmp_path / "stage"
    bound = "catalog/a/data"
    extra = "catalog/unbound/data"
    _write(stage / bound, b"bound")
    _write(stage / extra, b"must-not-publish")
    manifest = pg.build_manifest(
        "catalog", pg.attest_files(str(stage), [bound]))

    with pytest.raises(pg.GenerationError, match="member set"):
        pg.publish_transaction(
            str(warehouse),
            {bound: str(stage / bound), extra: str(stage / extra)},
            manifest)

    assert not (warehouse / bound).exists()
    assert not (warehouse / extra).exists()


def test_transaction_rejects_alias_with_different_bytes(tmp_path):
    warehouse = tmp_path / "warehouse"
    stage = tmp_path / "stage"
    bound = "dim/snapshots/date=2026-07-16/series.csv"
    alias = "dim/latest/series.csv"
    _write(stage / bound, b"bound")
    _write(stage / alias, b"different")
    manifest = pg.build_manifest(
        "dim", pg.attest_files(str(stage), [bound]),
        date="2026-07-16", source_catalog_generation_id="0" * 64)

    with pytest.raises(pg.GenerationError, match="alias bytes"):
        pg.publish_transaction(
            str(warehouse),
            {bound: str(stage / bound), alias: str(stage / alias)},
            manifest, aliases={alias: bound})


def test_shared_freezer_lock_blocks_both_producer_locks(tmp_path):
    warehouse = str(tmp_path / "warehouse")
    with pg.generation_locks(
            warehouse, {"catalog": "shared", "dim": "shared"}, timeout=0):
        with pytest.raises(pg.GenerationError, match="catalog generation is busy"):
            with pg.generation_locks(
                    warehouse, {"catalog": "exclusive"}, timeout=0):
                pass
        with pytest.raises(pg.GenerationError, match="dim generation is busy"):
            with pg.generation_locks(
                    warehouse, {"dim": "exclusive"}, timeout=0):
                pass


def test_manifest_identity_and_member_hash_tamper_fail_closed(tmp_path):
    warehouse = tmp_path / "warehouse"
    paths = ["catalog/series/part-00000.parquet"]
    _write(warehouse / paths[0], b"catalog-v1")
    manifest = pg.build_manifest(
        "catalog", pg.attest_files(str(warehouse), paths))
    tampered = dict(manifest, generation_id="0" * 64)
    with pytest.raises(pg.GenerationError, match="identity"):
        pg.validate_manifest(
            json.dumps(tampered), str(warehouse), group="catalog")

    _install_manifest(warehouse, manifest)
    (warehouse / paths[0]).write_bytes(b"catalog-v2")
    with pytest.raises(pg.GenerationError, match="bytes do not match"):
        pg.load_manifest(
            str(warehouse), "catalog", expected_paths=paths)


@pytest.mark.parametrize("path", [
    "../catalog/a", "/catalog/a", "catalog/../a", "catalog\\a",
])
def test_generation_paths_reject_escape_and_noncanonical_forms(path):
    with pytest.raises(pg.GenerationError):
        pg.build_manifest("catalog", [{
            "relative_path": path, "size": 1, "sha256": "0" * 64,
        }])


def test_attestation_rejects_symlink_component(tmp_path):
    warehouse = tmp_path / "warehouse"
    outside = tmp_path / "outside"
    _write(outside / "part", b"outside")
    (warehouse / "catalog").mkdir(parents=True)
    (warehouse / "catalog" / "series").symlink_to(
        outside, target_is_directory=True)
    with pytest.raises(pg.GenerationError, match="symlink"):
        pg.attest_files(
            str(warehouse), ["catalog/series/part"])


def test_manifest_control_directory_symlink_is_not_missing_fallback(tmp_path):
    warehouse = tmp_path / "warehouse"
    outside = tmp_path / "outside"
    outside.mkdir()
    warehouse.mkdir()
    (warehouse / pg.MANIFEST_DIR).symlink_to(
        outside, target_is_directory=True)
    with pytest.raises(pg.GenerationError, match="symlink"):
        pg.load_manifest(
            str(warehouse), "catalog", allow_missing=True)


def _write_parquet(path, query):
    import duckdb
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    try:
        con.execute("COPY (%s) TO '%s' (FORMAT PARQUET)" %
                    (query, str(path).replace("'", "''")))
    finally:
        con.close()


def _catalog_fixture(warehouse):
    _write_parquet(
        warehouse / "catalog/series/part-00000.parquet",
        "SELECT 'KXNFL' AS ticker, 'Sports' AS category, "
        "['Football'] AS tags, 'NFL' AS title")
    _write_parquet(
        warehouse / "catalog/events/part-00000.parquet",
        "SELECT 'EV-1' AS event_ticker, true AS mutually_exclusive")
    _write_parquet(
        warehouse / "catalog/markets/part-00000.parquet",
        "SELECT 'M-1' AS ticker, 'EV-1' AS event_ticker, "
        "1.0 AS floor_strike")


def test_dim_generation_binds_source_catalog_and_whole_date_group(tmp_path):
    warehouse = tmp_path / "warehouse"
    _catalog_fixture(warehouse)
    catalog_paths = sorted(
        "catalog/%s/part-00000.parquet" % name
        for name in ("series", "events", "markets"))
    catalog_manifest = pg.build_manifest(
        "catalog", pg.attest_files(str(warehouse), catalog_paths))
    _install_manifest(warehouse, catalog_manifest)

    assert dim_snapshot.main([
        "dim_snapshot", "--warehouse", str(warehouse),
        "--date", "2026-07-16",
    ]) == 0
    dim_paths = [
        "dim/snapshots/date=2026-07-16/%s.csv" % name
        for name in ("series", "events", "markets")]
    manifest = pg.load_manifest(
        str(warehouse), "dim", date="2026-07-16",
        expected_paths=dim_paths)
    assert manifest["source_catalog_generation_id"] == \
        catalog_manifest["generation_id"]
    assert len(manifest["files"]) == 3
    assert all((warehouse / path).is_file() for path in dim_paths)


def test_forward_generation_witness_contract_replaces_timestamp_resolution():
    date = "2026-07-16"
    contract = fcr._generation_witness_contract({
        "sha256": "a" * 64,
        "sealed_at": "2026-07-17T02:00:00Z",
    }, date, "kalshi-vault-fixture", "ec2")

    assert contract["seal_sealed_at_utc"] == "2026-07-17T02:00:00Z"
    assert contract["seal_sha256"] == "a" * 64
    assert contract["discovery_rule"] == \
        "EXACTLY_ONE_CONTENT_ADDRESSED_WITNESS"
    assert contract["read_rule"] == \
        "HEAD_VERSIONID_THEN_EXACT_GET_NO_FALLBACK"
    assert contract["key_prefix"] == \
        "ec2/control/publication-generations/v1/date=2026-07-16/"


def test_classification_refreshes_the_same_catalog_generation(tmp_path):
    warehouse = tmp_path / "warehouse"
    _catalog_fixture(warehouse)
    catalog_paths = sorted(
        "catalog/%s/part-00000.parquet" % name
        for name in ("series", "events", "markets"))
    before = pg.build_manifest(
        "catalog", pg.attest_files(str(warehouse), catalog_paths))
    _install_manifest(warehouse, before)
    config = tmp_path / "market_classes.yaml"
    config.write_text(
        "class_a_full_l1:\n  - Sports\nclass_b_trades_only:\n")
    review = tmp_path / "classification_review.csv"

    assert build_classification.main([
        "build_classification", "--warehouse", str(warehouse),
        "--config", str(config), "--review", str(review),
    ]) == 0
    after_paths = catalog_paths + [
        "catalog/series_classified/part-00000.parquet"]
    after = pg.load_manifest(
        str(warehouse), "catalog", expected_paths=after_paths)
    assert after["generation_id"] != before["generation_id"]
    assert review.is_file()


def test_catalog_network_failure_never_touches_canonical_generation(
        tmp_path, monkeypatch):
    warehouse = tmp_path / "warehouse"
    old = _write(
        warehouse / "catalog/series/part-00000.parquet", b"old-series")
    calls = []

    def fake_fetch(path, *_args, **_kwargs):
        calls.append(path)
        if len(calls) == 1:
            return ([{"ticker": "S", "category": "Sports"}], False)
        raise RuntimeError("injected network failure")

    monkeypatch.setattr(catalog_sync, "fetch_all", fake_fetch)
    with pytest.raises(RuntimeError, match="network failure"):
        catalog_sync.main([
            "catalog_sync", "--warehouse", str(warehouse),
            "--skip-markets", "--settled-pages", "0",
        ])
    assert old.read_bytes() == b"old-series"
    assert not pg.load_manifest(
        str(warehouse), "catalog", allow_missing=True)


def test_catalog_success_stages_then_publishes_one_complete_generation(
        tmp_path, monkeypatch):
    warehouse = tmp_path / "warehouse"
    for name in ("series", "events", "markets"):
        _write(
            warehouse / ("catalog/%s/part-00000.parquet" % name),
            ("old-%s" % name).encode())

    def fake_fetch(path, *_args, **_kwargs):
        if path == "/series/":
            return ([{"ticker": "S", "category": "Sports"}], False)
        if path == "/events/":
            return ([{"event_ticker": "E", "title": "event"}], False)
        raise AssertionError("unexpected fetch %s" % path)

    monkeypatch.setattr(catalog_sync, "fetch_all", fake_fetch)
    assert catalog_sync.main([
        "catalog_sync", "--warehouse", str(warehouse),
        "--skip-markets", "--settled-pages", "0",
    ]) == 0
    paths = [
        "catalog/%s/part-00000.parquet" % name
        for name in ("series", "events", "markets")]
    manifest = pg.load_manifest(
        str(warehouse), "catalog", expected_paths=paths)
    assert len(manifest["files"]) == 3
    assert (warehouse / paths[2]).read_bytes() == b"old-markets"
