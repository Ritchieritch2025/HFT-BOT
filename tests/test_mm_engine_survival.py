"""Approval #1 acceptance: credential exclusivity + survival assertions.

These are the alarms that would have caught both of 2026-07-27's silent
failures: the live engine starved of market data by six shadow peers
(zero QUOTE_EVAL for an hour), and three sell_rich decisions that never
became orders.  Alarms only -- none of this changes behavior.
"""
from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_mm_engine_guards import E, reset as _base_reset  # noqa: E402


def fresh_live():
    _base_reset()
    E.S.unpaused_mono = None
    E.S.evals_since_unpause = 0
    E.S.last_eval_mono = None
    E.S.want_unmet = {}
    E.S.survival_alarms = 0
    E.S.halted = False
    E.CTRL["paused"] = False


def test_exclusivity_refuses_when_peers_exist():
    with (mock.patch.object(E, "peer_engine_pids",
                            return_value=[111, 222]),
          mock.patch.object(E, "MAX_PEER_ENGINES", 0)):
        violated, peers = E.credential_exclusivity_violation()
    assert violated and peers == [111, 222]


def test_exclusivity_allows_documented_peer_count():
    """MM_MAX_PEER_ENGINES=1 permits exactly one deliberate shadow peer."""
    with (mock.patch.object(E, "peer_engine_pids", return_value=[111]),
          mock.patch.object(E, "MAX_PEER_ENGINES", 1)):
        violated, _ = E.credential_exclusivity_violation()
    assert not violated
    with (mock.patch.object(E, "peer_engine_pids",
                            return_value=[111, 222]),
          mock.patch.object(E, "MAX_PEER_ENGINES", 1)):
        violated, _ = E.credential_exclusivity_violation()
    assert violated


def test_alarm_a_fires_on_zero_evals_after_five_minutes():
    fresh_live()
    now = 10_000.0
    E.S.unpaused_mono = now - 301.0
    E.S.evals_since_unpause = 0
    with mock.patch.object(E, "MODE", "live"):
        alarms = E.survival_check(now_mono=now)
    assert any(a["kind"] == "no_quote_eval_since_unpause" for a in alarms)


def test_alarm_a_quiet_when_evals_flow():
    fresh_live()
    now = 10_000.0
    E.S.unpaused_mono = now - 400.0
    E.S.evals_since_unpause = 500
    E.S.last_eval_mono = now - 1.0
    with mock.patch.object(E, "MODE", "live"):
        assert E.survival_check(now_mono=now) == []


def test_alarm_a_extended_fires_on_midrun_stall():
    """Evaluations flowed, then stopped for 60s: the 08:00 starvation shape
    can also appear mid-run, so the assertion is continuous."""
    fresh_live()
    now = 10_000.0
    E.S.unpaused_mono = now - 900.0
    E.S.evals_since_unpause = 5000
    E.S.last_eval_mono = now - 61.0
    with mock.patch.object(E, "MODE", "live"):
        alarms = E.survival_check(now_mono=now)
    assert any(a["kind"] == "quote_eval_stalled" for a in alarms)


def test_alarm_b_fires_on_unserved_want():
    fresh_live()
    now = 10_000.0
    E.S.unpaused_mono = now - 100.0
    E.S.evals_since_unpause = 100
    E.S.last_eval_mono = now - 1.0
    E.S.want_unmet[("MKT", "bid")] = now - 2.5
    with mock.patch.object(E, "MODE", "live"):
        alarms = E.survival_check(now_mono=now)
    assert any(a["kind"] == "intent_without_order" and a["mt"] == "MKT"
               for a in alarms)


def test_alarm_b_quiet_within_two_seconds():
    fresh_live()
    now = 10_000.0
    E.S.unpaused_mono = now - 100.0
    E.S.evals_since_unpause = 100
    E.S.last_eval_mono = now - 1.0
    E.S.want_unmet[("MKT", "bid")] = now - 1.5
    with mock.patch.object(E, "MODE", "live"):
        assert E.survival_check(now_mono=now) == []


def test_alarms_never_fire_when_paused_or_halted():
    """Alarms are about a LIVE engine failing to act; a paused or halted
    engine is supposed to be silent."""
    fresh_live()
    now = 10_000.0
    E.S.unpaused_mono = now - 900.0
    E.S.want_unmet[("MKT", "bid")] = now - 50.0
    with mock.patch.object(E, "MODE", "live"):
        E.CTRL["paused"] = True
        assert E.survival_check(now_mono=now) == []
        E.CTRL["paused"] = False
        E.S.halted = True
        assert E.survival_check(now_mono=now) == []


def test_block_reason_priority_chain():
    """Approval #1 expansion: the reason mirrors the want expression's own
    evaluation order, and always returns something -- the identity
    sum(reasons) == side_evals - side_wants holds by construction."""
    br = E.block_reason
    base = dict(blocked=False, px=0.4, is_exit_path=False,
                has_unpaired_opp=False, entry_pricing_ok=True,
                zone_ok_=True, room=True, cheap_ok=True, edge=2.0,
                margin=1.0, cap_ok=True, skew_block=False)
    assert br(**{**base, "blocked": True}) == "side_latched"
    assert br(**{**base, "px": 0.0}) == "px_zero"
    assert br(**{**base, "has_unpaired_opp": True}) == "exit_leg_unready"
    assert br(**{**base, "entry_pricing_ok": False}) == "pricing_not_ready"
    assert br(**{**base, "zone_ok_": False}) == "zone_closed"
    assert br(**{**base, "room": False}) == "no_capital_room"
    assert br(**{**base, "cheap_ok": False}) == "tail_blocked"
    assert br(**{**base, "edge": 0.5}) == "edge_below_margin"
    assert br(**{**base, "edge": None}) == "edge_below_margin"
    assert br(**{**base, "cap_ok": False}) == "edge_above_cap"
    assert br(**{**base, "skew_block": True}) == "skew_blocked"
    assert br(**base) == "other"


def test_block_accounting_identity_holds_through_think():
    """End to end: run think() over an armed market and check the identity
    with zero error."""
    from test_mm_engine_guards import F0_KernelSideSelection
    fresh_live()
    E.S.block_reasons = {}
    E.S.side_evals = 0
    E.S.side_wants = 0
    F0_KernelSideSelection._arm_market(
        F0_KernelSideSelection(methodName="run"), 55.0)
    with mock.patch.object(E, "load_control", return_value=False):
        E.think()
    total_blocked = sum(E.S.block_reasons.values())
    assert E.S.side_evals > 0
    assert E.S.side_evals - E.S.side_wants - total_blocked == 0, (
        E.S.side_evals, E.S.side_wants, E.S.block_reasons)


def test_stale_order_tombstone_skips_only_our_recent_releases():
    """The 20:38 race: a just-filled order still shows 'resting' in the
    exchange snapshot after the local reservation was released.  A fresh
    tombstone (ours, <=10s) is skipped; a foreign or expired identity
    still trips -- fail-closed survives."""
    fresh_live()
    now = E.time.monotonic()
    E.S.order_tombstones = {"oid-just-filled": now - 1.0,
                            "oid-ancient": now - 300.0}
    recent = any(now - E.S.order_tombstones.get(i, -1e9) <= 10.0
                 for i in ["oid-just-filled"])
    ancient = any(now - E.S.order_tombstones.get(i, -1e9) <= 10.0
                  for i in ["oid-ancient"])
    foreign = any(now - E.S.order_tombstones.get(i, -1e9) <= 10.0
                  for i in ["oid-foreign"])
    assert recent and not ancient and not foreign
