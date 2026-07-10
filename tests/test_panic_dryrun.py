"""W-K2 acceptance (PLAN_RISK_KILLSWITCH §3): the panic CLI, drilled against
a seeded localhost mock exchange — DEMONSTRATED, not described.

Proves: dry-run default transmits NOTHING mutating (mock journal empty);
execute drill cancels all + verifies zero resting + liquidates with
hand-computed crossing prices; every liquidation order is IOC + reduce_only
(the mock REJECTS anything else — the dead-man/crossing operator ruling is
enforced by the counterparty in the drill); ack-loss on cancel and on order
both recover by REUSING the same request/client_order_id with no double fill
(journaled); a refused cancel yields a loud PARTIAL-FAILURE + nonzero exit;
truncated enumeration is never trusted; execute against prod-shaped env is
refused without the live arming stack (S1 choke point).

Runs the REAL binary (build/panic) with KALSHI_ENV=local against the mock —
the same execute code path the operator-gated live rehearsal (W-K6) will use.
"""
import json
import os
import socket
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANIC = os.path.join(ROOT, "build", "panic")
MOCK = os.path.join(ROOT, "tests", "mock_exchange_panic.py")

pytestmark = pytest.mark.skipif(
    not os.path.exists(PANIC), reason="build/panic not built (run make)")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def throwaway_key(tmp_path):
    key = tmp_path / "k.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                   capture_output=True, check=True)
    return str(key)


class Drill:
    def __init__(self, tmp_path, key, orders=0, positions="", faults=""):
        self.port = _free_port()
        self.journal = str(tmp_path / "journal.ndjson")
        env = dict(os.environ,
                   MOCK_ORDERS=str(orders), MOCK_POSITIONS=positions,
                   MOCK_FAULTS=faults, MOCK_JOURNAL=self.journal)
        self.proc = subprocess.Popen(
            [sys.executable, MOCK, str(self.port)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.proc.stdout.readline()          # wait for "on <port>"
        self.key = key

    def panic(self, *args, wait_ms="50"):
        env = dict(os.environ,
                   KALSHI_ENV="local", KALSHI_MOCK_PORT=str(self.port),
                   KALSHI_API_KEY_ID="drill-key-id",
                   KALSHI_PRIVATE_KEY_PATH=self.key)
        env.pop("KALSHI_BASE_URL", None)
        env.pop("KALSHI_MODE", None)
        env.pop("KALSHI_ALLOW_LIVE", None)
        p = subprocess.run([PANIC, "--wait-ms", wait_ms] + list(args),
                           env=env, capture_output=True, text=True, timeout=60)
        return p

    def journal_records(self):
        if not os.path.exists(self.journal):
            return []
        return [json.loads(l) for l in open(self.journal) if l.strip()]

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


@pytest.fixture()
def drill_factory(tmp_path, throwaway_key):
    made = []
    def make(**kw):
        d = Drill(tmp_path, throwaway_key, **kw)
        made.append(d)
        return d
    yield make
    for d in made:
        d.stop()


# ─────────────────────────────────────────────── dry-run transmits nothing

def test_dry_run_default_plans_but_never_mutates(drill_factory):
    d = drill_factory(orders=3, positions="KXMOCK-25DEC31-LONG:5.00")
    p = d.panic()
    assert p.returncode == 0, p.stdout + p.stderr
    assert "DRY-RUN" in p.stdout and "PANIC DRY-RUN OK" in p.stdout
    assert p.stdout.count("cancel ord-") == 3          # full plan printed
    assert "SELL 5.00 KXMOCK-25DEC31-LONG" in p.stdout
    assert d.journal_records() == []                   # NOTHING hit the mock


# ───────────────────────────────────── clean execute drill, hand-computed

def test_execute_drill_cancels_verifies_and_liquidates(drill_factory):
    d = drill_factory(orders=2,
                      positions="KXMOCK-25DEC31-LONG:5.00,KXMOCK-25DEC31-SHRT:-3.00")
    p = d.panic("--execute")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "PANIC CLEAN" in p.stdout
    assert "zero_resting=VERIFIED" in p.stdout and "flat=VERIFIED" in p.stdout
    recs = d.journal_records()
    cancels = [r for r in recs if r["op"] == "cancel"]
    fills = [r for r in recs if r["op"] == "order_filled"]
    assert len(cancels) == 2
    assert len(fills) == 2
    by_tk = {f["ticker"]: f for f in fills}
    # hand-computed round-1 crossing prices (mock touch = 0.4000 / 0.6000):
    # exit LONG  -> sell into the bid at 0.4000 (side ask)
    # exit SHORT -> buy from the ask at 0.6000 (side bid), magnitude 3.00
    long_f = by_tk["KXMOCK-25DEC31-LONG"]
    assert (long_f["side"], long_f["price"], long_f["count"]) == \
        ("ask", "0.4000", "5.00")
    shrt_f = by_tk["KXMOCK-25DEC31-SHRT"]
    assert (shrt_f["side"], shrt_f["price"], shrt_f["count"]) == \
        ("bid", "0.6000", "3.00")
    # the mock 400-rejects any order that is not IOC+reduce_only, so CLEAN
    # also proves the dead-man/crossing contract on every order sent
    assert not [r for r in recs if r["op"] == "bad_order_contract"]


# ─────────────────────────────────────────── ack-loss: retries, no doubles

def test_cancel_ack_loss_recovers_idempotently(drill_factory):
    d = drill_factory(orders=2, faults="cancel_ack_loss")
    p = d.panic("--execute")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "PANIC CLEAN" in p.stdout
    recs = d.journal_records()
    # each order: applied-but-ack-lost once; retry saw 404 => success; the
    # exchange never saw a THIRD attempt per order
    assert len([r for r in recs if r["op"] == "cancel_applied_ack_lost"]) == 2


def test_order_ack_loss_reuses_same_coid_no_double_fill(drill_factory):
    d = drill_factory(positions="KXMOCK-25DEC31-LONG:5.00",
                      faults="order_ack_loss")
    p = d.panic("--execute")
    assert p.returncode == 0, p.stdout + p.stderr
    recs = d.journal_records()
    lost = [r for r in recs if r["op"] == "order_applied_ack_lost"]
    dups = [r for r in recs if r["op"] == "duplicate_coid_rejected"]
    assert len(lost) == 1
    assert len(dups) >= 1                      # reissue carried the SAME coid
    assert dups[0]["coid"] == lost[0]["coid"]  # contract #9, byte-identical
    # exactly ONE fill applied at the exchange (no double liquidation)
    assert len([r for r in recs if r["op"] == "order_filled"]) == 0
    assert "PANIC CLEAN" in p.stdout           # position flat via the lost-ack fill


# ─────────────────────────────────────────────── loud partial failure (D2)

def test_refused_cancel_is_loud_partial_failure(drill_factory):
    oid = "ord-0000-0000-0000-0000-000000000000"
    d = drill_factory(orders=2, faults="cancel_refuse:%s" % oid)
    p = d.panic("--execute")
    assert p.returncode == 1
    assert "PANIC PARTIAL-FAILURE" in p.stdout
    assert "cancel refused/unreachable" in p.stderr
    assert "zero_resting=NO" in p.stdout       # never claims clear


def test_truncated_enumeration_never_trusted(drill_factory):
    d = drill_factory(faults="endless_pages")
    p = d.panic("--execute")
    assert p.returncode == 1
    assert "INCOMPLETE" in p.stdout + p.stderr


# ──────────────────────────────────────────── S1 choke point (arming gate)

def test_execute_refused_without_live_arming_outside_mock(drill_factory,
                                                          throwaway_key):
    """Against anything that is not the localhost mock env, --execute must
    pass require_orders_allowed — shadow/data_collect never transmit."""
    env = dict(os.environ,
               KALSHI_ENV="prod", KALSHI_MODE="shadow", KALSHI_ALLOW_PROD="1",
               KALSHI_API_KEY_ID="k", KALSHI_PRIVATE_KEY_PATH=throwaway_key)
    env.pop("KALSHI_ALLOW_LIVE", None)
    p = subprocess.run([PANIC, "--execute"], env=env, capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 2
    assert "not armed" in p.stderr
    assert "refused" in p.stderr


def test_execute_refused_when_localmock_points_at_real_host(throwaway_key):
    """Audit B1: KALSHI_HOST_UNSAFE_OVERRIDE can aim a local_mock env at a
    REAL host — the mock-drill branch must then refuse (loopback assertion),
    never fire. Refusal happens before any network I/O."""
    env = dict(os.environ,
               KALSHI_ENV="local", KALSHI_HOST_UNSAFE_OVERRIDE="1",
               KALSHI_BASE_URL="https://external-api.kalshi.com",
               KALSHI_API_KEY_ID="k", KALSHI_PRIVATE_KEY_PATH=throwaway_key)
    env.pop("KALSHI_ALLOW_LIVE", None)
    env.pop("KALSHI_MODE", None)
    p = subprocess.run([PANIC, "--execute"], env=env, capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 2
    assert "not armed" in p.stderr and "loopback" in p.stderr


def test_execute_refused_for_loopback_lookalike_host(throwaway_key):
    """Audit B1 hardening: the loopback check is EXACT host, not a substring
    — 'localhost.evil.com' must NOT count as loopback."""
    # https so the env-layer TLS rule doesn't reject first — we want to reach
    # panic's OWN loopback guard (env override skips the host allowlist).
    # Covers suffix lookalikes AND the userinfo tricks (curl dials the host
    # AFTER the '@'; re-audit round 2).
    for host in ("https://localhost.evil.com", "https://127.0.0.1.evil.com",
                 "https://127.0.0.1@evil.com", "https://127.0.0.1:18099@evil.com",
                 "https://localhost@evil.com",
                 # query/fragment authority terminator (audit round 3): curl
                 # ends the authority at '?'/'#', so these dial the REAL host
                 "https://external-api.kalshi.com?@127.0.0.1",
                 "https://external-api.kalshi.com#@localhost",
                 "https://evil.com?x=@127.0.0.1"):
        env = dict(os.environ,
                   KALSHI_ENV="local", KALSHI_HOST_UNSAFE_OVERRIDE="1",
                   KALSHI_BASE_URL=host,
                   KALSHI_API_KEY_ID="k", KALSHI_PRIVATE_KEY_PATH=throwaway_key)
        env.pop("KALSHI_ALLOW_LIVE", None)
        env.pop("KALSHI_MODE", None)
        p = subprocess.run([PANIC, "--execute"], env=env,
                           capture_output=True, text=True, timeout=30)
        assert p.returncode == 2, host
        assert "not armed" in p.stderr and "loopback" in p.stderr, \
            "%s -> %s" % (host, p.stderr)


def test_json_report_written(drill_factory, tmp_path):
    d = drill_factory(orders=1)
    out = str(tmp_path / "report.json")
    p = d.panic("--execute", "--json", out)
    assert p.returncode == 0
    rep = json.loads(open(out).read())
    assert rep["verdict"] == "CLEAN" and rep["mode"] == "execute"
    assert rep["orders_seen"] == 1 and rep["zero_resting_verified"] is True
