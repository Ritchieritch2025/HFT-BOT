"""Offline contract tests for the isolated passive RFQ collector."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
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
    assert 'p.add_argument("--binary"' not in TOOL.read_text()
    assert "OOMScoreAdjust=300" in unit
    assert "capture\\.service" in oom and "choom -p \"$pid\" -n 300" in oom
    assert '--exclude "*/rfq_${HH}.ndjson*"' in sync
    assert '--exclude "*/rfq_receipts_${HH}.ndjson*"' in sync
    assert "facts dim catalog legacy_greed _meta seals" in sync
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
    cap.write_text(__import__("json").dumps({
        "recv_wall_ns": wall, "recv_mono_ns": 1, "raw": __import__("json").dumps(good)
    }) + "\n")
    evidence = rfq.capture_evidence_since(cap, 0, hour)
    assert evidence["subscribed_communications"] == 1
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
    row = (b'{"type":"feed","ts_ms":2000,"connected":true,"valid":true,'
           b'"reconnects":0,"recorder_dropped":0}\n')
    metrics.write_bytes(prefix + row)
    ev = rfq.metrics_evidence_since(metrics, len(prefix))
    assert ev["feed_rows"] == 1
    assert ev["end_offset"] == metrics.stat().st_size
    metrics.write_bytes(metrics.read_bytes() + row.replace(b"2000", b"3000"))
    ev2 = rfq.metrics_evidence_since(metrics, ev["end_offset"])
    assert ev2["feed_rows"] == 1
    assert ev2["first_ts_ms"] == 3000


def test_capture_offset_is_snapshotted_before_child_launch():
    text = TOOL.read_text()
    marker = "capture_offset = _file_size(cap)"
    launch = "child = subprocess.Popen([str(binary)]"
    assert text.index(marker) < text.index(launch)


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
