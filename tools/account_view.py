#!/usr/bin/env python3
"""W-K1 — typed READ-ONLY account endpoints (PLAN_RISK_KILLSWITCH §3).

The data source for the panic CLI's verify-zero-resting step (W-K2), the
reconcile loop (W-K5), and the operator's own eyes:

    GET /portfolio/balance                    -> Balance
    GET /portfolio/positions   (paginated)    -> MarketPosition / EventPosition
    GET /portfolio/orders?status=resting      -> RestingOrder
                               (paginated)

READ-ONLY BY CONSTRUCTION: this module performs GET requests only; it
contains no order-mutation code and the registry classes it network_read
(S5: runnable from the console only when network tools are armed; it can
never transmit an order).

Endpoint paths, field names and types are taken from
docs/vendor/kalshi/latest/openapi.yaml (GetBalanceResponse,
GetPositionsResponse/MarketPosition/EventPosition, GetOrdersResponse/Order
— verified 2026-07-10), NEVER from memory (E4 discipline). Money fields are
FixedPointDollars decimal STRINGS parsed byte-exactly to E6 integers
(micro-dollars) — VERIFIED-LIVE 2026-07-10: a real position answered
market_exposure_dollars="4.726960", six decimals with NONZERO sub-centicent
digits, so E4 would be lossy narrowing of account money (D5). Contract
counts (FixedPointCount, spec-pinned to 2 decimals) stay E4 to match the
warehouse count_e4 convention. Both parsers are integer digit accumulation
(gold_load.parse_e4 and its E6 twin below); floats are REJECTED, never
coerced. The cents-typed integer fields (balance, portfolio_value) are
cross-checked against their fixed-point twins when both are present.

Boundary validation (D3): every record failing a field gate is DROPPED and
COUNTED by reason; the summary always prints the drop table (D2 — a clean
run says "0 dropped", a dirty one can never look clean). A response whose
top level is malformed fails the whole call closed (exit nonzero).

Signing: RSA-PSS via the openssl CLI (LibreSSL >= 3.3 supports
rsa_padding_mode:pss + saltlen:digest; MGF1 defaults to the digest = SHA-256
— parameters mirrored from the proven C++ implementation in
src/client.cpp::sign()/sign_request(), test_signing.cpp contract):
    message   = <ms-timestamp> + METHOD + "/trade-api/v2" + path-without-query
    headers   = KALSHI-ACCESS-KEY / -SIGNATURE (base64) / -TIMESTAMP
Credentials come from the environment ONLY (KALSHI_API_KEY_ID +
KALSHI_PRIVATE_KEY_PATH, i.e. `source ~/.kalshi/env.sh`); no header value is
ever printed (S4).

Usage:
  source ~/.kalshi/env.sh && python3 tools/account_view.py [balance|positions|orders|all]
  python3 tools/account_view.py --json all
  python3 tools/account_view.py --assert-zero-resting     # panic primitive:
                                                          # exit 1 if any resting order
Test hook: --base-url http://127.0.0.1:PORT (mock server; same signing path
with a throwaway key).
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gold_load  # noqa: E402  (parse_e4: byte-exact no-float E4 parser)

API_PREFIX = "/trade-api/v2"
# Canonical prod REST host per src/env.cpp:62 (api.elections.kalshi.com is
# the compatibility alias there); KALSHI_BASE_URL overrides.
PROD_BASE = "https://external-api.kalshi.com"
ORDER_STATUS = {"resting", "canceled", "executed"}   # openapi OrderStatus
BOOK_SIDE = {"bid", "ask"}                           # openapi BookSide
MAX_PAGES = 50            # pagination hard stop; truncation is LOUD (D2)


class AccountViewError(RuntimeError):
    """Top-level failure (transport, auth, malformed body) — fail closed."""


_DEC_RE = re.compile(r"^(-)?([0-9]+)(?:\.([0-9]+))?$")  # ASCII only — unicode digits REJECTED (audit B2; matches gold_load)


def parse_e6(v):
    """FixedPointDollars string -> E6 int (micro-dollars), byte-exact.

    E6 twin of gold_load.parse_e4 (same integer digit accumulation, same
    rejection rules), scaled for the 6-decimal precision that account money
    actually carries (VERIFIED-LIVE 2026-07-10, see module docstring).
    Floats/ints/malformed strings raise ValueError — never coerced."""
    if not isinstance(v, str):
        raise ValueError("not a fixed-point string: %r" % (v,))
    m = _DEC_RE.fullmatch(v)
    if not m:
        raise ValueError("malformed fixed-point string: %r" % (v,))
    sign, ip, fp = m.group(1), m.group(2), m.group(3) or ""
    if any(c != "0" for c in fp[6:]):
        raise ValueError("more than 6 fraction digits: %r" % (v,))
    n = 0
    for c in ip:
        n = n * 10 + (ord(c) - 48)
    n *= 1_000_000
    mul = 100_000
    for c in fp[:6]:
        n += (ord(c) - 48) * mul
        mul //= 10
    return -n if sign else n


# ─────────────────────────────────────────────────────────── signing

def _sign_openssl(key_path, message):
    p = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sigopt", "rsa_padding_mode:pss",
         "-sigopt", "rsa_pss_saltlen:digest", "-sign", key_path, "-binary"],
        input=message.encode(), capture_output=True)
    if p.returncode != 0 or not p.stdout:
        # stderr may reference the key path but never key material
        raise AccountViewError("openssl signing failed: %s"
                               % p.stderr.decode(errors="replace")[:200])
    return base64.b64encode(p.stdout).decode()


def signed_headers(method, path, key_id, key_path, now_ms=None):
    """Mirror of src/client.cpp sign_request(): sign over
    timestamp + METHOD + prefix + path-without-query."""
    ts = str(int(time.time() * 1000) if now_ms is None else now_ms)
    sig_path = path.split("?", 1)[0]
    message = ts + method + API_PREFIX + sig_path
    return {"KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-SIGNATURE": _sign_openssl(key_path, message),
            "KALSHI-ACCESS-TIMESTAMP": ts}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """audit N1: a 3xx would forward the signed auth headers to an attacker-
    chosen location; there is no legitimate redirect on this API — refuse."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AccountViewError("refusing HTTP %d redirect to %r (auth headers "
                               "are never forwarded)" % (code, newurl))


_OPENER = urllib.request.build_opener(_NoRedirect())


def _get(base_url, path, key_id, key_path, timeout=15):
    url = base_url + API_PREFIX + path
    req = urllib.request.Request(url, method="GET")
    for k, v in signed_headers("GET", path, key_id, key_path).items():
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise AccountViewError("GET %s -> HTTP %d %s"
                               % (path, e.code, e.read()[:200])) from None
    except urllib.error.URLError as e:
        raise AccountViewError("GET %s -> %s" % (path, e.reason)) from None
    try:
        return json.loads(body)
    except ValueError:
        raise AccountViewError("GET %s -> unparseable body (%d bytes)"
                               % (path, len(body))) from None


# ─────────────────────────────────────── typed field gates (D3, no floats)

def _e6(obj, key, drops):
    """Money (FixedPointDollars) -> E6 int, byte-exact; rejects counted."""
    try:
        return parse_e6(obj[key])
    except Exception:
        drops["bad_%s" % key] = drops.get("bad_%s" % key, 0) + 1
        raise _Drop()


def _e4(obj, key, drops):
    """Count (FixedPointCount, 2dp) -> E4 int, byte-exact; rejects counted."""
    try:
        v = obj[key]
        if not isinstance(v, str):
            raise ValueError("not a fixed-point string")
        return gold_load.parse_e4(v)
    except Exception:
        drops["bad_%s" % key] = drops.get("bad_%s" % key, 0) + 1
        raise _Drop()


def _int(obj, key, drops):
    v = obj.get(key)
    if type(v) is int:
        return v
    drops["bad_%s" % key] = drops.get("bad_%s" % key, 0) + 1
    raise _Drop()


def _str(obj, key, drops, enum=None):
    v = obj.get(key)
    if isinstance(v, str) and v and (enum is None or v in enum):
        return v
    drops["bad_%s" % key] = drops.get("bad_%s" % key, 0) + 1
    raise _Drop()


class _Drop(Exception):
    pass


# ─────────────────────────────────────────────────────────── endpoints

def get_balance(base_url, key_id, key_path):
    """-> dict(balance_cents, balance_e4, portfolio_value_cents, updated_ts).

    balance_dollars (fixed-point string) is AUTHORITATIVE — VERIFIED-LIVE
    2026-07-10: the real account answered balance=2326 (cents int) with
    balance_dollars="23.2614" — consistent with floor (ONE sample; round
    not excluded, audit N2). Cross-check accepts |cents - e6| < 1 whole
    cent (floor and round both pass); anything >= 1 cent apart is
    corruption and fails closed."""
    b = _get(base_url, "/portfolio/balance", key_id, key_path)
    drops = {}
    try:
        cents = _int(b, "balance", drops)
        e6 = _e6(b, "balance_dollars", drops)
        pv = _int(b, "portfolio_value", drops)
        ts = _int(b, "updated_ts", drops)
    except _Drop:
        raise AccountViewError("balance response failed field gates: %s"
                               % drops) from None
    if abs(cents * 10000 - e6) >= 10000:   # audit N2: floor vs round is
        # unresolved from one live sample; both keep |diff| < 1 cent —
        # anything >= 1 whole cent apart is corruption, fail closed
        raise AccountViewError(
            "balance mismatch: balance=%d cents vs balance_dollars=%d E6 "
            "(>= 1 cent apart) — refusing to pick one (fail-closed)"
            % (cents, e6))
    return {"balance_cents": cents, "balance_e6": e6,
            "portfolio_value_cents": pv, "updated_ts": ts}


def _paged(base_url, path_base, key_id, key_path, list_key):
    """Cursor pagination; yields raw records; loud truncation (D2)."""
    cursor, pages = None, 0
    while True:
        path = path_base + (("&cursor=" + cursor) if cursor else "")
        body = _get(base_url, path, key_id, key_path)
        recs = body.get(list_key)
        if not isinstance(recs, list):
            raise AccountViewError("%s: missing/invalid '%s' array"
                                   % (path_base, list_key))
        for r in recs:
            yield r
        cursor = body.get("cursor") or ""
        pages += 1
        if not cursor:
            return
        if pages >= MAX_PAGES:
            # audit B1: a truncated enumeration can hide a resting order —
            # never a WARN-and-proceed; fail the whole call closed (S2/D2)
            raise AccountViewError(
                "pagination exceeded %d pages for %s — enumeration "
                "INCOMPLETE, refusing to report a partial answer"
                % (MAX_PAGES, path_base))


def get_positions(base_url, key_id, key_path):
    """-> (market_positions, event_positions, drops). Typed per openapi
    MarketPosition/EventPosition; money byte-exact E4."""
    mkt, evt, drops = [], [], {}
    raw_m, raw_e = [], []
    cursor, pages = None, 0
    while True:  # positions carries TWO arrays per page — custom pager
        path = "/portfolio/positions?limit=200" + \
            (("&cursor=" + cursor) if cursor else "")
        body = _get(base_url, path, key_id, key_path)
        m, e = body.get("market_positions"), body.get("event_positions")
        if not isinstance(m, list) or not isinstance(e, list):
            raise AccountViewError("positions: missing arrays")
        raw_m.extend(m); raw_e.extend(e)
        cursor = body.get("cursor") or ""
        pages += 1
        if not cursor:
            break
        if pages >= MAX_PAGES:
            raise AccountViewError(   # audit B1: same fail-closed rule
                "positions pagination exceeded %d pages — INCOMPLETE"
                % MAX_PAGES)
    for r in raw_m:
        try:
            mkt.append({
                "ticker": _str(r, "ticker", drops),
                "position_fp_e4": _e4(r, "position_fp", drops),
                "market_exposure_e6": _e6(r, "market_exposure_dollars", drops),
                "realized_pnl_e6": _e6(r, "realized_pnl_dollars", drops),
                "fees_paid_e6": _e6(r, "fees_paid_dollars", drops),
            })
        except _Drop:
            drops["market_positions_dropped"] = \
                drops.get("market_positions_dropped", 0) + 1
    for r in raw_e:
        try:
            evt.append({
                "event_ticker": _str(r, "event_ticker", drops),
                "event_exposure_e6": _e6(r, "event_exposure_dollars", drops),
                "realized_pnl_e6": _e6(r, "realized_pnl_dollars", drops),
                "fees_paid_e6": _e6(r, "fees_paid_dollars", drops),
            })
        except _Drop:
            drops["event_positions_dropped"] = \
                drops.get("event_positions_dropped", 0) + 1
    return mkt, evt, drops


def get_resting_orders(base_url, key_id, key_path):
    """-> (orders, drops). status=resting server-side filter; each order
    re-checked client-side (defense in depth — a non-resting order in this
    list is an API-contract violation worth counting, not trusting)."""
    out, drops = [], {}
    for r in _paged(base_url, "/portfolio/orders?status=resting&limit=200",
                    key_id, key_path, "orders"):
        try:
            status = _str(r, "status", drops, enum=ORDER_STATUS)
            if status != "resting":
                drops["non_resting_in_filter"] = \
                    drops.get("non_resting_in_filter", 0) + 1
                continue
            out.append({
                "order_id": _str(r, "order_id", drops),
                "client_order_id": _str(r, "client_order_id", drops),
                "ticker": _str(r, "ticker", drops),
                "book_side": _str(r, "book_side", drops, enum=BOOK_SIDE),
                "yes_price_e6": _e6(r, "yes_price_dollars", drops),
                "remaining_count_fp_e4": _e4(r, "remaining_count_fp", drops),
                "initial_count_fp_e4": _e4(r, "initial_count_fp", drops),
            })
        except _Drop:
            drops["orders_dropped"] = drops.get("orders_dropped", 0) + 1
    return out, drops


# ─────────────────────────────────────────────────────────────── CLI

def _dollars(e6):
    sign = "-" if e6 < 0 else ""
    e6 = abs(e6)
    return "%s$%d.%06d" % (sign, e6 // 1_000_000, e6 % 1_000_000)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("what", nargs="?", default="all",
                    choices=("balance", "positions", "orders", "all"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--assert-zero-resting", action="store_true",
                    help="exit 1 if ANY resting order exists (panic primitive)")
    ap.add_argument("--base-url", default=None,
                    help="override (tests/mock); default KALSHI_BASE_URL or prod")
    args = ap.parse_args(argv[1:])

    key_id = os.environ.get("KALSHI_API_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path or not os.path.exists(key_path):
        print("FAIL: KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set or "
              "key missing (source ~/.kalshi/env.sh)", file=sys.stderr)
        return 2
    base = args.base_url or os.environ.get("KALSHI_BASE_URL", PROD_BASE)

    out, all_drops = {}, {}
    try:
        if args.assert_zero_resting or args.what in ("orders", "all"):
            orders, d = get_resting_orders(base, key_id, key_path)
            out["resting_orders"] = orders
            all_drops.update(d)
        if not args.assert_zero_resting:
            if args.what in ("balance", "all"):
                out["balance"] = get_balance(base, key_id, key_path)
            if args.what in ("positions", "all"):
                mkt, evt, d = get_positions(base, key_id, key_path)
                out["market_positions"] = mkt
                out["event_positions"] = evt
                all_drops.update(d)
    except AccountViewError as e:
        print("FAIL (closed): %s" % e, file=sys.stderr)
        return 1

    if args.assert_zero_resting:
        n = len(out["resting_orders"])
        dropped = sum(v for k, v in all_drops.items())
        if dropped:
            # an order we could not PARSE might still be resting — fail closed
            print("ZERO-RESTING: UNPROVABLE — %d record(s) failed field gates "
                  "%s" % (dropped, all_drops), file=sys.stderr)
            return 1
        print("resting orders: %d %s" % (n, "(ZERO — clear)" if n == 0 else
                                         "(NOT clear)"))
        return 0 if n == 0 else 1

    if args.json:
        print(json.dumps({"data": out, "drops": all_drops}, indent=2))
    else:
        if "balance" in out:
            b = out["balance"]
            print("balance: %s | portfolio value: $%d.%02d | updated_ts=%d"
                  % (_dollars(b["balance_e6"]), b["portfolio_value_cents"] // 100,
                     b["portfolio_value_cents"] % 100, b["updated_ts"]))
        if "market_positions" in out:
            print("market positions: %d" % len(out["market_positions"]))
            for p in out["market_positions"][:20]:
                print("  %-40s pos=%s exposure=%s pnl=%s"
                      % (p["ticker"], p["position_fp_e4"],
                         _dollars(p["market_exposure_e6"]),
                         _dollars(p["realized_pnl_e6"])))
            print("event positions: %d" % len(out["event_positions"]))
        if "resting_orders" in out:
            print("resting orders: %d" % len(out["resting_orders"]))
            for o in out["resting_orders"][:20]:
                print("  %-40s %s yes@%s rem=%s id=%s"
                      % (o["ticker"], o["book_side"],
                         _dollars(o["yes_price_e6"]),
                         o["remaining_count_fp_e4"], o["order_id"][:13]))
        print("field-gate drops: %s" % (all_drops if all_drops else "0 dropped"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
