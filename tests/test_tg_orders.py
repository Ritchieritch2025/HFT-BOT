"""Acceptance tests for the one-shot Telegram order activity monitor."""

import json
import os
import stat
import sys

import pytest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "deploy"))
import tg_orders as orders  # noqa: E402


ORDER = {
    "order_id": "order-0000000000000001",
    "user_id": "user",
    "client_order_id": "",  # UI/manual orders legitimately leave this empty
    "ticker": "KXSPORTS-TEAM-A",
    "side": "yes",
    "action": "buy",
    "outcome_side": "yes",
    "book_side": "bid",
    "type": "limit",
    "status": "resting",
    "yes_price_dollars": "0.930000",
    "no_price_dollars": "0.070000",
    "fill_count_fp": "0.00",
    "remaining_count_fp": "32.09",
    "initial_count_fp": "32.09",
    "taker_fees_dollars": "0.000000",
    "maker_fees_dollars": "0.000000",
    "taker_fill_cost_dollars": "0.000000",
    "maker_fill_cost_dollars": "0.000000",
    "created_time": "2026-07-17T00:16:29Z",
    "last_update_time": "2026-07-17T00:16:29Z",
}

EXECUTED = dict(
    ORDER,
    type="market",
    status="executed",
    fill_count_fp="32.09",
    remaining_count_fp="0.00",
    taker_fees_dollars="0.146500",
    taker_fill_cost_dollars="29.843700",
    last_update_time="2026-07-17T00:16:30Z",
)

FILL = {
    "fill_id": "fill-0000000000000001",
    "trade_id": "fill-0000000000000001",
    "order_id": ORDER["order_id"],
    "ticker": ORDER["ticker"],
    "market_ticker": ORDER["ticker"],
    "side": "yes",
    "action": "buy",
    "outcome_side": "yes",
    "book_side": "bid",
    "count_fp": "32.09",
    "yes_price_dollars": "0.930000",
    "no_price_dollars": "0.070000",
    "is_taker": True,
    "fee_cost": "0.146500",
    "created_time": "2026-07-17T00:16:30Z",
    "ts": 1784247390,
}

SETTLEMENT = {
    "ticker": ORDER["ticker"],
    "event_ticker": "KXSPORTS-TEAM",
    "market_result": "yes",
    "yes_count_fp": "32.09",
    "yes_total_cost_dollars": "29.843700",
    "no_count_fp": "0.00",
    "no_total_cost_dollars": "0.000000",
    "revenue": 3209,
    "settled_time": "2026-07-17T01:02:52Z",
    "fee_cost": "0.146500",
    "value": 100,
}

MARKET = {
    "ticker": ORDER["ticker"],
    "event_ticker": "KXSPORTS-TEAM",
    "title": "New York M vs Philadelphia Winner?",
    "subtitle": "",
    "yes_sub_title": "New York M",
    "no_sub_title": "Philadelphia",
}

PARTIAL = dict(
    ORDER,
    status="resting",
    fill_count_fp="10.00",
    remaining_count_fp="22.09",
    taker_fees_dollars="0.050000",
    taker_fill_cost_dollars="9.300000",
    last_update_time="2026-07-17T00:16:45Z",
)

CANCELED_PARTIAL = dict(
    PARTIAL,
    status="canceled",
    last_update_time="2026-07-17T00:17:30Z",
)


class FakeReader:
    def __init__(self, recent=None, resting=None, fills=None, settlements=None,
                 details=None, markets=None):
        self.recent = list(recent or [])
        self.resting = list(resting or [])
        self.fill_rows = list(fills or [])
        self.settlement_rows = list(settlements or [])
        self.details = dict(details or {})
        self.markets = dict(markets or {ORDER["ticker"]: MARKET})
        self.detail_calls = []
        self.market_calls = []
        self.min_ts_calls = []

    def orders(self, min_ts, max_ts):
        self.min_ts_calls.append(("orders", min_ts, max_ts))
        return self.recent

    def resting_orders(self):
        return self.resting

    def order(self, order_id):
        self.detail_calls.append(order_id)
        return self.details[order_id]

    def market(self, ticker):
        self.market_calls.append(ticker)
        return self.markets[ticker]

    def fills(self, min_ts, max_ts):
        self.min_ts_calls.append(("fills", min_ts, max_ts))
        return self.fill_rows

    def settlements(self, min_ts, max_ts):
        self.min_ts_calls.append(("settlements", min_ts, max_ts))
        return self.settlement_rows


def poll(tmp_path, reader, sender, now):
    return orders.poll_once(
        reader=reader,
        sender=sender,
        state_path=str(tmp_path / "orders.json"),
        now_epoch=now,
        conf={"ORDER_ACTIVITY_LOOKBACK_S": "300",
              "ORDER_ACTIVITY_RETENTION_DAYS": "30",
              "ORDER_ACTIVITY_BATCH_CHARS": "3500",
              "ORDER_ACTIVITY_TIMEZONE": "America/New_York"},
    )


def test_first_successful_poll_baselines_without_replaying_history(tmp_path):
    sent = []
    reader = FakeReader(recent=[EXECUTED], fills=[FILL],
                        settlements=[SETTLEMENT])
    result = poll(tmp_path, reader, lambda text: sent.append(text) or True,
                  now=1_000_000)

    assert result == {"bootstrapped": True, "queued": 1,
                      "delivered": 1, "pending": 0}
    assert len(sent) == 1 and "订单推送已启用" in sent[0]
    assert "下单" not in sent[0] and "💱" not in sent[0]
    state_path = tmp_path / "orders.json"
    state = json.loads(state_path.read_text())
    assert state["bootstrapped"] is True
    assert set(state["orders"]) == {ORDER["order_id"]}
    assert set(state["fills"]) == {FILL["fill_id"]}
    assert len(state["settlements"]) == 1
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.tmp"))


def test_instant_executed_order_fill_and_settlement_are_aggregated_once(tmp_path):
    poll(tmp_path, FakeReader(), lambda _text: True, now=2_000_000)
    sent = []
    reader = FakeReader(recent=[EXECUTED], fills=[FILL],
                        settlements=[SETTLEMENT])
    result = poll(
        tmp_path,
        reader,
        lambda text: sent.append(text) or True,
        now=2_000_060,
    )

    assert result["queued"] == 3  # creation, actual fill, and settlement
    assert result["pending"] == 0
    assert len(sent) == 1
    message = sent[0]
    assert message.splitlines() == [
        "🆕 下单｜New York M / Philadelphia｜买 YES 32.09 @ $0.93｜完成",
        "💱 成交｜New York M / Philadelphia｜买 YES 32.09 @ $0.93｜成交耗时 1s",
        "🏁 结算｜New York M / Philadelphia｜YES｜净 +$2.0998",
    ]
    for noise in ("Kalshi 订单动态", "YES选项", "费用", "成交额",
                  "taker", "#", "来源", "EDT"):
        assert noise not in message
    assert reader.market_calls == [ORDER["ticker"]]  # cached across 2 facts

    # The overlap deliberately returns the same durable records again.
    again = []
    repeated = poll(
        tmp_path,
        FakeReader(recent=[EXECUTED], fills=[FILL],
                   settlements=[SETTLEMENT]),
        lambda text: again.append(text) or True,
        now=2_000_120,
    )
    assert repeated["queued"] == 0 and again == []


def test_order_status_fill_remaining_fee_and_cost_changes(tmp_path):
    poll(tmp_path, FakeReader(recent=[ORDER], resting=[ORDER]),
         lambda _text: True, now=3_000_000)
    sent = []
    result = poll(tmp_path, FakeReader(recent=[EXECUTED], fills=[FILL]),
                  lambda text: sent.append(text) or True, now=3_000_060)

    assert result["queued"] == 2  # status snapshot + authoritative fill fact
    assert len(sent) == 1
    message = sent[0]
    assert message.splitlines() == [
        "✅ 完成｜New York M / Philadelphia｜买 YES｜32.09/32.09",
        "💱 成交｜New York M / Philadelphia｜买 YES 32.09 @ $0.93｜成交耗时 1s",
    ]


def test_partial_fill_then_cancel_are_two_meaningful_changes(tmp_path):
    poll(tmp_path, FakeReader(recent=[ORDER], resting=[ORDER]),
         lambda _text: True, now=3_500_000)
    messages = []
    first = poll(tmp_path,
                 FakeReader(recent=[PARTIAL], resting=[PARTIAL]),
                 lambda text: messages.append(text) or True,
                 now=3_500_060)
    second = poll(tmp_path,
                  FakeReader(recent=[CANCELED_PARTIAL]),
                  lambda text: messages.append(text) or True,
                  now=3_500_120)

    assert first["queued"] == 1 and second["queued"] == 1
    assert messages[0] == (
        "🟡 部成｜New York M / Philadelphia｜买 YES｜+10（共 10/32.09）")
    assert messages[1] == (
        "❌ 撤单｜New York M / Philadelphia｜买 YES｜已成 10/32.09")


def test_old_resting_order_disappearance_is_resolved_by_single_get(tmp_path):
    poll(tmp_path, FakeReader(recent=[ORDER], resting=[ORDER]),
         lambda _text: True, now=4_000_000)
    reader = FakeReader(details={ORDER["order_id"]: EXECUTED}, fills=[FILL])
    sent = []
    poll(tmp_path, reader, lambda text: sent.append(text) or True,
         now=4_000_060)

    assert reader.detail_calls == [ORDER["order_id"]]
    assert len(sent) == 1 and "✅ 完成" in sent[0]


def test_fill_endpoint_is_backstop_when_order_snapshot_has_not_changed(tmp_path):
    poll(tmp_path, FakeReader(recent=[ORDER], resting=[ORDER]),
         lambda _text: True, now=5_000_000)
    sent = []
    result = poll(
        tmp_path,
        FakeReader(recent=[ORDER], resting=[ORDER], fills=[FILL]),
        lambda text: sent.append(text) or True,
        now=5_000_060,
    )
    assert result["queued"] == 1
    assert sent[0] == (
        "💱 成交｜New York M / Philadelphia｜买 YES 32.09 @ $0.93｜成交耗时 1s")


def test_compact_lines_include_order_fill_speed_and_observation_latency():
    order = orders.normalize_order(EXECUTED)
    fill = orders.normalize_fill(FILL)

    new_line = orders._format_new_order(order, MARKET, None, 1_784_247_400)
    fill_line = orders._format_fills([fill], MARKET, None, order,
                                     1_784_247_400)

    assert new_line.endswith("｜观测 11s")
    assert fill_line.endswith("｜成交耗时 1s｜观测 10s")
    assert "\n" not in new_line and "\n" not in fill_line


def test_fee_and_cost_only_snapshot_rewrite_is_notification_noise(tmp_path):
    poll(tmp_path, FakeReader(recent=[ORDER], resting=[ORDER]),
         lambda _text: True, now=5_500_000)
    accounting_only = dict(
        ORDER, taker_fees_dollars="0.010000",
        taker_fill_cost_dollars="0.930000",
        last_update_time="2026-07-17T00:17:00Z")
    sent = []
    result = poll(tmp_path,
                  FakeReader(recent=[accounting_only],
                             resting=[accounting_only]),
                  lambda text: sent.append(text) or True,
                  now=5_500_060)

    assert result["queued"] == 0 and sent == []


def test_delivery_failure_persists_one_pending_fact_and_retries(tmp_path):
    poll(tmp_path, FakeReader(), lambda _text: True, now=6_000_000)
    failed_messages = []
    failed = poll(
        tmp_path,
        FakeReader(recent=[EXECUTED], fills=[FILL]),
        lambda text: failed_messages.append(text) or False,
        now=6_000_060,
    )
    assert failed["queued"] == 2 and failed["pending"] == 2
    state = json.loads((tmp_path / "orders.json").read_text())
    assert len(state["pending"]) == 2

    retried = []
    recovered = poll(
        tmp_path,
        FakeReader(recent=[EXECUTED], fills=[FILL]),
        lambda text: retried.append(text) or True,
        now=6_000_120,
    )
    assert recovered["queued"] == 0
    assert recovered["delivered"] == 2 and recovered["pending"] == 0
    assert len(retried) == 1 and retried[0] == failed_messages[0]


def test_single_oversize_event_is_never_truncated_or_acked(tmp_path):
    poll(tmp_path, FakeReader(), lambda _text: True, now=6_500_000)
    state_path = tmp_path / "orders.json"
    state = json.loads(state_path.read_text())
    state["pending"] = [{"id": "oversize", "sequence": 1,
                         "created_epoch": 6_500_001, "text": "x" * 4000}]
    state["next_sequence"] = 2
    state_path.write_text(json.dumps(state))
    sent = []

    with pytest.raises(orders.OrderActivityError, match="exceeds"):
        poll(tmp_path, FakeReader(), lambda text: sent.append(text) or True,
             now=6_500_060)
    assert sent == []
    assert json.loads(state_path.read_text())["pending"][0]["id"] == "oversize"


def test_timestamp_only_rewrite_is_noise(tmp_path):
    poll(tmp_path, FakeReader(recent=[ORDER], resting=[ORDER]),
         lambda _text: True, now=7_000_000)
    rewritten = dict(ORDER, last_update_time="2026-07-17T00:17:00Z")
    sent = []
    result = poll(tmp_path, FakeReader(recent=[rewritten], resting=[rewritten]),
                  lambda text: sent.append(text) or True, now=7_000_060)
    assert result["queued"] == 0 and sent == []


def test_corrupt_state_never_becomes_silent_bootstrap(tmp_path):
    state_path = tmp_path / "orders.json"
    state_path.write_text("{broken")
    with pytest.raises(orders.OrderActivityError, match="cannot read state"):
        orders.poll_once(reader=FakeReader(), sender=lambda _text: True,
                         state_path=str(state_path), now_epoch=8_000_000,
                         conf={})


def test_valid_json_with_invalid_schema_never_becomes_silent_bootstrap(tmp_path):
    state_path = tmp_path / "orders.json"
    state_path.write_text(json.dumps({"version": 1, "bootstrapped": True}))
    with pytest.raises(orders.OrderActivityError, match="last_poll_epoch"):
        orders.poll_once(reader=FakeReader(), sender=lambda _text: True,
                         state_path=str(state_path), now_epoch=8_000_000,
                         conf={})


def test_stale_order_regression_fails_without_advancing_state(tmp_path):
    poll(tmp_path, FakeReader(recent=[PARTIAL], resting=[PARTIAL]),
         lambda _text: True, now=8_500_000)
    state_path = tmp_path / "orders.json"
    before = state_path.read_text()
    sent = []

    with pytest.raises(orders.OrderActivityError, match="fill_count regressed"):
        poll(tmp_path, FakeReader(recent=[ORDER], resting=[ORDER]),
             lambda text: sent.append(text) or True, now=8_500_060)
    assert sent == []
    assert state_path.read_text() == before


def test_reader_uses_recent_overlap_pagination_and_only_get_paths():
    seen = []

    def get_fn(path, key_id, key_path):
        seen.append((path, key_id, key_path))
        if path.startswith("/portfolio/orders?"):
            if "status=resting" in path:
                return {"orders": [], "cursor": ""}
            if "cursor=NEXT%2F1" in path:
                return {"orders": [EXECUTED], "cursor": ""}
            return {"orders": [ORDER], "cursor": "NEXT/1"}
        if path.startswith("/portfolio/fills?"):
            return {"fills": [FILL], "cursor": ""}
        if path.startswith("/portfolio/settlements?"):
            return {"settlements": [SETTLEMENT], "cursor": ""}
        if path.startswith("/portfolio/orders/"):
            return {"order": EXECUTED}
        if path.startswith("/markets/"):
            return {"market": MARKET}
        raise AssertionError(path)

    reader = orders.KalshiReader("key-id", "/tmp/key.pem", get_fn=get_fn)
    assert len(reader.orders(1234, 1300)) == 2
    assert reader.resting_orders() == []
    assert reader.fills(1234, 1300) == [FILL]
    assert reader.settlements(1234, 1300) == [SETTLEMENT]
    assert reader.order(ORDER["order_id"]) == EXECUTED
    assert reader.market(ORDER["ticker"]) == MARKET
    paths = [row[0] for row in seen]
    assert any("min_ts=1234" in path for path in paths)
    assert any("max_ts=1300" in path for path in paths)
    assert any("cursor=NEXT%2F1" in path for path in paths)
    assert all(path.startswith(("/portfolio/", "/markets/")) for path in paths)
    assert all(key_id == "key-id" and key_path == "/tmp/key.pem"
               for _path, key_id, key_path in seen)


def test_reader_rejects_missing_pagination_cursor():
    reader = orders.KalshiReader(
        "key-id", "/tmp/key.pem",
        get_fn=lambda _path, _key_id, _key_path: {"orders": []})
    with pytest.raises(orders.OrderActivityError, match="invalid cursor"):
        reader.orders(1234, 1300)


def test_settlements_accept_conforming_terminal_page_without_cursor():
    reader = orders.KalshiReader(
        "key-id", "/tmp/key.pem",
        get_fn=lambda _path, _key_id, _key_path: {"settlements": [SETTLEMENT]})
    assert reader.settlements(1234, 1300) == [SETTLEMENT]


def test_market_metadata_failure_is_attempted_once_per_ticker_per_poll(tmp_path):
    poll(tmp_path, FakeReader(), lambda _text: True, now=8_750_000)
    sent = []
    reader = FakeReader(recent=[EXECUTED], fills=[FILL],
                        settlements=[SETTLEMENT],
                        markets={"UNRELATED": MARKET})
    result = poll(tmp_path, reader, lambda text: sent.append(text) or True,
                  now=8_750_060)

    assert result["queued"] == 3 and len(sent) == 1
    assert reader.market_calls == [ORDER["ticker"]]
    assert ORDER["ticker"] in sent[0]


def test_fixed_point_fields_reject_floats_and_do_not_advance_state(tmp_path):
    bad = dict(ORDER, yes_price_dollars=0.93)
    with pytest.raises(orders.OrderActivityError,
                       match="yes_price_dollars must be"):
        poll(tmp_path, FakeReader(recent=[bad]), lambda _text: True,
             now=9_000_000)
    assert not (tmp_path / "orders.json").exists()


def test_downtime_catchup_uses_fixed_bounded_window(tmp_path):
    first = FakeReader()
    poll(tmp_path, first, lambda _text: True, now=10_000_000)
    second = FakeReader()
    poll(tmp_path, second, lambda _text: True, now=10_010_000)

    assert second.min_ts_calls == [
        ("orders", 9_999_700, 10_000_300),
        ("fills", 9_999_700, 10_000_300),
        ("settlements", 9_999_700, 10_000_300),
    ]
    state = json.loads((tmp_path / "orders.json").read_text())
    assert state["last_poll_epoch"] == 10_000_300


def test_order_immutable_identity_change_fails_closed(tmp_path):
    poll(tmp_path, FakeReader(recent=[ORDER], resting=[ORDER]),
         lambda _text: True, now=10_500_000)
    changed = dict(ORDER, ticker="OTHER-TICKER",
                   last_update_time="2026-07-17T00:18:00Z")

    with pytest.raises(orders.OrderActivityError,
                       match="immutable ticker changed"):
        poll(tmp_path, FakeReader(recent=[changed], resting=[changed]),
             lambda _text: True, now=10_500_060)
