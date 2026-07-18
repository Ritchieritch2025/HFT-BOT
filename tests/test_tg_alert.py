"""Offline regression tests for deploy/tg_alert.py event semantics."""
import copy
import importlib.util
import os
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEPLOY = os.path.join(ROOT, "deploy")


def _load_alert():
    sys.path.insert(0, DEPLOY)
    try:
        sys.modules.pop("tg_common", None)
        spec = importlib.util.spec_from_file_location(
            "tg_alert_test", os.path.join(DEPLOY, "tg_alert.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(DEPLOY)


def _state_io(monkeypatch, alert, initial):
    state = copy.deepcopy(initial)

    def read_json(name, default=None):
        return copy.deepcopy(state.get(name, default))

    def write_json(name, obj):
        state[name] = copy.deepcopy(obj)

    monkeypatch.setattr(alert.tg, "read_json", read_json)
    monkeypatch.setattr(alert.tg, "write_json", write_json)
    return state


def test_balance_change_is_one_shot_info_not_active_alarm(monkeypatch):
    alert = _load_alert()
    alert.CONF = {"BALANCE_POLL_MIN": "5", "BALANCE_FAIL_YELLOW": "3"}
    state = _state_io(monkeypatch, alert, {
        "balance_state.json": {"last_poll": 0, "fail_streak": 0,
                               "value": "33.8357"},
    })
    sent = []
    monkeypatch.setattr(alert.time, "time", lambda: 1000.0)
    monkeypatch.setattr(alert, "_balance_now", lambda: (True, "3.8455"))
    monkeypatch.setattr(alert.tg, "send",
                        lambda text: sent.append(text) or True)

    fired = []
    alert.check_balance_tripwire(fired)
    assert fired == []
    assert len(sent) == 1
    assert sent[0] == "💰 余额｜$33.8357 → $3.8455"
    assert "未授权" not in sent[0]
    assert state["balance_state.json"]["value"] == "3.8455"
    assert "pending_change" not in state["balance_state.json"]

    # The next minute is a skipped API poll, not a fictitious recovery/event.
    monkeypatch.setattr(alert.time, "time", lambda: 1060.0)
    monkeypatch.setattr(alert, "_balance_now",
                        lambda: (_ for _ in ()).throw(AssertionError("polled")))
    alert.check_balance_tripwire(fired)
    assert fired == [] and len(sent) == 1


def test_balance_send_failure_keeps_pending_and_retries(monkeypatch):
    alert = _load_alert()
    alert.CONF = {"BALANCE_POLL_MIN": "5", "BALANCE_FAIL_YELLOW": "3"}
    state = _state_io(monkeypatch, alert, {
        "balance_state.json": {"last_poll": 0, "fail_streak": 0,
                               "value": "10.00"},
    })
    outcomes = iter((False, True))
    sent = []
    monkeypatch.setattr(alert.time, "time", lambda: 1000.0)
    monkeypatch.setattr(alert, "_balance_now", lambda: (True, "12.00"))
    monkeypatch.setattr(
        alert.tg, "send",
        lambda text: sent.append(text) or next(outcomes))

    alert.check_balance_tripwire([])
    st = state["balance_state.json"]
    assert st["value"] == "10.00"
    assert st["pending_change"]["to"] == "12.00"

    # Retry pending delivery on the next minute without waiting five minutes or
    # performing another account read.
    monkeypatch.setattr(alert.time, "time", lambda: 1060.0)
    monkeypatch.setattr(alert, "_balance_now",
                        lambda: (_ for _ in ()).throw(AssertionError("polled")))
    alert.check_balance_tripwire([])
    st = state["balance_state.json"]
    assert len(sent) == 2 and st["value"] == "12.00"
    assert "pending_change" not in st


def test_skipped_balance_poll_keeps_endpoint_failure_active(monkeypatch):
    alert = _load_alert()
    alert.CONF = {"BALANCE_POLL_MIN": "5", "BALANCE_FAIL_YELLOW": "3"}
    _state_io(monkeypatch, alert, {
        "balance_state.json": {"last_poll": 1000, "fail_streak": 3,
                               "value": "10.00"},
    })
    monkeypatch.setattr(alert.time, "time", lambda: 1060.0)
    monkeypatch.setattr(alert.tg, "send",
                        lambda text: (_ for _ in ()).throw(AssertionError(text)))

    fired = []
    alert.check_balance_tripwire(fired)
    assert fired == [(alert.tg.YELLOW, "balance_endpoint",
                      "余额端点连续 3 次读取失败")]


def test_main_runs_order_events_after_fault_state_machine(monkeypatch):
    alert = _load_alert()
    calls = []
    monkeypatch.setattr(alert, "collect", lambda: calls.append("collect") or [])
    monkeypatch.setattr(alert.time, "time", lambda: 1000.0)
    monkeypatch.setattr(alert.tg, "read_json", lambda _name, default=None: {})
    monkeypatch.setattr(alert.tg, "write_json",
                        lambda name, _obj: calls.append("write:" + name))
    monkeypatch.setattr(alert.tg, "log", lambda text: calls.append("log:" + text))
    monkeypatch.setattr(alert, "check_order_activity_monitor",
                        lambda: calls.append("orders"))

    alert.main()
    assert calls == ["collect", "write:alert_state.json", "orders"]


def test_order_activity_child_has_hard_timeout(monkeypatch):
    alert = _load_alert()
    alert.CONF = {"ORDER_ACTIVITY_TIMEOUT_S": "7"}
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        raise alert.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(alert.subprocess, "run", run)
    ok, detail = alert._run_order_activity()

    assert ok is False and "7 秒" in detail
    command, kwargs = seen[0]
    assert command[-1].endswith("/deploy/tg_orders.py")
    assert kwargs["timeout"] == 7


def test_persistent_order_monitor_failure_warns_then_recovers(monkeypatch):
    alert = _load_alert()
    alert.CONF = {"ORDER_ACTIVITY_FAIL_YELLOW": "3"}
    state = _state_io(monkeypatch, alert, {
        "order_activity_health.json": {},
    })
    outcomes = iter(((False, "退出码 1"), (False, "退出码 1"),
                     (False, "退出码 1"), (True, "")))
    moments = iter((1000.0, 1060.0, 1120.0, 1180.0))
    sent = []
    monkeypatch.setattr(alert, "_run_order_activity", lambda: next(outcomes))
    monkeypatch.setattr(alert.time, "time", lambda: next(moments))
    monkeypatch.setattr(alert.tg, "log", lambda _text: None)
    monkeypatch.setattr(alert.tg, "send",
                        lambda text: sent.append(text) or True)

    for _ in range(4):
        alert.check_order_activity_monitor()

    assert len(sent) == 2
    assert "连续 3 次失败" in sent[0]
    assert "已恢复:order_activity" in sent[1]
    health = state["order_activity_health.json"]
    assert health["fail_streak"] == 0 and "alerted" not in health
