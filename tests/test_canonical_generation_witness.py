import hashlib
import json
import sys
import copy
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import canonical_generation_witness as gw  # noqa: E402
import canonical_receipts as cr  # noqa: E402
import forward_canonical_receipts as fcr  # noqa: E402
import publication_generation as pg  # noqa: E402


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        payload = payload.encode()
    path.write_bytes(payload)
    return payload


def _install_manifest(warehouse, manifest):
    path = Path(pg.manifest_path(
        str(warehouse), manifest["group"], manifest.get("date")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")


def _producer_tree(tmp_path, date="2026-07-17"):
    raw = tmp_path / "raw"
    warehouse = tmp_path / "warehouse"
    raw.mkdir()
    warehouse.mkdir()
    seal = {
        "version": 2, "method": "full_v2", "status": "SEALED",
        "date": date, "sealed_at": "2026-07-18T02:00:00Z",
    }
    seal_raw = _write(
        warehouse / "seals" / f"date={date}.json",
        json.dumps(seal, sort_keys=True, indent=2) + "\n")
    catalog_files = []
    for rel in cr.CATALOG_REQUIRED:
        payload = _write(
            warehouse / "catalog" / rel,
            ("catalog:" + rel).encode())
        catalog_files.append({
            "relative_path": "catalog/" + rel,
            "size": len(payload), "sha256": _sha(payload),
        })
    catalog = pg.build_manifest("catalog", catalog_files)
    _install_manifest(warehouse, catalog)
    dim_files = []
    for name in cr.DIM_REQUIRED:
        rel = f"dim/snapshots/date={date}/{name}"
        payload = _write(warehouse / rel, ("dim:" + name).encode())
        dim_files.append({
            "relative_path": rel, "size": len(payload),
            "sha256": _sha(payload),
        })
    dim = pg.build_manifest(
        "dim", dim_files, date=date,
        source_catalog_generation_id=catalog["generation_id"])
    _install_manifest(warehouse, dim)
    binding = {
        "sha256": _sha(seal_raw), "size": len(seal_raw),
        "_payload": seal_raw,
    }
    return raw, warehouse, seal, binding, catalog, dim


class FakeClient:
    def __init__(self):
        self.bucket = gw.DEFAULT_BUCKET
        self.objects = {}
        self.put_original_calls = []
        self.put_witness_calls = []
        self.identity_calls = 0
        self.histories = {}
        self.clock = 0

    def identity(self, expected):
        assert expected == gw.DEFAULT_PUBLISHER_ARN
        self.identity_calls += 1
        return {"Arn": expected}

    def add(self, key, payload, *, version=None,
            modified="2026-07-18T02:01:00Z"):
        self.clock += 1
        version = version or f"v{self.clock}"
        self.objects.setdefault(key, []).append({
            "VersionId": version, "payload": payload,
            "LastModified": modified,
        })
        return version

    def head(self, key, version_id=None):
        rows = self.objects.get(key) or []
        if not rows:
            raise gw.WitnessError("REMOTE_NOT_FOUND", key)
        if version_id is None:
            row = rows[-1]
        else:
            matches = [row for row in rows
                       if row["VersionId"] == version_id]
            if len(matches) != 1:
                raise gw.WitnessError("REMOTE_NOT_FOUND", key)
            row = matches[0]
        return {
            "VersionId": row["VersionId"],
            "ContentLength": len(row["payload"]),
            "LastModified": row["LastModified"],
        }

    def get_exact(self, key, version_id, output):
        rows = [row for row in self.objects.get(key, [])
                if row["VersionId"] == version_id]
        assert len(rows) == 1
        output.write_bytes(rows[0]["payload"])
        return {"VersionId": version_id,
                "ContentLength": len(rows[0]["payload"])}

    def put_original(self, key, body):
        self.put_original_calls.append(key)
        version = self.add(key, Path(body).read_bytes())
        return {"VersionId": version}

    def put_if_absent(self, key, body):
        self.put_witness_calls.append(key)
        if self.objects.get(key):
            return None
        version = self.add(key, Path(body).read_bytes())
        return {"VersionId": version}

    def list_current(self, prefix):
        return {
            "IsTruncated": False,
            "Contents": [{
                "Key": key, "Size": len(rows[-1]["payload"]),
            } for key, rows in sorted(self.objects.items())
             if key.startswith(prefix)],
        }

    def list_versions(self, key):
        if key in self.histories:
            return self.histories[key]
        rows = self.objects.get(key) or []
        return {
            "key": key, "pages": 1, "rows_seen": len(rows),
            "versions": [{
                "Key": key, "VersionId": row["VersionId"],
                "LastModified": row["LastModified"],
                "Size": len(row["payload"]),
            } for row in rows],
            "delete_markers": [], "complete_history": True,
        }


def _patch_authoritative(monkeypatch, seal, binding):
    receipt = {
        "size": 101, "sha256": "3" * 64,
        "sealed_inventory_sha256": "4" * 64,
    }
    authority = {
        "schema_version": gw.fcr.HISTORICAL_METADATA_AUTHORITY_SCHEMA,
        "state": gw.fcr.HISTORICAL_METADATA_AUTHORITY_STATE,
        "date": seal["date"], "seal_sha256": binding["sha256"],
        "manifest_date_sha256": "5" * 64,
        "manifest_fact_projection_sha256": "9" * 64,
        "raw_files_sha256": "6" * 64,
        "archive_file_stats_sha256": "7" * 64,
        "verified_fact_bytes_sha256": "8" * 64,
        "capture_receipt": receipt,
        "l2_receipt": (copy.deepcopy(receipt)
                       if seal["date"] in {"2026-07-15", "2026-07-16"}
                       else None),
        "local_raw_bytes_read": 0,
        "raw_rebuild_or_download": False,
        "required_followup":
            "EXACT_S3_VERSION_SIZE_AND_SHA256_VERIFICATION",
    }
    binding["historical_metadata_authority"] = authority
    result = (
        seal, binding, b"manifest", "2" * 64, [], [], [])
    monkeypatch.setattr(
        gw.cr, "_authoritative_day_inputs",
        lambda *_a, **_kw: result)
    monkeypatch.setattr(
        gw.fcr, "historical_authoritative_day_inputs",
        lambda *_a, **_kw: result)


def test_future_producer_syncs_original_keys_and_is_idempotent(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, catalog, dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)
    client = FakeClient()

    first = gw.publish_future_generation(
        seal["date"], str(raw), str(warehouse), client)
    first_original_puts = list(client.put_original_calls)
    changed_rel = cr.CATALOG_REQUIRED[0]
    _write(warehouse / "catalog" / changed_rel, b"next-day-catalog-bytes")
    changed_catalog = pg.build_manifest(
        "catalog", pg.attest_files(str(warehouse), [
            "catalog/" + rel for rel in cr.CATALOG_REQUIRED]))
    _install_manifest(warehouse, changed_catalog)
    changed_dim = pg.build_manifest(
        "dim", dim["files"], date=seal["date"],
        source_catalog_generation_id=changed_catalog["generation_id"])
    _install_manifest(warehouse, changed_dim)
    second = gw.publish_future_generation(
        seal["date"], str(raw), str(warehouse), client)

    assert first["state"] == "PRODUCER_GENERATION_WITNESS_READY"
    assert first["catalog_dim_coherence_claim"] is True
    assert first["catalog_generation_id"] == catalog["generation_id"]
    assert first["dim_generation_id"] == dim["generation_id"]
    assert first["original_object_puts"] == 7  # seal + 3 catalog + 3 dim
    assert first["witness_puts"] == 1
    assert second["original_object_puts"] == 0
    assert second["witness_puts"] == 0
    assert second["existing_witness_reused"] is True
    assert second["catalog_generation_id"] == catalog["generation_id"]
    assert second["catalog_generation_id"] != changed_catalog["generation_id"]
    assert client.put_original_calls == first_original_puts
    assert len(client.put_witness_calls) == 1
    assert all(key.startswith("ec2/warehouse/")
               for key in client.put_original_calls)
    assert all(not key.startswith("research/")
               for key in client.put_original_calls + client.put_witness_calls)
    witness_key = first["witness_object"]["key"]
    assert witness_key.startswith(
        f"ec2/control/publication-generations/v1/date={seal['date']}/")
    witness_raw = client.objects[witness_key][-1]["payload"]
    witness = json.loads(witness_raw)
    assert witness_raw == fcr.generation_witness_bytes(witness)
    validated = fcr.validate_generation_witness(
        witness, seal["date"], gw.DEFAULT_BUCKET, gw.DEFAULT_PREFIX,
        binding["sha256"], binding["size"], seal["sealed_at"])
    assert validated["generation_authority"] == \
        fcr.GENERATION_AUTHORITY_PRODUCER
    assert validated["catalog_dim_coherence_claim"] is True
    forged = copy.deepcopy(witness)
    forged["catalog_dim_coherence_claim"] = False
    with pytest.raises(cr.ReceiptError, match="producer coherence claim"):
        fcr.validate_generation_witness(
            forged, seal["date"], gw.DEFAULT_BUCKET, gw.DEFAULT_PREFIX,
            binding["sha256"], binding["size"], seal["sealed_at"])


def test_future_retry_after_first_put_uses_same_durable_generation(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, catalog, dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)
    intention_root = tmp_path / "canonical_receipts" / \
        "generation-witness-intents"

    class CrashOnceClient(FakeClient):
        crashed = False

        def put_original(self, key, body):
            response = super().put_original(key, body)
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("simulated process loss after remote PUT")
            return response

    client = CrashOnceClient()
    with pytest.raises(RuntimeError, match="simulated process loss"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), client,
            intention_root=intention_root)

    # A newer coherent local generation appears before the retry.  Recovery
    # must use only the old persisted snapshot rather than silently switching.
    changed_rel = cr.CATALOG_REQUIRED[0]
    _write(warehouse / "catalog" / changed_rel, b"newer-local-catalog")
    changed_catalog = pg.build_manifest(
        "catalog", pg.attest_files(str(warehouse), [
            "catalog/" + rel for rel in cr.CATALOG_REQUIRED]))
    _install_manifest(warehouse, changed_catalog)
    changed_dim = pg.build_manifest(
        "dim", dim["files"], date=seal["date"],
        source_catalog_generation_id=changed_catalog["generation_id"])
    _install_manifest(warehouse, changed_dim)

    result = gw.publish_future_generation(
        seal["date"], str(raw), str(warehouse), client,
        intention_root=intention_root)

    assert result["state"] == "PRODUCER_GENERATION_WITNESS_READY"
    assert result["catalog_generation_id"] == catalog["generation_id"]
    assert result["dim_generation_id"] == dim["generation_id"]
    assert result["catalog_generation_id"] != changed_catalog["generation_id"]
    witness = json.loads(client.objects[
        result["witness_object"]["key"]][-1]["payload"])
    assert witness["catalog_generation"] == catalog
    assert witness["dim_generation"] == dim

    date_dir = intention_root / f"date={seal['date']}"
    intention_files = list(date_dir.glob("intention-*.json"))
    ready_files = list(date_dir.glob("ready-*.json"))
    snapshot_dirs = list(date_dir.glob("snapshot-*"))
    assert len(intention_files) == len(ready_files) == len(snapshot_dirs) == 1
    assert intention_root.stat().st_mode & 0o777 == 0o700
    assert date_dir.stat().st_mode & 0o777 == 0o700
    assert snapshot_dirs[0].stat().st_mode & 0o777 == 0o700
    for path in date_dir.rglob("*"):
        if path.is_dir():
            assert path.stat().st_mode & 0o777 == 0o700
        else:
            assert path.stat().st_mode & 0o777 == 0o600


def test_future_corrupt_persistent_snapshot_fails_closed_without_put(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, _catalog, _dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)
    intention_root = tmp_path / "canonical_receipts" / \
        "generation-witness-intents"

    class CrashBeforePutClient(FakeClient):
        def put_original(self, key, body):
            raise RuntimeError("stop before first remote mutation")

    first = CrashBeforePutClient()
    with pytest.raises(RuntimeError, match="before first remote mutation"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), first,
            intention_root=intention_root)
    snapshot_file = next(
        path for path in intention_root.rglob("*.json")
        if "snapshot-" in str(path.parent))
    snapshot_file.write_bytes(b"corrupt")
    snapshot_file.chmod(0o600)

    retry = FakeClient()
    with pytest.raises(gw.WitnessError, match="persistent bytes differ"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), retry,
            intention_root=intention_root)
    assert retry.put_original_calls == []
    assert retry.put_witness_calls == []


def test_future_retry_finishes_partial_persistent_snapshot_before_any_put(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, catalog, dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)
    intention_root = tmp_path / "canonical_receipts" / \
        "generation-witness-intents"
    real_snapshot = gw._snapshot_file
    calls = {"count": 0}

    def crash_during_snapshot(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 3:
            partial = Path(args[1])
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial-final-from-power-loss")
            partial.chmod(0o600)
            raise RuntimeError("simulated loss during persistent snapshot")
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(gw, "_snapshot_file", crash_during_snapshot)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="during persistent snapshot"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), client,
            intention_root=intention_root)
    assert client.put_original_calls == []
    assert client.put_witness_calls == []
    assert len(list(intention_root.rglob("intention-*.json"))) == 1
    snapshot_root = next(intention_root.rglob("snapshot-*"))
    stale_temp = snapshot_root / ".tmp-snapshot-stale"
    stale_temp.write_bytes(b"partial-temp-from-power-loss")
    stale_temp.chmod(0o600)

    monkeypatch.setattr(gw, "_snapshot_file", real_snapshot)
    result = gw.publish_future_generation(
        seal["date"], str(raw), str(warehouse), client,
        intention_root=intention_root)
    assert result["catalog_generation_id"] == catalog["generation_id"]
    assert result["dim_generation_id"] == dim["generation_id"]
    assert result["original_object_puts"] == 7
    assert not stale_temp.exists()
    assert len(list(intention_root.rglob("intention-*.json"))) == 1
    assert len(list(intention_root.rglob("ready-*.json"))) == 1


def test_future_unexpected_persistent_snapshot_entry_fails_closed(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, _catalog, _dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)
    intention_root = tmp_path / "canonical_receipts" / \
        "generation-witness-intents"
    real_snapshot = gw._snapshot_file

    def stop_after_snapshot_directory(*_args, **_kwargs):
        raise RuntimeError("stop with incomplete snapshot tree")

    monkeypatch.setattr(gw, "_snapshot_file", stop_after_snapshot_directory)
    first = FakeClient()
    with pytest.raises(RuntimeError, match="incomplete snapshot tree"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), first,
            intention_root=intention_root)
    snapshot_root = next(intention_root.rglob("snapshot-*"))
    foreign = snapshot_root / "foreign-entry"
    foreign.write_bytes(b"not part of the intention")
    foreign.chmod(0o600)

    monkeypatch.setattr(gw, "_snapshot_file", real_snapshot)
    retry = FakeClient()
    with pytest.raises(gw.WitnessError, match="unexpected snapshot entry"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), retry,
            intention_root=intention_root)
    assert retry.put_original_calls == []
    assert retry.put_witness_calls == []


def test_future_pre_ready_partial_can_rebuild_new_coherent_generation(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, catalog, dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)
    intention_root = tmp_path / "canonical_receipts" / \
        "generation-witness-intents"
    real_snapshot = gw._snapshot_file
    calls = {"count": 0}

    def partial_then_power_loss(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            partial = Path(args[1])
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"pre-ready-partial")
            partial.chmod(0o600)
            raise RuntimeError("pre-ready power loss")
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(gw, "_snapshot_file", partial_then_power_loss)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="pre-ready power loss"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), client,
            intention_root=intention_root)
    assert client.put_original_calls == []
    old_intention = next(intention_root.rglob("intention-*.json")).name

    changed_rel = cr.CATALOG_REQUIRED[0]
    _write(warehouse / "catalog" / changed_rel, b"new-pre-ready-generation")
    changed_catalog = pg.build_manifest(
        "catalog", pg.attest_files(str(warehouse), [
            "catalog/" + rel for rel in cr.CATALOG_REQUIRED]))
    _install_manifest(warehouse, changed_catalog)
    changed_dim = pg.build_manifest(
        "dim", dim["files"], date=seal["date"],
        source_catalog_generation_id=changed_catalog["generation_id"])
    _install_manifest(warehouse, changed_dim)
    monkeypatch.setattr(gw, "_snapshot_file", real_snapshot)

    result = gw.publish_future_generation(
        seal["date"], str(raw), str(warehouse), client,
        intention_root=intention_root)
    assert result["catalog_generation_id"] == changed_catalog["generation_id"]
    assert result["catalog_generation_id"] != catalog["generation_id"]
    assert result["dim_generation_id"] == changed_dim["generation_id"]
    assert result["original_object_puts"] == 7
    intentions = list(intention_root.rglob("intention-*.json"))
    assert len(intentions) == 1
    assert intentions[0].name != old_intention


def test_future_network_io_occurs_only_after_generation_locks_release(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, _catalog, _dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)
    active = {"locks": False}
    real_locks = gw.pg.generation_locks

    @contextmanager
    def tracked_locks(*args, **kwargs):
        with real_locks(*args, **kwargs):
            active["locks"] = True
            try:
                yield
            finally:
                active["locks"] = False

    class GuardedClient(FakeClient):
        def _unlocked(self):
            assert active["locks"] is False

        def head(self, *args, **kwargs):
            self._unlocked()
            return super().head(*args, **kwargs)

        def get_exact(self, *args, **kwargs):
            self._unlocked()
            return super().get_exact(*args, **kwargs)

        def put_original(self, *args, **kwargs):
            self._unlocked()
            return super().put_original(*args, **kwargs)

        def put_if_absent(self, *args, **kwargs):
            self._unlocked()
            return super().put_if_absent(*args, **kwargs)

        def list_current(self, *args, **kwargs):
            self._unlocked()
            return super().list_current(*args, **kwargs)

    monkeypatch.setattr(gw.pg, "generation_locks", tracked_locks)
    result = gw.publish_future_generation(
        seal["date"], str(raw), str(warehouse), GuardedClient())
    assert result["state"] == "PRODUCER_GENERATION_WITNESS_READY"


def test_future_producer_rejects_dim_catalog_generation_mismatch_before_put(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, _catalog, dim = _producer_tree(tmp_path)
    bad = pg.build_manifest(
        "dim", dim["files"], date=seal["date"],
        source_catalog_generation_id="f" * 64)
    _install_manifest(warehouse, bad)
    _patch_authoritative(monkeypatch, seal, binding)
    client = FakeClient()

    with pytest.raises(gw.WitnessError, match="dim/catalog"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), client)
    assert client.put_original_calls == []
    assert client.put_witness_calls == []


def test_future_producer_does_not_treat_authorization_failure_as_missing(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, _catalog, _dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)

    class DeniedClient(FakeClient):
        def head(self, key, version_id=None):
            raise gw.WitnessError("AWS_COMMAND_FAILED", "AccessDenied")

    client = DeniedClient()
    with pytest.raises(gw.WitnessError, match="AccessDenied"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), client)
    assert client.put_original_calls == []
    assert client.put_witness_calls == []


def test_future_producer_cannot_rebuild_preexisting_historical_dates(tmp_path):
    client = FakeClient()
    with pytest.raises(gw.WitnessError, match="requires no rebuild"):
        gw.publish_future_generation(
            "2026-07-14", str(tmp_path), str(tmp_path), client)
    assert client.identity_calls == 0


def test_witness_prefix_conflict_blocks_instead_of_creating_second_witness(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, _catalog, _dim = _producer_tree(tmp_path)
    _patch_authoritative(monkeypatch, seal, binding)
    client = FakeClient()
    first = gw.publish_future_generation(
        seal["date"], str(raw), str(warehouse), client)
    calls = len(client.put_witness_calls)
    prefix = fcr.generation_witness_prefix(
        gw.DEFAULT_PREFIX, seal["date"]) + "/"
    client.add(prefix + "witness-" + "0" * 64 + ".json", b"conflict")

    with pytest.raises(gw.WitnessError, match="multiple witnesses"):
        gw.publish_future_generation(
            seal["date"], str(raw), str(warehouse), client)
    assert len(client.put_witness_calls) == calls
    assert first["witness_object"]["key"] in client.objects


def _legacy_tree(tmp_path, monkeypatch, date="2026-07-15"):
    raw = tmp_path / "raw"
    warehouse = tmp_path / "warehouse"
    raw.mkdir()
    warehouse.mkdir()
    seal = {
        "version": 2, "method": "full_v2", "status": "SEALED",
        "date": date, "sealed_at": "2026-07-16T02:00:00Z",
    }
    seal_raw = _write(
        warehouse / "seals" / f"date={date}.json",
        json.dumps(seal, sort_keys=True, indent=2) + "\n")
    binding = {"sha256": _sha(seal_raw), "size": len(seal_raw),
               "_payload": seal_raw}
    _patch_authoritative(monkeypatch, seal, binding)
    client = FakeClient()
    seal_key = f"ec2/warehouse/seals/date={date}.json"
    client.add(
        seal_key, seal_raw, version="seal-v1",
        # Deliberately unrelated to catalog selection.  Migration authority
        # comes from the local seal.sealed_at, never this upload timestamp.
        modified="2026-07-20T23:59:59Z")
    minute = gw.LEGACY_EXPECTED_BATCH_MINUTES[date]
    for number, spec in enumerate(gw._catalog_specs()):
        versions = []
        if spec["required"]:
            payload = ("legacy:" + spec["relative_path"]).encode()
            modified = minute + f":0{number}Z"
            version = f"catalog-v{number}"
            client.add(spec["key"], payload, version=version,
                       modified=modified)
            versions.append({
                "Key": spec["key"], "VersionId": version,
                "LastModified": modified, "Size": len(payload),
            })
        client.histories[spec["key"]] = {
            "key": spec["key"], "pages": 1,
            "rows_seen": len(versions), "versions": versions,
            "delete_markers": [], "complete_history": True,
        }
    for name in cr.DIM_REQUIRED:
        key = f"ec2/warehouse/dim/snapshots/date={date}/{name}"
        client.add(key, ("legacy-dim:" + name).encode(),
                   version="dim-" + name,
                   modified="2026-07-19T04:00:00Z")
    return raw, warehouse, seal, binding, client


def test_legacy_migration_reads_history_and_writes_only_small_witness(
        tmp_path, monkeypatch):
    raw, warehouse, seal, binding, client = _legacy_tree(
        tmp_path, monkeypatch)
    proof_root = tmp_path / "proofs"

    result = gw.migrate_legacy_generation(
        seal["date"], str(raw), str(warehouse),
        str(tmp_path / "quality"), str(proof_root), client)

    assert result["state"] == "LEGACY_GENERATION_WITNESS_READY"
    assert result["catalog_dim_coherence_claim"] is False
    assert result["original_object_puts"] == 0
    assert result["witness_puts"] == 1
    assert client.put_original_calls == []
    assert len(client.put_witness_calls) == 1
    assert Path(result["history_proof"]).is_file()
    proof = json.loads(Path(result["history_proof"]).read_text())
    assert proof["seal_sealed_at_utc"] == seal["sealed_at"]
    assert proof["known_batch_minute_utc"] == "2026-07-17T03:10"
    authority = binding["historical_metadata_authority"]
    assert proof["historical_metadata_authority"] == authority
    assert proof["historical_metadata_authority_sha256"] == \
        cr.canonical_sha256(authority)
    assert result["historical_metadata_authority_sha256"] == \
        cr.canonical_sha256(authority)
    assert all(row["complete_history"] is True
               for row in proof["catalog_histories"])
    witness_key = result["witness_object"]["key"]
    witness_raw = client.objects[witness_key][-1]["payload"]
    witness = json.loads(witness_raw)
    validated = fcr.validate_generation_witness(
        witness, seal["date"], gw.DEFAULT_BUCKET, gw.DEFAULT_PREFIX,
        binding["sha256"], binding["size"], seal["sealed_at"])
    assert validated["generation_authority"] == \
        fcr.GENERATION_AUTHORITY_LEGACY
    assert validated["catalog_dim_coherence_claim"] is False
    assert (validated["dim_generation"]["source_catalog_generation_id"]
            != validated["catalog_generation"]["generation_id"])
    assert "CATALOG_DIM_COHERENCE_NOT_CLAIMED" in \
        validated["legacy_migration"]["limitations"]
    assert "DATED_DIM_AND_SELECTED_CATALOG_ARE_MIGRATION_COMBINATION_ONLY" \
        in validated["legacy_migration"]["limitations"]
    assert validated["seal"]["LastModified"] == "2026-07-20T23:59:59Z"
    forged_claim = copy.deepcopy(witness)
    forged_claim["catalog_dim_coherence_claim"] = True
    with pytest.raises(cr.ReceiptError, match="must deny"):
        fcr.validate_generation_witness(
            forged_claim, seal["date"], gw.DEFAULT_BUCKET, gw.DEFAULT_PREFIX,
            binding["sha256"], binding["size"], seal["sealed_at"])
    forged_link = copy.deepcopy(witness)
    forged_link["dim_generation"] = pg.build_manifest(
        "dim", forged_link["dim_generation"]["files"], date=seal["date"],
        source_catalog_generation_id=
            forged_link["catalog_generation"]["generation_id"])
    with pytest.raises(cr.ReceiptError, match="falsely claims"):
        fcr.validate_generation_witness(
            forged_link, seal["date"], gw.DEFAULT_BUCKET, gw.DEFAULT_PREFIX,
            binding["sha256"], binding["size"], seal["sealed_at"])

    # Later catalog versions change live history, but a terminal exact legacy
    # witness must be reused before any history reconstruction or new PUT.
    spec = gw._catalog_specs()[0]
    later_payload = b"later-catalog-version"
    later_version = client.add(
        spec["key"], later_payload, version="later-catalog-v1",
        modified="2026-07-20T04:00:00Z")
    client.histories[spec["key"]]["versions"].append({
        "Key": spec["key"], "VersionId": later_version,
        "LastModified": "2026-07-20T04:00:00Z",
        "Size": len(later_payload),
    })
    client.histories[spec["key"]]["rows_seen"] += 1
    witness_calls = len(client.put_witness_calls)
    client.list_versions = lambda *_a, **_kw: pytest.fail(
        "terminal witness reuse listed catalog history")

    repeated = gw.migrate_legacy_generation(
        seal["date"], str(raw), str(warehouse),
        str(tmp_path / "quality"), str(proof_root), client)
    assert repeated["existing_witness_reused"] is True
    assert repeated["original_object_puts"] == 0
    assert repeated["witness_puts"] == 0
    assert repeated["history_proof_sha256"] == result[
        "history_proof_sha256"]
    assert len(client.put_witness_calls) == witness_calls


def test_legacy_migration_blocks_an_earlier_complete_catalog_batch(
        tmp_path, monkeypatch):
    raw, warehouse, seal, _binding, client = _legacy_tree(
        tmp_path, monkeypatch)
    for number, spec in enumerate(
            row for row in gw._catalog_specs() if row["required"]):
        payload = ("earlier:" + spec["relative_path"]).encode()
        modified = f"2026-07-17T03:00:0{number}Z"
        version = "earlier-v" + str(number)
        client.add(spec["key"], payload, version=version, modified=modified)
        client.histories[spec["key"]]["versions"].insert(0, {
            "Key": spec["key"], "VersionId": version,
            "LastModified": modified, "Size": len(payload),
        })
        client.histories[spec["key"]]["rows_seen"] += 1

    with pytest.raises(gw.WitnessError, match="earlier complete"):
        gw.migrate_legacy_generation(
            seal["date"], str(raw), str(warehouse),
            str(tmp_path / "quality"), str(tmp_path / "proofs"), client)
    assert client.put_original_calls == []
    assert client.put_witness_calls == []


def test_legacy_authority_tamper_blocks_before_any_s3_object_operation(
        tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    warehouse = tmp_path / "warehouse"
    raw.mkdir()
    warehouse.mkdir()

    def tampered_authority(*_args, **_kwargs):
        raise cr.ReceiptError(
            "HISTORICAL_METADATA_AUTHORITY_BLOCKED",
            "capture receipt tampered")

    monkeypatch.setattr(
        gw.fcr, "historical_authoritative_day_inputs", tampered_authority)

    class NoS3ObjectClient(FakeClient):
        def head(self, *_args, **_kwargs):
            pytest.fail("HEAD occurred before authority verification")

        def get_exact(self, *_args, **_kwargs):
            pytest.fail("GET occurred before authority verification")

        def list_versions(self, *_args, **_kwargs):
            pytest.fail("LIST versions occurred before authority verification")

        def list_current(self, *_args, **_kwargs):
            pytest.fail("LIST current occurred before authority verification")

        def put_original(self, *_args, **_kwargs):
            pytest.fail("PUT occurred before authority verification")

        def put_if_absent(self, *_args, **_kwargs):
            pytest.fail("witness PUT occurred before authority verification")

    with pytest.raises(gw.WitnessError, match="capture receipt tampered"):
        gw.migrate_legacy_generation(
            "2026-07-15", str(raw), str(warehouse),
            str(tmp_path / "quality"), str(tmp_path / "proofs"),
            NoS3ObjectClient())


def test_legacy_migration_is_restricted_to_exactly_four_dates(tmp_path):
    client = FakeClient()
    with pytest.raises(gw.WitnessError, match="restricted to four dates"):
        gw.migrate_legacy_generation(
            "2026-07-14", str(tmp_path), str(tmp_path),
            str(tmp_path / "quality"), str(tmp_path / "proofs"), client)
    assert client.identity_calls == 0


def test_aws_surface_has_no_copy_delete_tag_or_research_write_command():
    source = Path(gw.__file__).read_text()
    for forbidden in (
            '"copy-object"', '"delete-object"', '"delete-objects"',
            '"put-object-tagging"', '"research/"'):
        assert forbidden not in source


def test_sterile_publisher_environment_drops_trading_proxy_and_endpoint(
        monkeypatch):
    for name in list(gw.os.environ):
        if name.startswith("AWS_") or name in {
                "HTTPS_PROXY", "HTTP_PROXY", "KALSHI_PRIVATE_KEY_PATH"}:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "publisher")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://attacker.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "/trading/key")

    env = gw._sterile_publisher_environment()

    assert env["AWS_ACCESS_KEY_ID"] == "publisher"
    assert env["AWS_SECRET_ACCESS_KEY"] == "secret"
    assert "AWS_ENDPOINT_URL" not in env
    assert "HTTPS_PROXY" not in env
    assert "KALSHI_PRIVATE_KEY_PATH" not in env
    assert env["HOME"] == "/nonexistent"
    assert env["AWS_CONFIG_FILE"] == "/dev/null"
    assert env["AWS_SHARED_CREDENTIALS_FILE"] == "/dev/null"
    assert env["AWS_CLI_HISTORY_FILE"] == "/dev/null"
    assert env["AWS_CLI_HISTORY_ENABLED"] == "false"
    assert env["AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"] == "true"
    assert env["AWS_CLI_AUTO_PROMPT"] == "off"
    assert env["AWS_STS_REGIONAL_ENDPOINTS"] == "regional"


def test_real_aws_mutator_pins_binary_authorization_and_clean_commit(
        monkeypatch, tmp_path):
    authorization = tmp_path / "authorization"
    commits = {"value": "a" * 40}
    monkeypatch.setattr(gw, "_read_authorization", lambda path: "b" * 64)
    monkeypatch.setattr(
        gw, "_clean_mutation_provenance", lambda: commits["value"])
    monkeypatch.setattr(gw, "_sterile_publisher_environment", lambda: {})
    with pytest.raises(gw.WitnessError, match="path is fixed"):
        gw.AwsCli("/usr/bin/aws", gw.DEFAULT_BUCKET, authorization)

    client = gw.AwsCli(
        gw.PINNED_AWS_CLI, gw.DEFAULT_BUCKET, authorization)
    client.identity_verified = True
    monkeypatch.setattr(client, "_run", lambda *_a, **_kw: {"VersionId": "v1"})
    body = _write(tmp_path / "body", b"small")
    assert client.put_if_absent("ec2/control/example", body) == {
        "VersionId": "v1"}
    commits["value"] = "c" * 40
    with pytest.raises(gw.WitnessError, match="HEAD changed"):
        client.put_if_absent("ec2/control/example-2", body)


def test_exact_operator_authorization_artifact_is_hash_pinned(monkeypatch):
    artifact = (ROOT / "docs" / "plan_releases" / "pipeline"
                / "W-PUB-REF-01C_AUTOMATION_EXECUTION_AUTHORIZATION_2026-07-17.json")
    monkeypatch.setattr(gw, "DEFAULT_AUTHORIZATION_FILE", artifact)
    assert gw._read_authorization(artifact) == \
        gw.AUTOMATION_AUTHORIZATION_SHA256


def test_real_producer_host_lock_fails_closed_when_already_held(
        tmp_path, monkeypatch):
    parent = tmp_path / "canonical_receipts"
    parent.mkdir()
    lock = parent / "generation-witness.lock"
    monkeypatch.setattr(gw, "PRODUCTION_WRITER_LOCK", lock)
    client = object.__new__(gw.AwsCli)
    fd = gw.os.open(lock, gw.os.O_RDWR | gw.os.O_CREAT, 0o600)
    gw.os.fchmod(fd, 0o600)
    gw.fcntl.flock(fd, gw.fcntl.LOCK_EX | gw.fcntl.LOCK_NB)
    try:
        with pytest.raises(gw.WitnessError, match="another generation writer"):
            with gw._production_writer_lock(client):
                pytest.fail("contended writer lock was acquired")
    finally:
        gw.fcntl.flock(fd, gw.fcntl.LOCK_UN)
        gw.os.close(fd)
    with gw._production_writer_lock(client):
        pass
