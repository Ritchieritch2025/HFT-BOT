"""W-K1 acceptance (PLAN_RISK_KILLSWITCH §3): typed read-only account
endpoints.

Fixtures are shaped exactly per docs/vendor/kalshi/latest/openapi.yaml
(GetBalanceResponse / GetPositionsResponse / GetOrdersResponse). Proves:
byte-exact money parsing to E6 micro-dollars (no floats — a numeric-typed
dollars field is REJECTED, never coerced; E6 because live position money
carries 6 decimals), counts to E4; D3 field gates drop+count malformed records; a
malformed top level fails the whole call closed; cents↔fixed-point
cross-check; pagination via cursor; the zero-resting panic primitive
(including its fail-closed answer when records are unparseable); the
signing recipe (message layout mirrors src/client.cpp; PSS signature
round-trips through openssl verify); credential redaction (headers never
printed); and the module's read-only guarantee (GET only, no mutation
endpoint strings).
"""
import base64
import http.server
import json
import os
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import account_view as av  # noqa: E402

# ── fixtures: exact openapi response shapes ─────────────────────────────

BALANCE_OK = {"balance": 4, "balance_dollars": "0.04",
              "portfolio_value": 0, "updated_ts": 1783600000}
# VERIFIED-LIVE shape 2026-07-10: fixed-point balance carries CENTICENT
# precision; the cents int is its floor (2326 == floor(232614/100))
BALANCE_CENTICENT = {"balance": 2326, "balance_dollars": "23.2614",
                     "portfolio_value": 0, "updated_ts": 1783600000}
BALANCE_MISMATCH = {"balance": 5, "balance_dollars": "0.04",
                    "portfolio_value": 0, "updated_ts": 1783600000}

ORDER_OK = {
    "order_id": "a1b2c3d4-0000-0000-0000-000000000001",
    "user_id": "u", "client_order_id": "coid-1", "ticker": "KXBTC-25DEC31-B50",
    "side": "yes", "action": "buy", "outcome_side": "yes", "book_side": "bid",
    "type": "limit", "status": "resting",
    "yes_price_dollars": "0.5600", "no_price_dollars": "0.4400",
    "fill_count_fp": "0.00", "remaining_count_fp": "5.00",
    "initial_count_fp": "5.00", "taker_fees_dollars": "0.0000",
    "maker_fees_dollars": "0.0000", "taker_fill_cost_dollars": "0.0000",
    "maker_fill_cost_dollars": "0.0000"}
ORDER_FLOAT_PRICE = dict(ORDER_OK, order_id="bad-float", client_order_id="c2",
                         yes_price_dollars=0.56)          # number, not string!
ORDER_BAD_SIDE = dict(ORDER_OK, order_id="bad-side", client_order_id="c3",
                      book_side="yes")                    # not in BookSide
ORDER_EXECUTED = dict(ORDER_OK, order_id="exec-1", client_order_id="c4",
                      status="executed")                  # leaked past filter

MKT_POS_OK = {"ticker": "KXBTC-25DEC31-B50", "total_traded_dollars": "1.2500",
              "position_fp": "-5.00", "market_exposure_dollars": "0.5600",
              "realized_pnl_dollars": "-0.0300", "fees_paid_dollars": "0.0175",
              "last_updated_ts": "2026-07-10T00:00:00Z"}
# VERIFIED-LIVE 2026-07-10: real position money carries 6 decimals with
# nonzero sub-centicent digits — the reason money is E6 here, not E4
MKT_POS_6DP = dict(MKT_POS_OK, ticker="KXMVECROSS-LIVE-SHAPE",
                   market_exposure_dollars="4.726960", position_fp="25.69")
EVT_POS_OK = {"event_ticker": "KXBTC-25DEC31", "total_cost_dollars": "1.2500",
              "total_cost_shares_fp": "5.00",
              "event_exposure_dollars": "0.5600",
              "realized_pnl_dollars": "-0.0300", "fees_paid_dollars": "0.0175"}


class _Handler(http.server.BaseHTTPRequestHandler):
    routes = {}
    seen = []

    def do_GET(self):
        _Handler.seen.append({"path": self.path,
                              "headers": dict(self.headers)})
        body = _Handler.routes.get(self.path.split("?")[0])
        if callable(body):
            body = body(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    _Handler.routes, _Handler.seen = {}, []
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


@pytest.fixture()
def creds(tmp_path, monkeypatch):
    key = tmp_path / "throwaway.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"],
                   capture_output=True, check=True)
    monkeypatch.setenv("KALSHI_API_KEY_ID", "test-key-id-0000")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(key))
    return "test-key-id-0000", str(key)


# ───────────────────────────────────────────────────────── signing

def test_signing_message_layout_and_pss_roundtrip(creds, tmp_path):
    key_id, key_path = creds
    hdrs = av.signed_headers("GET", "/portfolio/balance?limit=5",
                             key_id, key_path, now_ms=1783600000000)
    assert hdrs["KALSHI-ACCESS-KEY"] == key_id
    assert hdrs["KALSHI-ACCESS-TIMESTAMP"] == "1783600000000"
    # message = ts + METHOD + prefix + path WITHOUT query (src/client.cpp:279)
    message = "1783600000000GET/trade-api/v2/portfolio/balance"
    pub = tmp_path / "pub.pem"
    subprocess.run(["openssl", "rsa", "-in", key_path, "-pubout",
                    "-out", str(pub)], capture_output=True, check=True)
    sig = tmp_path / "sig.bin"
    sig.write_bytes(base64.b64decode(hdrs["KALSHI-ACCESS-SIGNATURE"]))
    v = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sigopt", "rsa_padding_mode:pss",
         "-sigopt", "rsa_pss_saltlen:digest", "-verify", str(pub),
         "-signature", str(sig)], input=message.encode(), capture_output=True)
    assert v.returncode == 0 and b"Verified OK" in v.stdout


# ────────────────────────────────────────────── typed parsing, byte-exact

def test_balance_typed_and_cross_checked(server, creds):
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/balance"] = BALANCE_OK
    b = av.get_balance(base, *creds)
    assert b == {"balance_cents": 4, "balance_e6": 40000,
                 "portfolio_value_cents": 0, "updated_ts": 1783600000}


def test_parse_e6_byte_exact_no_float():
    """E6 money parser: VERIFIED-LIVE 6dp values land exactly; floats,
    ints and >6dp rejected (never coerced)."""
    assert av.parse_e6("4.726960") == 4_726_960
    assert av.parse_e6("-0.008000") == -8_000
    assert av.parse_e6("23.2614") == 23_261_400
    for bad in (0.56, 56, None, "1e3", "0x10", "1.2345678"):  # 7 nonzero dp
        with pytest.raises(Exception):
            av.parse_e6(bad)
    assert av.parse_e6("1.2345670") == 1_234_567  # zero beyond 6dp tolerated


def test_balance_centicent_precision_cents_is_floor(server, creds):
    """The live-verified shape: sub-cent true balance, cents field = floor.
    The fixed-point value is authoritative and preserved byte-exactly."""
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/balance"] = BALANCE_CENTICENT
    b = av.get_balance(base, *creds)
    assert b["balance_e6"] == 23_261_400 and b["balance_cents"] == 2326


def test_balance_cents_vs_fixedpoint_mismatch_fails_closed(server, creds):
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/balance"] = BALANCE_MISMATCH
    with pytest.raises(av.AccountViewError, match="mismatch"):
        av.get_balance(base, *creds)


def test_orders_byte_exact_e4_and_field_gates(server, creds):
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/orders"] = {
        "orders": [ORDER_OK, ORDER_FLOAT_PRICE, ORDER_BAD_SIDE,
                   ORDER_EXECUTED], "cursor": ""}
    orders, drops = av.get_resting_orders(base, *creds)
    assert len(orders) == 1
    o = orders[0]
    assert o["yes_price_e6"] == 560_000       # "0.5600" byte-exact E6
    assert o["remaining_count_fp_e4"] == 50000  # "5.00" -> 5*10000
    assert o["book_side"] == "bid"
    # D3/D2: every rejection counted by reason, never silent
    assert drops["bad_yes_price_dollars"] == 1     # float, not string
    assert drops["bad_book_side"] == 1
    assert drops["non_resting_in_filter"] == 1
    assert drops["orders_dropped"] == 2


def test_positions_typed_negative_position(server, creds):
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/positions"] = {
        "market_positions": [MKT_POS_OK, MKT_POS_6DP],
        "event_positions": [EVT_POS_OK], "cursor": ""}
    mkt, evt, drops = av.get_positions(base, *creds)
    assert mkt[0]["position_fp_e4"] == -50000       # "-5.00" count stays E4
    assert mkt[0]["realized_pnl_e6"] == -30_000     # "-0.0300"
    assert mkt[0]["fees_paid_e6"] == 17_500         # "0.0175"
    assert evt[0]["event_exposure_e6"] == 560_000
    # the live-verified 6dp shape parses EXACTLY (this is why money is E6)
    assert mkt[1]["market_exposure_e6"] == 4_726_960
    assert mkt[1]["position_fp_e4"] == 256_900      # "25.69"
    assert drops == {}


def test_pagination_follows_cursor(server, creds):
    _srv, base = server
    def orders_route(path):
        if "cursor=NEXT" in path:
            return {"orders": [dict(ORDER_OK, order_id="page2-xxxxxxxxx",
                                    client_order_id="c9")], "cursor": ""}
        return {"orders": [ORDER_OK], "cursor": "NEXT"}
    _Handler.routes["/trade-api/v2/portfolio/orders"] = orders_route
    orders, drops = av.get_resting_orders(base, *creds)
    assert len(orders) == 2 and drops == {}


def test_malformed_top_level_fails_closed(server, creds):
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/orders"] = b"not json at all"
    with pytest.raises(av.AccountViewError, match="unparseable"):
        av.get_resting_orders(base, *creds)
    _Handler.routes["/trade-api/v2/portfolio/orders"] = {"nope": True}
    with pytest.raises(av.AccountViewError, match="missing"):
        av.get_resting_orders(base, *creds)


# ───────────────────────────────────────── zero-resting panic primitive

def _cli(base, *extra):
    return av.main(["account_view", "--base-url", base] + list(extra))


def test_assert_zero_resting(server, creds, capsys):
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/orders"] = {"orders": [],
                                                         "cursor": ""}
    assert _cli(base, "--assert-zero-resting") == 0
    assert "ZERO — clear" in capsys.readouterr().out
    _Handler.routes["/trade-api/v2/portfolio/orders"] = {
        "orders": [ORDER_OK], "cursor": ""}
    assert _cli(base, "--assert-zero-resting") == 1


def test_assert_zero_resting_unprovable_when_records_unparseable(
        server, creds, capsys):
    """An order that fails field gates might still be resting at the
    exchange — 'zero resting' must be UNPROVABLE, exit 1 (S2)."""
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/orders"] = {
        "orders": [ORDER_FLOAT_PRICE], "cursor": ""}
    assert _cli(base, "--assert-zero-resting") == 1
    assert "UNPROVABLE" in capsys.readouterr().err


# ─────────────────────────────────────────────── hygiene: S4 + read-only

def test_no_credential_material_in_output(server, creds, capsys):
    _srv, base = server
    _Handler.routes["/trade-api/v2/portfolio/balance"] = BALANCE_OK
    _Handler.routes["/trade-api/v2/portfolio/positions"] = {
        "market_positions": [], "event_positions": [], "cursor": ""}
    _Handler.routes["/trade-api/v2/portfolio/orders"] = {"orders": [],
                                                         "cursor": ""}
    assert _cli(base, "all") == 0
    out = capsys.readouterr()
    hdrs = {k.lower(): v for k, v in _Handler.seen[0]["headers"].items()}
    sig = hdrs["kalshi-access-signature"]     # urllib title-cases header names
    assert sig                                 # header actually sent
    for secret in ("test-key-id-0000", sig[:24]):
        assert secret not in out.out and secret not in out.err
    assert "0 dropped" in out.out


def test_module_is_read_only_by_construction():
    src = open(os.path.join(ROOT, "tools", "account_view.py")).read()
    assert 'method="GET"' in src
    for verb in ('method="POST"', 'method="DELETE"', 'method="PUT"',
                 "orders/batched", "/amend", "/decrease"):
        assert verb not in src, verb
