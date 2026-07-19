"""Offline contract tests for the isolated passive RFQ collector."""

import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "rfq_capture.py"


def load_module():
    spec = importlib.util.spec_from_file_location("rfq_capture", TOOL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


rfq = load_module()


def safe_parent(tmp_path):
    key = tmp_path / "key.pem"
    key.write_text("fixture")
    return {
        "KALSHI_ENV": "prod",
        "KALSHI_ALLOW_PROD": "1",
        "KALSHI_API_KEY_SCOPE": "read",
        "KALSHI_API_KEY_ID": "fixture-id",
        "KALSHI_PRIVATE_KEY_PATH": str(key),
        "KALSHI_MODE": "data_collect",
        "KALSHI_ALLOW_LIVE": "1",          # must be scrubbed
        "KALSHI_WS_TICKERS": "SHOULD-GO",  # communications is global
    }


def test_child_environment_is_capture_only_and_global(tmp_path):
    parent = safe_parent(tmp_path)
    parent.update({
        "KALSHI_HOST_UNSAFE_OVERRIDE": "1",
        "KALSHI_BASE_URL": "https://attacker.invalid",
        "KALSHI_WS_URL": "wss://attacker.invalid/ws",
        "KALSHI_WS_SIGN_PATH": "/wrong",
    })
    env = rfq.child_environment(
        parent, tmp_path / "rfq_01.ndjson",
        tmp_path / "metrics.ndjson", 111,
    )
    assert env["KALSHI_MODE"] == "data_collect"
    assert env["KALSHI_WS_CHANNELS"] == "communications"
    assert env["KALSHI_WS_OMIT_USE_YES_PRICE"] == "1"
    assert env["KALSHI_WS_FIREHOSE"] == "1"
    assert env["KALSHI_SHADOW_XCHECK"] == "0"
    assert env["KALSHI_SHADOW_SECONDS"] == "111"
    assert "KALSHI_WS_TICKERS" not in env
    assert "KALSHI_ALLOW_LIVE" not in env
    assert "KALSHI_HOST_UNSAFE_OVERRIDE" not in env
    assert env["KALSHI_BASE_URL"] == "https://external-api.kalshi.com"
    assert env["KALSHI_WS_URL"] == "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    assert env["KALSHI_WS_SIGN_PATH"] == "/trade-api/ws/v2"


@pytest.mark.parametrize("change", [
    {"KALSHI_MODE": "live"},
    {"KALSHI_ENV": "demo"},
    {"KALSHI_ALLOW_PROD": "0"},
    {"KALSHI_API_KEY_SCOPE": "write"},
    {"KALSHI_API_KEY_ID": ""},
    {"KALSHI_PRIVATE_KEY_PATH": "/missing/key"},
])
def test_child_environment_fails_closed(tmp_path, change):
    parent = safe_parent(tmp_path)
    parent.update(change)
    with pytest.raises(rfq.Refused):
        rfq.child_environment(parent, tmp_path / "cap", tmp_path / "met", 30)


def test_hourly_name_and_rotation_boundary():
    import datetime as dt
    now = dt.datetime(2026, 7, 11, 23, 59, 50, tzinfo=dt.timezone.utc)
    path, seconds = rfq.segment(now, Path("/raw"))
    assert str(path).endswith("date=2026-07-11/rfq_23.ndjson")
    assert seconds == 10  # exact rotation; never deliberately crosses midnight
    assert str(rfq.capture_template(Path("/raw"))).endswith(
        "date={UTC_DATE}/rfq_{UTC_HOUR}.ndjson")


def test_dry_run_opens_no_socket_and_prints_exact_subscription(tmp_path):
    env = dict(os.environ, KALSHI_ENV="prod", KALSHI_ALLOW_PROD="1",
               KALSHI_API_KEY_SCOPE="read")
    env.pop("KALSHI_MODE", None)
    p = subprocess.run(
        [sys.executable, str(TOOL), "--dry-run", "--raw-root", str(tmp_path / "raw"),
         "--live-root", str(tmp_path / "live")],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=10,
    )
    assert p.returncode == 0, p.stderr
    assert "no socket opened" in p.stdout
    assert "KALSHI_WS_CHANNELS=communications" in p.stdout
    assert "KALSHI_MODE=data_collect" in p.stdout
    assert "KALSHI_WS_TICKERS=<absent>" in p.stdout
    assert "fresh_lane_state=UNBOUND_DIAGNOSTIC" in p.stdout


def test_explicit_missing_authority_refuses_without_creating_it(tmp_path):
    missing = tmp_path / "control" / "fresh_rfq_epoch_authority.json"
    env = dict(os.environ, KALSHI_ENV="prod", KALSHI_ALLOW_PROD="1",
               KALSHI_API_KEY_SCOPE="read")
    p = subprocess.run(
        [sys.executable, str(TOOL), "--dry-run",
         "--raw-root", str(tmp_path / "raw"),
         "--live-root", str(tmp_path / "live"),
         "--fresh-lane-authority", str(missing)],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=10,
    )
    assert p.returncode == 78
    assert "fresh lane authority validation failed" in p.stderr
    assert not missing.exists()


def _fresh_authority_file(tmp_path, monkeypatch, *, t0=None):
    fresh = rfq._fresh_receipts_module()
    precommit = rfq._fresh_precommit_module()

    def sha(label):
        return hashlib.sha256(label.encode()).hexdigest()

    rows = [{
        "key": "raw_rfq/date=2026-06-%02d/rfq_%02d.ndjson.%d" %
               (1 + index // 24, index % 24, 1000 + index),
        "size": index + 1,
        "sha256": sha("old-%d" % index),
    } for index in range(284)]
    monkeypatch.setattr(
        fresh, "OLD_284_OBJECT_SET_SHA256",
        fresh.old_object_set_sha256(rows))
    now = rfq.dt.datetime.now(rfq.dt.timezone.utc).replace(microsecond=0)
    if t0 is None:
        t0_value = (now + rfq.dt.timedelta(hours=2)).replace(
            minute=0, second=0, microsecond=0)
        t0 = t0_value.strftime("%Y-%m-%dT%H:%M:%SZ")
    authority = fresh.build_fresh_epoch_authority(
        operator_authorization_sha256=fresh.OPERATOR_AUTHORIZATION_SHA256,
        strict_t0_utc=t0,
        deployment_commit="a" * 40,
        generation="capture-generation-7",
        created_at_utc=(now - rfq.dt.timedelta(seconds=60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        deny_identities=rows,
    )
    path = tmp_path / "fresh_rfq_epoch_authority.json"
    precommit.precommit_authority(
        authority, path, now=now, expected_owner_uid=os.geteuid())
    return path, authority


def _load_authority(path, authority, **kwargs):
    kwargs.setdefault("expected_owner_uid", os.geteuid())
    kwargs.setdefault("git_probe", lambda _root: authority["deployment_commit"])
    kwargs.setdefault("expected_generation", authority["generation"])
    return rfq.load_fresh_lane_authority(path, **kwargs)


def _strict_v3_segment(authority, provenance):
    fresh = rfq._fresh_receipts_module()
    hour = authority["strict_t0_utc"][:13]
    start = rfq.dt.datetime.strptime(hour, "%Y-%m-%dT%H").replace(
        tzinfo=rfq.dt.timezone.utc)
    start_ns = int(start.timestamp()) * 1_000_000_000
    end_ns = start_ns + 3_600_000_000_000
    date_text, hour_text = hour.split("T")
    relpath = f"date={date_text}/rfq_{hour_text}.ndjson"
    origin = f"/raw/{relpath}"
    ack_wall = provenance["child_subscription_ack_wall_ns"]
    ack_identity = provenance["child_subscription_ack_identity_sha256"]
    shard = {
        "ordinal": 0, "relpath": relpath, "bytes_before": 0,
        "size": 100, "parsed_bytes_at_close": 100,
        "sha256": hashlib.sha256(b"fresh shard").hexdigest(),
    }
    receipt = {
        "type": "rfq_segment_receipt", "schema": "rfq-segment-receipt-v3",
        "lane_id": authority["lane_id"],
        "authority_sha256": authority["authority_sha256"],
        "deployment_commit": authority["deployment_commit"],
        "generation": authority["generation"],
        "segment_hour": hour,
        "expected_start_wall_ns": start_ns,
        "expected_end_wall_ns": end_ns,
        "child_started_wall_ns": start_ns - 1_000_000,
        "segment_started_wall_ns": start_ns,
        "receipt_observed_wall_ns": end_ns + 1_000_000,
        "start_lag_ms": 0, "end_early_ms": 0,
        "end_reason": "boundary", "boundary_closed": True,
        "close_stability_ms": 1000, "child_rc": None, "status": "PASS",
        "subscription_proven": True,
        "capture_relpath": relpath, "capture_origin_path": f"/raw/{relpath}",
        "capture_bytes_before": 0, "capture_bytes_at_close": 100,
        "capture_sha256_at_close": shard["sha256"],
        "capture_shards": [shard], "capture_shard_count": 1,
        "capture_shard_set_sha256": fresh.canonical_sha256([shard]),
        "raw_evidence": {
            "recorder_rows": 2, "subscribed_communications": 1,
            "subscription_ack_wall_ns": ack_wall,
            "subscription_ack_identity_sha256": ack_identity,
            "rfq_created": 0, "rfq_deleted": 0,
            "markers": {"hour_open": 1}, "partition_mismatches": 0,
            "max_stream_epoch": 1, "subscription_invalidations": 0,
            "subscription_proven_at_end": True, "findings": [],
            "start_offsets": {origin: 0}, "end_offsets": {origin: 100},
            "shards": [origin],
        },
        "metrics_evidence": {
            "feed_rows": 3600, "connected_valid_rows": 3600,
            "min_reconnects": 0, "max_reconnects": 0,
            "min_disconnects": 0, "max_disconnects": 0,
            "min_errors": 0, "max_errors": 0,
            "max_recorder_dropped": 0, "max_recorder_write_failures": 0,
            "first_ts_ms": start_ns // 1_000_000,
            "last_ts_ms": end_ns // 1_000_000 - 1,
            "findings": [], "end_offset": 1000, "next_window_offset": 1000,
        },
        "findings": [],
        "generated_at_utc": (start + rfq.dt.timedelta(hours=1, seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }
    receipt.update(provenance)
    return receipt


def test_unbound_receipt_provenance_is_diagnostic_only():
    fields = rfq.fresh_lane_receipt_fields(
        None, "2026-07-17T00", supervisor_pid=10, child_pid=20,
        child_generation=3)
    assert fields == {
        "fresh_lane_state": "UNBOUND_DIAGNOSTIC",
        "supervisor_pid": 10, "child_pid": 20, "child_generation": 3,
        "child_subscription_ack_wall_ns": None,
        "child_subscription_ack_identity_sha256": None,
        "child_subscription_ack_count": 0,
    }
    for forbidden in ("lane_id", "authority_sha256", "deployment_commit",
                      "generation", "fresh_eligible", "research_eligible"):
        assert forbidden not in fields
    assert rfq.parse_args([]).fresh_lane_authority is None


def test_valid_authority_binds_receipt_and_pre_t0_stays_diagnostic(
        tmp_path, monkeypatch):
    path, authority = _fresh_authority_file(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "read_bytes", lambda _self: (_ for _ in ()).throw(
        AssertionError("authority must be read from its fixed FD")))
    binding = _load_authority(path, authority)
    assert binding["authority"] == authority
    valid, findings = rfq.fresh_lane_finalize_guard(binding)
    assert valid is True and findings == []
    fields = rfq.fresh_lane_receipt_fields(
        binding, authority["strict_t0_utc"][:13],
        supervisor_pid=11, child_pid=22,
        child_generation=4,
        child_subscription_ack_wall_ns=int(rfq.dt.datetime.strptime(
            authority["strict_t0_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=rfq.dt.timezone.utc).timestamp() * 1e9) + 1_000_000,
        child_subscription_ack_identity_sha256=hashlib.sha256(
            b"persistent-child-ack").hexdigest(),
        child_subscription_ack_count=1)
    assert fields["fresh_lane_state"] == "BOUND_AUTHORITY"
    assert fields["lane_id"] == authority["lane_id"]
    assert fields["authority_sha256"] == authority["authority_sha256"]
    assert fields["deployment_commit"] == authority["deployment_commit"]
    assert fields["generation"] == authority["generation"]
    assert fields["supervisor_pid"] == 11
    assert fields["child_pid"] == 22
    assert fields["child_generation"] == 4
    assert "fresh_eligible" not in fields and "research_eligible" not in fields

    fresh = rfq._fresh_receipts_module()
    strict_segment = _strict_v3_segment(authority, fields)
    validated = fresh.validate_v3_segment_receipt(strict_segment, authority)
    assert validated["authority_sha256"] == authority["authority_sha256"]
    with pytest.raises(fresh.FreshRfqError, match="SEGMENT_SCHEMA"):
        fresh.validate_v3_segment_receipt(
            dict(strict_segment, unexpected_field=True), authority)

    # A persistent socket may have authenticated before this hour.  The child
    # ACK tuple stays bound at top level even when this hour's raw bytes contain
    # no second ACK.
    persisted = json.loads(json.dumps(strict_segment))
    persisted_ack_wall = persisted["expected_start_wall_ns"] - 500_000
    persisted["child_subscription_ack_wall_ns"] = persisted_ack_wall
    persisted["raw_evidence"].update({
        "subscribed_communications": 0,
        "subscription_ack_wall_ns": None,
        "subscription_ack_identity_sha256": None,
        "subscription_proven_at_end": None,
    })
    assert fresh.validate_v3_segment_receipt(
        persisted, authority)["child_subscription_ack_wall_ns"] == \
        persisted_ack_wall

    before_hour = (rfq.dt.datetime.strptime(
        authority["strict_t0_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=rfq.dt.timezone.utc) - rfq.dt.timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H")
    before = rfq.fresh_lane_receipt_fields(
        binding, before_hour, supervisor_pid=11, child_pid=22,
        child_generation=4)
    assert before["fresh_lane_state"] == "BOUND_PRE_T0_DIAGNOSTIC"
    assert "fresh_eligible" not in before and "research_eligible" not in before


def test_authority_loader_rejects_symlink_and_bad_contract(tmp_path, monkeypatch):
    path, authority = _fresh_authority_file(tmp_path, monkeypatch)
    link = tmp_path / "authority-link.json"
    link.symlink_to(path)
    with pytest.raises(rfq.Refused, match="symlink"):
        _load_authority(link, authority)

    bad = tmp_path / "bad-authority.json"
    bad.write_text("{}\n")
    with pytest.raises(rfq.Refused):
        _load_authority(bad, authority)

    monkeypatch.setattr(rfq, "FRESH_AUTHORITY_MAX_BYTES",
                        path.stat().st_size - 1)
    with pytest.raises(rfq.Refused, match="size is outside"):
        _load_authority(path, authority)


def test_loader_rejects_bare_pretty_writable_and_hardlinked_authority(
        tmp_path, monkeypatch):
    path, authority = _fresh_authority_file(tmp_path, monkeypatch)
    envelope = json.loads(path.read_text())

    bare = tmp_path / "bare-authority.json"
    bare.write_bytes(
        rfq._fresh_receipts_module().canonical_bytes(authority) + b"\n")
    bare.chmod(0o444)
    with pytest.raises(rfq.Refused):
        _load_authority(bare, authority)

    pretty = tmp_path / "pretty-envelope.json"
    pretty.write_text(json.dumps(envelope, indent=2) + "\n")
    pretty.chmod(0o444)
    with pytest.raises(rfq.Refused, match="canonical JSON"):
        _load_authority(pretty, authority)

    writable = tmp_path / "writable-envelope.json"
    writable.write_bytes(path.read_bytes())
    writable.chmod(0o644)
    with pytest.raises(rfq.Refused, match="owner/mode/link count"):
        _load_authority(writable, authority)

    hardlink = tmp_path / "hardlink-envelope.json"
    os.link(path, hardlink)
    with pytest.raises(rfq.Refused, match="owner/mode/link count"):
        _load_authority(path, authority)


def test_loader_rejects_unsafe_parent_ancestor_symlink_and_ctime_forgery(
        tmp_path, monkeypatch):
    path, authority = _fresh_authority_file(tmp_path, monkeypatch)
    payload = path.read_bytes()

    unsafe = tmp_path / "unsafe-parent"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    try:
        unsafe_path = unsafe / "authority-envelope.json"
        unsafe_path.write_bytes(payload)
        unsafe_path.chmod(0o444)
        with pytest.raises(rfq.Refused, match="parent owner/mode"):
            _load_authority(unsafe_path, authority)
    finally:
        unsafe.chmod(0o700)

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    real_path = real_parent / "authority-envelope.json"
    real_path.write_bytes(payload)
    real_path.chmod(0o444)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(rfq.Refused):
        _load_authority(linked_parent / real_path.name, authority)

    precommit = rfq._fresh_precommit_module()
    forged = tmp_path / "forged-ctime-envelope.json"
    now = rfq.dt.datetime.now(rfq.dt.timezone.utc).replace(microsecond=0)
    old_envelope = precommit.build_precommit_envelope(
        authority, precommitted_at=now - rfq.dt.timedelta(seconds=30))
    forged.write_bytes(
        rfq._fresh_receipts_module().canonical_bytes(old_envelope) + b"\n")
    forged.chmod(0o444)
    with pytest.raises(rfq.Refused, match="filesystem/precommit T0 timing"):
        _load_authority(forged, authority, now=now)


def test_loader_binds_runtime_commit_and_generation(tmp_path, monkeypatch):
    path, authority = _fresh_authority_file(tmp_path, monkeypatch)
    with pytest.raises(rfq.Refused, match="deployment commit"):
        _load_authority(path, authority, runtime_commit="b" * 40)
    with pytest.raises(rfq.Refused, match="service generation"):
        _load_authority(path, authority, expected_generation="another-generation")


def test_clean_git_head_probe_uses_fixed_argv_without_shell(monkeypatch):
    calls = []
    responses = iter([
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="c" * 40 + "\n", stderr=""),
    ])

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return next(responses)

    monkeypatch.setattr(rfq.subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_DIR", "/tmp/attacker-controlled.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/attacker-controlled-tree")
    assert rfq.probe_clean_git_head(ROOT) == "c" * 40
    assert len(calls) == 2
    assert all(call[1]["shell"] is False for call in calls)
    assert all(call[0][0] == "/usr/bin/git" for call in calls)
    assert all("safe.directory=%s" % ROOT.resolve() in call[0]
               for call in calls)
    assert all("core.fsmonitor=false" in call[0] and
               "core.hooksPath=/dev/null" in call[0]
               for call in calls)
    assert all("GIT_DIR" not in call[1]["env"] and
               "GIT_WORK_TREE" not in call[1]["env"]
               for call in calls)
    assert calls[0][0][-2:] == ["--porcelain=v1", "--untracked-files=all"]
    assert calls[1][0][-3:] == ["rev-parse", "--verify", "HEAD"]


@pytest.mark.parametrize("changed", ["dirty", "head"])
def test_runtime_worktree_change_invalidates_bound_authority(
        tmp_path, monkeypatch, changed):
    path, authority = _fresh_authority_file(tmp_path, monkeypatch)
    calls = 0

    def probe(_root):
        nonlocal calls
        calls += 1
        if calls == 1:
            return authority["deployment_commit"]
        if changed == "dirty":
            raise rfq.Refused("capture worktree is not clean")
        return "b" * 40

    binding = _load_authority(path, authority, git_probe=probe)
    valid, findings = rfq.fresh_lane_finalize_guard(binding)
    assert valid is False
    assert any("capture worktree" in finding for finding in findings)


def test_authority_tamper_blocks_finalize_and_marks_invalid(
        tmp_path, monkeypatch):
    path, authority = _fresh_authority_file(tmp_path, monkeypatch)
    binding = _load_authority(path, authority)
    # Semantically identical JSON with changed file bytes/identity is still a
    # forbidden post-launch authority mutation.
    path.chmod(0o644)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    path.chmod(0o444)
    with pytest.raises(rfq.Refused):
        rfq.revalidate_fresh_lane_authority(binding)
    valid, findings = rfq.fresh_lane_finalize_guard(binding)
    assert valid is False
    assert any("authority invalidated" in finding for finding in findings)
    fields = rfq.fresh_lane_receipt_fields(
        binding, authority["strict_t0_utc"][:13], supervisor_pid=1, child_pid=2,
        child_generation=1, authority_valid=valid)
    assert fields["fresh_lane_state"] == "BOUND_AUTHORITY_INVALID"


def test_child_termination_is_pid_scoped(tmp_path):
    """The disable path calls terminate on its owned Popen, never pgrep/pkill."""
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    sentinel = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        rfq._terminate_child(child, timeout_s=2)
        assert child.poll() is not None
        assert sentinel.poll() is None, "unrelated process was touched"
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        sentinel.terminate()
        sentinel.wait(timeout=5)


def test_kernel_lock_is_crash_safe_and_reacquirable(tmp_path):
    path = tmp_path / "rfq_capture.lock"
    h1 = rfq._acquire_lock(path)
    with pytest.raises(rfq.Refused):
        rfq._acquire_lock(path)
    rfq._release_lock(h1)
    h2 = rfq._acquire_lock(path)  # stale file exists, but kernel lock is gone
    rfq._release_lock(h2)


def test_deploy_contract_is_independent_and_active_files_are_excluded():
    unit = (ROOT / "deploy" / "kalshi-rfq-capture.service").read_text()
    sync = (ROOT / "deploy" / "ec2_s3_sync.sh").read_text()
    oom = (ROOT / "deploy" / "oom_guard.sh").read_text()
    ws = (ROOT / "apps" / "ws_shadow.cpp").read_text()
    ws_client = (ROOT / "src" / "ws_client.cpp").read_text()
    assert "PartOf=kalshi-pipeline" not in unit
    assert "Requires=kalshi-pipeline" not in unit
    assert "tools/rfq_capture.py" in unit
    assert "rfq_readonly.env.sh" in unit
    assert "EnvironmentFile=/etc/hft-bot/kalshi-rfq-fresh-generation.env" in unit
    assert ("--fresh-lane-authority \"/var/lib/kalshi-rfq-fresh/control/"
            "${FRESH_RFQ_GENERATION}/authority-envelope.json\"") in unit
    assert "--fresh-lane-expected-generation \"${FRESH_RFQ_GENERATION}\"" in unit
    assert "--fresh-lane-authority-owner-uid 0" in unit
    assert "WorkingDirectory=/home/ubuntu/hft-bot-rfq-fresh" in unit
    assert "Environment=PATH=/usr/local/bin:/usr/bin:/bin" in unit
    assert "/usr/bin/python3 tools/rfq_capture.py" in unit
    assert ".venv/bin" not in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert ("ReadOnlyPaths=/home/ubuntu/hft-bot-rfq-fresh "
            "/var/lib/kalshi-rfq-fresh/control") in unit
    assert ("ReadWritePaths=/home/ubuntu/hft-bot/work/raw "
            "/home/ubuntu/hft-bot/work/live") in unit
    assert "/home/ubuntu/hft-bot/work/control" not in unit
    assert 'p.add_argument("--binary"' not in TOOL.read_text()
    assert "OOMScoreAdjust=300" in unit
    assert "capture\\.service" in oom and "choom -p \"$pid\" -n 300" in oom
    assert '--exclude "*/rfq_${HH}.ndjson*"' in sync
    assert '--exclude "*/rfq_receipts_${HH}.ndjson*"' in sync
    assert "facts dim catalog legacy_greed _meta seals" in sync
    assert "LEGACY_RESEARCH_COPY_DISABLED" in sync
    assert "research_release.py publish" in sync
    assert "--no-rfq" in sync
    assert "research_zero_copy_v3_cutover_approved" in sync
    assert sync.index('if [ "$MODE" = research_sync ]') < sync.index(
        'source "$HOME/.kalshi/env.sh"')
    assert "rt.mode == Mode::Live" in ws
    assert "can_place_orders(rt)" in ws
    assert "rec.recv_mono_ns = trading::mono_ns()" in ws_client
    assert "rec.recv_wall_ns = trading::wall_ns()" in ws_client
    assert ws_client.index("recorder_->record") < ws_client.index("decoder_.decode")
    assert "date={UTC_DATE}" in TOOL.read_text()


def test_wrapper_promotes_recorder_loss_and_invalid_connected_feed(tmp_path):
    metrics = tmp_path / "metrics.ndjson"
    metrics.write_text(
        '{"type":"feed","connected":true,"valid":true,"recorder_dropped":0}\n'
        '{"type":"feed","connected":true,"valid":false,"recorder_dropped":2,"recorder_write_failures":1}\n'
    )
    findings = rfq.metrics_loss_since(metrics, 0)
    assert any("recorder_dropped=2" in x for x in findings)
    assert any("recorder_write_failures=1" in x for x in findings)
    assert rfq.metrics_loss_since(metrics, metrics.stat().st_size) == []

    # The decoded telemetry ring is not the byte-exact recorder and therefore
    # cannot invalidate otherwise complete RFQ raw.
    metrics.write_text(
        '{"type":"feed","ts_ms":1,"connected":true,"valid":true,'
        '"recorder_dropped":0,"recorder_write_failures":0,'
        '"telemetry_dropped":9}\n')
    assert rfq.metrics_loss_since(metrics, 0) == []


def test_segment_health_requires_positive_metrics_and_exact_ack(tmp_path):
    metrics = tmp_path / "metrics.ndjson"
    metrics.write_text("")
    empty = rfq.metrics_evidence_since(metrics, 0)
    assert "no RFQ feed metrics rows" in empty["findings"]
    assert "no connected+valid RFQ feed heartbeat" in empty["findings"]

    hour = "2026-07-12T04"
    wall = int(rfq.dt.datetime(2026, 7, 12, 4, tzinfo=rfq.dt.timezone.utc).timestamp() * 1e9)
    cap = tmp_path / "rfq_04.ndjson"
    good = {"type": "subscribed", "msg": {"channel": "communications", "sid": 9}}
    physical = __import__("json").dumps({
        "recv_wall_ns": wall, "recv_mono_ns": 1, "raw": __import__("json").dumps(good)
    }) + "\n"
    cap.write_text(physical)
    evidence = rfq.capture_evidence_since(cap, 0, hour)
    assert evidence["subscribed_communications"] == 1
    assert evidence["subscription_ack_identity_sha256"] == hashlib.sha256(
        physical.encode()).hexdigest()
    assert evidence["partition_mismatches"] == 0
    assert evidence["findings"] == []


def test_partition_mismatch_and_wrong_channel_ack_is_not_proof(tmp_path):
    cap = tmp_path / "rfq_04.ndjson"
    wrong = {"type": "subscribed", "msg": {"channel": "ticker", "sid": 2}}
    # 05:00 receipt placed in the 04 partition.
    wall = int(rfq.dt.datetime(2026, 7, 12, 5, tzinfo=rfq.dt.timezone.utc).timestamp() * 1e9)
    cap.write_text(__import__("json").dumps({
        "recv_wall_ns": wall, "recv_mono_ns": 1, "raw": __import__("json").dumps(wrong)
    }) + "\n")
    evidence = rfq.capture_evidence_since(cap, 0, "2026-07-12T04")
    assert evidence["subscribed_communications"] == 0
    assert evidence["partition_mismatches"] == 1
    assert any("partition mismatch" in x for x in evidence["findings"])


def test_unsubscribe_or_error_revokes_persistent_subscription_proof(tmp_path):
    hour = "2026-07-12T04"
    base = rfq.dt.datetime(2026, 7, 12, 4, tzinfo=rfq.dt.timezone.utc)
    rows = []
    for seconds, frame in (
        (1, {"type": "subscribed", "msg": {"channel": "communications", "sid": 9}}),
        (2, {"type": "unsubscribed", "sid": 9, "msg": {}}),
        (3, {"type": "error", "msg": {"code": 7}}),
    ):
        wall = int((base + rfq.dt.timedelta(seconds=seconds)).timestamp() * 1e9)
        rows.append(json.dumps({"recv_wall_ns": wall, "recv_mono_ns": seconds,
                                "raw": json.dumps(frame)}))
    marker_wall = int((base + rfq.dt.timedelta(seconds=4)).timestamp() * 1e9)
    rows.append(json.dumps({"recv_wall_ns": marker_wall, "recv_mono_ns": 4,
                            "marker": "transport_close", "raw": ""}))
    cap = tmp_path / "rfq_04.ndjson"
    cap.write_text("\n".join(rows) + "\n")
    evidence = rfq.capture_evidence_since(cap, 0, hour)
    assert evidence["subscribed_communications"] == 1
    assert evidence["subscription_invalidations"] == 3
    assert evidence["subscription_proven_at_end"] is False
    assert len([x for x in evidence["findings"] if "control frame" in x]) == 2
    assert any("transport boundary marker" in x for x in evidence["findings"])


def test_partition_close_requires_new_hour_marker_and_old_file_stability(tmp_path):
    old = tmp_path / "rfq_04.ndjson"
    old.write_text("old\n")
    new = tmp_path / "rfq_05.ndjson"
    wall = int(rfq.dt.datetime(
        2026, 7, 12, 5, tzinfo=rfq.dt.timezone.utc).timestamp() * 1e9)
    new.write_text(json.dumps({
        "recv_wall_ns": wall, "recv_mono_ns": 1, "source": "Kalshi",
        "channel": "", "source_ticker": "", "marker": "hour_open", "raw": "",
    }) + "\n")

    class Alive:
        @staticmethod
        def poll():
            return None

    closed, stable_ms = rfq.wait_for_partition_close(
        old, new, "2026-07-12T05", Alive(), timeout_s=1.0, stable_s=0.05)
    assert closed is True
    assert stable_ms >= 50


def test_metrics_reader_advances_incrementally_without_lifetime_rescan(tmp_path):
    metrics = tmp_path / "metrics.ndjson"
    prefix = (b'{"type":"system","ts_ms":1}\n' * 10_000)
    row = (_feed_metric(2000) + "\n").encode()
    metrics.write_bytes(prefix + row)
    ev = rfq.metrics_evidence_since(metrics, len(prefix))
    assert ev["feed_rows"] == 1
    assert ev["end_offset"] == metrics.stat().st_size
    metrics.write_bytes(metrics.read_bytes() + row.replace(b"2000", b"3000"))
    ev2 = rfq.metrics_evidence_since(metrics, ev["end_offset"])
    assert ev2["feed_rows"] == 1
    assert ev2["first_ts_ms"] == 3000


def _feed_metric(ts_ms, **overrides):
    row = {
        "type": "feed", "ts_ms": ts_ms, "synthetic": False,
        "source": "kalshi_ws", "connected": True, "valid": True,
        "reconnects": 0, "disconnects": 0, "errors": 0,
        "recorder_dropped": 0, "recorder_write_failures": 0,
        "capture": "/raw/date=2026-07-12/rfq_04.ndjson",
        "mode": "data_collect", "env": "prod",
    }
    row.update(overrides)
    return json.dumps(row, separators=(",", ":"))


def test_metrics_reader_requires_exact_fields_and_contiguous_cadence(
        tmp_path, monkeypatch):
    metrics = tmp_path / "metrics.ndjson"
    start_ms = 1_783_826_400_000
    metrics.write_text("\n".join(
        _feed_metric(start_ms + index * 1000) for index in range(3600)) + "\n")
    evidence = rfq.metrics_evidence_since(
        metrics, 0, start_ms=start_ms, end_ms=start_ms + 3_600_000)
    assert evidence["feed_rows"] == 3600
    assert evidence["connected_valid_rows"] == 3600
    assert evidence["findings"] == []

    metrics.write_text(
        _feed_metric(start_ms) + "\n" +
        _feed_metric(start_ms + 6000, source="wrong") + "\n" +
        _feed_metric(start_ms + 7000) + "\n" +
        '{"type":"feed","ts_ms":1,"ts_ms":2}\n')
    invalid = rfq.metrics_evidence_since(metrics, 0)
    assert any("capture contract" in finding for finding in invalid["findings"])
    assert any("gap exceeds" in finding for finding in invalid["findings"])
    assert any("malformed" in finding for finding in invalid["findings"])

    monkeypatch.setattr(rfq, "MAX_METRICS_LINE_BYTES", 64)
    metrics.write_text(_feed_metric(start_ms) + "\n")
    bounded = rfq.metrics_evidence_since(metrics, 0)
    assert any("bounded parser limit" in finding
               for finding in bounded["findings"])


def test_capture_shard_offsets_are_snapshotted_before_child_launch():
    text = TOOL.read_text()
    marker = "capture_before_snapshot = hour_shard_snapshot(cap)"
    launch = "child = subprocess.Popen([str(binary)]"
    assert text.index(marker) < text.index(launch)


def _capture_row(when, *, frame=None, marker=None):
    row = {
        "recv_wall_ns": int(when.timestamp() * 1e9),
        "recv_mono_ns": 1, "source": "Kalshi", "channel": "",
        "source_ticker": "", "raw": json.dumps(frame or {}),
    }
    if marker is not None:
        row["marker"] = marker
        row["raw"] = ""
    return json.dumps(row) + "\n"


def test_capture_parser_rejects_ambiguous_shapes_and_keeps_ns_boundary_exact():
    hour = "2026-07-12T04"
    evidence = rfq._empty_capture_evidence()
    rfq._consume_capture_line(evidence, b"[]\n", hour)
    rfq._consume_capture_line(evidence, json.dumps({
        "recv_wall_ns": 10 ** 1000, "raw": "{}"}).encode() + b"\n", hour)
    inner_list = json.dumps({
        "recv_wall_ns": 1_783_826_400_000_000_000, "raw": "[]"}).encode() + b"\n"
    rfq._consume_capture_line(evidence, inner_list, hour)
    duplicate = json.dumps({
        "recv_wall_ns": 1_783_826_400_000_000_000,
        "raw": ('{"type":"error","type":"subscribed",'
                '"msg":{"channel":"communications","sid":9}}'),
    }).encode() + b"\n"
    rfq._consume_capture_line(evidence, duplicate, hour)
    assert evidence["subscribed_communications"] == 0
    assert evidence["findings"].count("malformed recorder row") == 2
    assert evidence["findings"].count("malformed inner WS frame") == 2

    exact = rfq._empty_capture_evidence()
    end_ns = int(rfq.dt.datetime(
        2026, 7, 12, 5, tzinfo=rfq.dt.timezone.utc).timestamp() * 1e9)
    before_boundary = json.dumps({
        "recv_wall_ns": end_ns - 1, "raw": "{}"}).encode() + b"\n"
    rfq._consume_capture_line(exact, before_boundary, hour)
    assert exact["partition_mismatches"] == 0


def test_capture_reader_advances_across_new_rotation_shards(tmp_path):
    hour = "2026-07-12T04"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1, tzinfo=rfq.dt.timezone.utc)
    base = tmp_path / "rfq_04.ndjson"
    base.write_text(_capture_row(when, marker="hour_open"))
    shard1 = tmp_path / "rfq_04.ndjson.1"
    shard1.write_text(_capture_row(
        when, frame={"type": "subscribed",
                     "msg": {"channel": "communications", "sid": 9}}))

    first = rfq.capture_evidence_since_shards(base, {}, hour)
    assert first["recorder_rows"] == 2
    assert first["subscribed_communications"] == 1
    assert first["subscription_proven_at_end"] is True
    cursor = first["end_offsets"]

    shard2 = tmp_path / "rfq_04.ndjson.2"
    shard2.write_text(
        _capture_row(when, frame={"type": "unsubscribed", "sid": 9, "msg": {}}) +
        _capture_row(when, marker="loss"))
    second = rfq.capture_evidence_since_shards(base, cursor, hour)
    assert second["recorder_rows"] == 2, "base/.1 must not be rescanned"
    assert second["subscription_invalidations"] == 1
    assert second["subscription_proven_at_end"] is False
    assert any("loss/gap" in x for x in second["findings"])

    total = rfq._empty_capture_evidence()
    rfq._merge_capture_evidence(total, first)
    rfq._merge_capture_evidence(total, second)
    assert total["recorder_rows"] == 4
    assert total["subscription_proven_at_end"] is False
    assert total["subscription_ack_identity_sha256"] == \
        first["subscription_ack_identity_sha256"]
    assert total["shards"] == [str(base), str(shard1), str(shard2)]

    # A later chunk must not erase an earlier shard inventory. Disappearance
    # is blocking evidence, not permission to rewrite segment history.
    reduced = rfq._empty_capture_evidence()
    reduced["shards"] = [str(base)]
    rfq._merge_capture_evidence(total, reduced)
    assert total["shards"] == [str(base), str(shard1), str(shard2)]


def test_capture_shard_discovery_rejects_gap_and_symlink(tmp_path):
    base = tmp_path / "rfq_04.ndjson"
    base.write_text("base\n")
    (tmp_path / "rfq_04.ndjson.2").write_text("gap\n")
    _, findings = rfq.discover_hour_shards(base, require_base=True)
    assert any("non-contiguous" in x for x in findings)
    # A hostile huge suffix must be rejected in bounded memory; discovery
    # reports a compact missing range instead of materializing every ordinal.
    (tmp_path / "rfq_04.ndjson.999999999").write_text("huge-gap\n")
    _, huge_findings = rfq.discover_hour_shards(base, require_base=True)
    assert any("3-999999998" in x for x in huge_findings)

    other = tmp_path / "other"
    other.write_text("target\n")
    (tmp_path / "rfq_04.ndjson.1").symlink_to(other)
    _, findings2 = rfq.discover_hour_shards(base, require_base=True)
    assert any("symlink forbidden" in x for x in findings2)


def test_partition_close_stability_tracks_append_and_new_shard(tmp_path):
    old = tmp_path / "rfq_04.ndjson"
    old.write_text("base\n")
    shard1 = tmp_path / "rfq_04.ndjson.1"
    shard1.write_text("one\n")
    new = tmp_path / "rfq_05.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 5, tzinfo=rfq.dt.timezone.utc)
    new.write_text(_capture_row(when, marker="hour_open"))

    class Alive:
        @staticmethod
        def poll():
            return None

    def late_rotation():
        time.sleep(0.04)
        with shard1.open("a") as fh:
            fh.write("late\n")
        time.sleep(0.04)
        (tmp_path / "rfq_04.ndjson.2").write_text("new-shard\n")

    worker = threading.Thread(target=late_rotation)
    started = time.monotonic()
    worker.start()
    closed, stable_ms = rfq.wait_for_partition_close(
        old, new, "2026-07-12T05", Alive(), timeout_s=1.0, stable_s=0.10)
    worker.join(timeout=1)
    assert closed is True
    assert stable_ms >= 100
    assert time.monotonic() - started >= 0.18


def test_multishard_attestation_is_streaming_and_binds_order(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    base.write_bytes(b"a" * (2 * 1024 * 1024 + 7))
    shard1 = day / "rfq_04.ndjson.1"
    shard1.write_bytes(b"b" * (1024 * 1024 + 3))
    monkeypatch.setattr(Path, "read_bytes", lambda _self: (_ for _ in ()).throw(
        AssertionError("large RFQ files must never use Path.read_bytes")))
    requested = []
    original_read = os.read

    def bounded_read(fd, count):
        requested.append(count)
        return original_read(fd, count)

    monkeypatch.setattr(rfq.os, "read", bounded_read)
    shards, findings = rfq.attest_hour_shards(base, raw, {})
    assert findings == []
    shards, eof_findings = rfq.bind_complete_line_eof(
        shards, {str(base): base.stat().st_size,
                 str(shard1): shard1.stat().st_size}, raw)
    assert eof_findings == []
    assert [row["ordinal"] for row in shards] == [0, 1]
    assert [row["size"] for row in shards] == [2 * 1024 * 1024 + 7,
                                                 1024 * 1024 + 3]
    assert max(requested) <= rfq.HASH_CHUNK_BYTES
    assert len(rfq.capture_shard_set_sha256(shards)) == 64
    assert "read_bytes(" not in TOOL.read_text()


def test_final_parse_and_hash_use_identical_bytes_and_exact_offsets(
        tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    first = _capture_row(when, marker="hour_open").encode()
    ack = _capture_row(
        when, frame={"type": "subscribed",
                     "msg": {"channel": "communications", "sid": 9}}).encode()
    base.write_bytes(first)
    shard = day / "rfq_04.ndjson.1"
    shard.write_bytes(ack)
    requested = []
    original_read = os.read

    def bounded_read(fd, count):
        requested.append(count)
        return original_read(fd, count)

    monkeypatch.setattr(rfq.os, "read", bounded_read)
    evidence, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {str(base): 0, str(shard): 0}, "2026-07-12T04")
    assert findings == [] and evidence["findings"] == []
    assert evidence["subscribed_communications"] == 1
    assert evidence["subscription_ack_identity_sha256"] == \
        hashlib.sha256(ack).hexdigest()
    assert evidence["start_offsets"] == {str(base): 0, str(shard): 0}
    assert evidence["end_offsets"] == {
        str(base): len(first), str(shard): len(ack)}
    assert [row["sha256"] for row in objects] == [
        hashlib.sha256(first).hexdigest(), hashlib.sha256(ack).hexdigest()]
    assert all(row["parsed_bytes_at_close"] == row["size"] for row in objects)
    assert max(requested) <= rfq.HASH_CHUNK_BYTES


def test_final_parse_excludes_old_transport_marker_but_hashes_full_file(
        tmp_path):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    prefix = (_capture_row(when, marker="transport_close") +
              _capture_row(when, frame={"type": "rfq_created"})).encode()
    ack = _capture_row(
        when, frame={"type": "subscribed",
                     "msg": {"channel": "communications", "sid": 9}}).encode()
    suffix_event = _capture_row(
        when, frame={"type": "rfq_deleted"}).encode()
    complete = prefix + ack + suffix_event
    base.write_bytes(complete)

    evidence, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {str(base): len(prefix)}, "2026-07-12T04")

    assert findings == [] and evidence["findings"] == []
    assert evidence["markers"].get("transport_close", 0) == 0
    assert evidence["subscription_invalidations"] == 0
    assert evidence["rfq_created"] == 0
    assert evidence["rfq_deleted"] == 1
    assert evidence["subscribed_communications"] == 1
    assert evidence["subscription_proven_at_end"] is True
    assert evidence["subscription_ack_identity_sha256"] == \
        hashlib.sha256(ack).hexdigest()
    assert objects == [{
        "ordinal": 0,
        "relpath": "date=2026-07-12/rfq_04.ndjson",
        "bytes_before": len(prefix),
        "size": len(complete),
        "parsed_bytes_at_close": len(complete),
        "sha256": hashlib.sha256(complete).hexdigest(),
    }]


@pytest.mark.parametrize("unsafe", ["negative", "past_eof", "mid_line"])
def test_final_parse_rejects_unsafe_segment_start_cursor(tmp_path, unsafe):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    complete = _capture_row(when, marker="transport_close").encode()
    base.write_bytes(complete)
    cursor = {
        "negative": -1,
        "past_eof": len(complete) + 1,
        "mid_line": 1,
    }[unsafe]

    evidence, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {str(base): cursor}, "2026-07-12T04")

    assert objects == []
    assert evidence["recorder_rows"] == 0
    assert evidence["markers"] == {}
    assert len(findings) == 1
    if unsafe == "mid_line":
        assert "not a complete-line boundary" in findings[0]
    else:
        assert "invalid pre-capture byte cursor" in findings[0]


@pytest.mark.parametrize("unsafe", [True, "0", 1.0, None])
def test_final_parse_requires_exact_integer_segment_cursor(tmp_path, unsafe):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    base.write_bytes(_capture_row(when, marker="hour_open").encode())

    evidence, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {str(base): unsafe}, "2026-07-12T04")

    assert objects == []
    assert evidence["recorder_rows"] == 0
    assert len(findings) == 1
    assert "cursor is not an integer" in findings[0]


def test_final_parse_accepts_aligned_eof_without_replaying_prefix(tmp_path):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    prefix = _capture_row(when, marker="transport_close").encode()
    base.write_bytes(prefix)

    evidence, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {str(base): len(prefix)}, "2026-07-12T04")

    assert findings == [] and evidence["findings"] == []
    assert evidence["recorder_rows"] == 0
    assert evidence["markers"] == {}
    assert evidence["end_offsets"] == {str(base): len(prefix)}
    assert objects[0]["parsed_bytes_at_close"] == len(prefix)
    assert objects[0]["sha256"] == hashlib.sha256(prefix).hexdigest()


def test_final_parse_reports_torn_new_suffix_at_exact_complete_line_eof(
        tmp_path):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    prefix = _capture_row(when, marker="transport_close").encode()
    complete_suffix = _capture_row(
        when, frame={"type": "rfq_created"}).encode()
    torn_suffix = b'{"recv_wall_ns":1783828860000000000'
    payload = prefix + complete_suffix + torn_suffix
    base.write_bytes(payload)

    evidence, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {str(base): len(prefix)}, "2026-07-12T04")

    complete_eof = len(prefix) + len(complete_suffix)
    assert findings == []
    assert evidence["rfq_created"] == 1
    assert evidence["markers"].get("transport_close", 0) == 0
    assert evidence["end_offsets"] == {str(base): complete_eof}
    assert any("non-newline-terminated final row" in finding
               for finding in evidence["findings"])
    assert objects[0]["parsed_bytes_at_close"] == complete_eof
    assert objects[0]["size"] == len(payload)
    assert objects[0]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_final_parse_segment_cursor_may_cross_hash_read_boundary(
        tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    prefix = _capture_row(when, marker="transport_close").encode()
    suffix = _capture_row(
        when, frame={"type": "rfq_created"}).encode()
    payload = prefix + suffix
    base.write_bytes(payload)
    # Force the parser start into the middle of a hash read sequence instead
    # of relying on production's much larger chunk size.
    monkeypatch.setattr(rfq, "HASH_CHUNK_BYTES", 17)

    evidence, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {str(base): len(prefix)}, "2026-07-12T04")

    assert findings == [] and evidence["findings"] == []
    assert evidence["recorder_rows"] == 1
    assert evidence["rfq_created"] == 1
    assert evidence["markers"].get("transport_close", 0) == 0
    assert objects[0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert objects[0]["parsed_bytes_at_close"] == len(payload)


def test_equal_length_rewrite_cannot_pair_stale_ack_with_new_hash(tmp_path):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    original = _capture_row(
        when, frame={"type": "subscribed",
                     "msg": {"channel": "communications", "sid": 9}}).encode()
    base.write_bytes(original)
    stale = rfq.capture_evidence_since_shards(base, {}, "2026-07-12T04")
    assert stale["subscribed_communications"] == 1
    rewritten = original.replace(b"subscribed", b"not_subbed")
    assert len(rewritten) == len(original)
    base.write_bytes(rewritten)
    final, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {}, "2026-07-12T04")
    assert findings == []
    assert final["subscribed_communications"] == 0
    assert final["subscription_ack_identity_sha256"] is None
    assert objects[0]["sha256"] == hashlib.sha256(rewritten).hexdigest()


def test_final_capture_parser_enforces_line_bound_before_json_decode(
        tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    base.write_bytes(b'{"padding":"' + b"x" * 100 + b'"}\n')
    monkeypatch.setattr(rfq, "MAX_CAPTURE_LINE_BYTES", 64)
    evidence, objects, findings = rfq.parse_and_attest_hour_shards(
        base, raw, {}, "2026-07-12T04")
    assert findings == []
    assert any("bounded parser limit" in finding
               for finding in evidence["findings"])
    assert objects[0]["parsed_bytes_at_close"] is None


def test_capture_shard_set_digest_is_order_independent_and_fail_closed():
    base = {
        "ordinal": 0, "relpath": "date=2026-07-12/rfq_04.ndjson",
        "bytes_before": 0, "size": 10, "parsed_bytes_at_close": 10,
        "sha256": "a" * 64,
    }
    rotated = {
        "ordinal": 1, "relpath": "date=2026-07-12/rfq_04.ndjson.1",
        "bytes_before": 0, "size": 20, "parsed_bytes_at_close": 20,
        "sha256": "b" * 64,
    }
    assert (rfq.capture_shard_set_sha256([base, rotated]) ==
            rfq.capture_shard_set_sha256([rotated, base]))

    duplicate = [base, dict(rotated, ordinal=0,
                            relpath="date=2026-07-12/rfq_04.ndjson")]
    gap = [base, dict(rotated, ordinal=2,
                      relpath="date=2026-07-12/rfq_04.ndjson.2")]
    unsafe = [dict(base, relpath="../rfq_04.ndjson")]
    for bad in (duplicate, gap, unsafe):
        with pytest.raises(ValueError):
            rfq.capture_shard_set_sha256(bad)


def test_final_complete_line_cursor_rejects_torn_tail(tmp_path):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    base.write_bytes(_capture_row(when, marker="hour_open").encode() + b'{"torn"')
    evidence = rfq.capture_evidence_since_shards(base, {}, "2026-07-12T04")
    shards, findings = rfq.attest_hour_shards(base, raw, {})
    assert findings == []
    bound, eof_findings = rfq.bind_complete_line_eof(
        shards, evidence["end_offsets"], raw)
    assert bound[0]["parsed_bytes_at_close"] < bound[0]["size"]
    assert any("unparsed/torn bytes" in finding for finding in eof_findings)


@pytest.mark.parametrize("mutation", ["append", "new_shard"])
def test_final_parse_then_mutation_cannot_match_attestation(tmp_path, mutation):
    raw = tmp_path / "raw"
    day = raw / "date=2026-07-12"
    day.mkdir(parents=True)
    base = day / "rfq_04.ndjson"
    when = rfq.dt.datetime(2026, 7, 12, 4, 1,
                           tzinfo=rfq.dt.timezone.utc)
    base.write_text(_capture_row(when, marker="hour_open"))
    evidence = rfq.capture_evidence_since_shards(base, {}, "2026-07-12T04")
    if mutation == "append":
        with base.open("a", encoding="utf-8") as fh:
            fh.write(_capture_row(when, frame={"type": "rfq_created"}))
    else:
        (day / "rfq_04.ndjson.1").write_text(
            _capture_row(when, frame={"type": "rfq_created"}))
    shards, findings = rfq.attest_hour_shards(base, raw, {})
    assert findings == []
    _, eof_findings = rfq.bind_complete_line_eof(
        shards, evidence["end_offsets"], raw)
    expected = ("unparsed/torn bytes" if mutation == "append" else
                "missing final complete-line cursor")
    assert any(expected in finding for finding in eof_findings)


@pytest.mark.parametrize("mutation", ["append", "new_shard"])
def test_final_snapshot_rescan_rejects_late_shard_mutation(tmp_path, mutation):
    base = tmp_path / "rfq_04.ndjson"
    base.write_text("stable\n")
    before = rfq.hour_shard_snapshot(base, require_base=True)
    if mutation == "append":
        with base.open("a", encoding="utf-8") as handle:
            handle.write("late\n")
    else:
        (tmp_path / "rfq_04.ndjson.1").write_text("late shard\n")
    after = rfq.hour_shard_snapshot(base, require_base=True)
    findings = rfq.final_attestation_snapshot_findings(before, after)
    assert findings == ["RFQ shard set changed during final attestation/rescan"]


def test_second_authority_guard_runs_after_final_hash_and_snapshot_rescan():
    source = TOOL.read_text()
    finalize = source[source.index("def finalize_current"):]
    same_source = finalize.index("parse_and_attest_hour_shards(")
    final_hash = finalize.index("capture_shard_set_sha256(")
    second_guard = finalize.index("final_authority_valid, final_authority_findings")
    snapshot_after = finalize.index("attestation_snapshot_after =")
    assert same_source < final_hash < snapshot_after < second_guard


def test_clean_head_guard_runs_before_child_launch():
    source = TOOL.read_text()
    loop = source[source.index("while not stopping:"):]
    guard = loop.index("fresh_lane_finalize_guard(fresh_authority_binding)")
    launch = loop.index("child = subprocess.Popen([str(binary)]")
    assert guard < launch


def test_missing_subscription_ack_fails_closed_after_bounded_grace():
    assert rfq.subscription_startup_timed_out(29.9, False, 30.0) is False
    assert rfq.subscription_startup_timed_out(30.1, False, 30.0) is True
    assert rfq.subscription_startup_timed_out(300.0, True, 30.0) is False


def test_ingest_checkpoints_rfq_family_without_contaminating_market_facts(tmp_path):
    """D4: new raw naming is consumed byte-for-byte by the existing scanner.

    RFQ remains raw-only in this rollout, so no ticker/trade/book fact may be
    fabricated while the per-file checkpoint must reach the complete size.
    """
    import duckdb
    sys.path.insert(0, str(ROOT / "tools"))
    import ingest
    wh = tmp_path / "warehouse"
    wh.mkdir()
    cap = tmp_path / "rfq_00.ndjson"
    raw = '{"type":"rfq_created","sid":9,"msg":{"id":"r1","creator_id":"","market_ticker":"KXNBA-26JUL12-BOS","created_ts":"2026-07-12T00:00:00Z","contracts_fp":"10.00"}}'
    cap.write_text('{"recv_wall_ns":1783814400000000000,"recv_mono_ns":1,"channel":"rfq_created","raw":%s}\n'
                   % __import__("json").dumps(raw))
    db = tmp_path / "staging.duckdb"
    con = duckdb.connect(str(db))
    counts = ingest.Ingester(con, str(wh)).process_file(str(cap))
    checkpoint = con.execute("SELECT byte_offset FROM checkpoint WHERE file=?",
                             [str(cap.resolve())]).fetchone()[0]
    facts = sum(con.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
                for t in ("orderbooks_l1", "trades", "orderbooks_full"))
    con.close()
    assert counts == (0, 0, 0)
    assert checkpoint == cap.stat().st_size
    assert facts == 0
