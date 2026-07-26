#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mm_engine — automatic quote/cancel market maker, BTC15M + ETH15M.

MODE=shadow (default): full logic runs, order calls are logged only.
MODE=live: real orders via V2 /portfolio/events/orders (post_only GTC).
Same code path both modes (audit rule 10.6): live = routing flip.

Strategy (ground-up thesis, frozen; F0 2026-07-25):
  * side selection by the PRICING KERNEL: fair = 100*p_settle_above from
    the live RTI stream; a side is quoted only when its quote price is
    at least MARGIN_C cheap vs kernel fair.  No kernel price (sigma
    unavailable / RTI stale) -> no quotes, fail closed.
  * both sides eligible, price-improve +0.1c over best bid of each side
    (tail bands only), clamped to (opposite ask - 0.1c); post_only.
  * zones by MARKET mid: tte 10-30m all; 5-10m only |mid-50|>10;
    2-5m only mid<20 or >80; <2m never (hard cancel).
  * inventory: max 1 fill/side/market; after a fill quote only the
    opposite side in that market; total open entry cost <= $150.
  * sentinel: |kernel FV - mid| > 15c -> pause market; RTI stale > 5s
    -> cancel all + pause; realized loss <= -$50 -> HALT (kill).
  * requote: book moved -> cancel+replace, max 1 replace/s/side.
Receipts: NDJSON hourly, every INTENT/ACK/REJ/FILL/SETTLE, src refs.
"""
import asyncio, base64, collections, json, math, os, sys, time, uuid, urllib.request
from pathlib import Path
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

sys.path.insert(0, "/home/ubuntu")
import rti_pricing as rp
from mm_control import (
    ControlError,
    STATUS_SCHEMA,
    atomic_write_json,
    default_control,
    read_control,
    validate_control,
)

MODE = os.environ.get("MM_MODE", "shadow")
WS = "wss://api.elections.kalshi.com/trade-api/ws/v2"
REST = "https://api.elections.kalshi.com/trade-api/v2"
V2O = "https://external-api.kalshi.com/trade-api/v2"
SERIES = ("KXBTC15M",)          # operator 2026-07-25: single-market focus
IDX = {"KXBTC15M": "BRTI"}
CLIP = os.environ.get("MM_CLIP", "5.00")
IMP = 0.001                       # +0.1c improvement
SENTINEL_C = 15.0
MARGIN_C = float(os.environ.get("MM_MARGIN", "0.3"))  # kernel-fair edge gate, cents
GAMMA_C = float(os.environ.get("MM_GAMMA", "0.5"))  # F1 skew retreat, cents/contract
MIN_REQUOTE_C = float(os.environ.get("MM_MIN_REQUOTE", "0.5"))  # F5, cents:
    # hold a WANTED resting order unless the target moved at least this
    # far (D7: 18,854 rate blocks vs 232 orders from 0.1c-move requoting).
    # Risk-off cancels (side no longer wanted) bypass the threshold.
                                                    # derivation: docs/research_reports/
                                                    # MM_SKEW_GAMMA_DERIVATION.md
MAX_OPEN_COST = float(os.environ.get("MM_MAX_COST", "50"))
MAX_NET = int(os.environ.get("MM_MAX_NET", "6"))
HARD_MAX_OPEN_COST = float(os.environ.get("MM_HARD_MAX_COST", "200"))
HARD_MAX_CLIP = os.environ.get("MM_HARD_MAX_CLIP", "20.00")
HARD_MAX_NET = int(os.environ.get("MM_HARD_MAX_NET", "100"))
KILL_LOSS = -50.0
OUT = Path(os.environ.get("MM_OUT", "/home/ubuntu/h6b_inputs/mm_engine"))
OUT.mkdir(parents=True, exist_ok=True)
CTRL_PATH = Path(os.environ.get(
    "MM_CTRL", "/home/ubuntu/h6b_inputs/mm_control.json"))
STATUS_PATH = Path(os.environ.get(
    "MM_STATUS", "/home/ubuntu/h6b_inputs/mm_control_status.json"))

KEY_ID = os.environ["KALSHI_API_KEY_ID"]
PRIV = serialization.load_pem_private_key(
    Path(os.environ["KALSHI_PRIV_KEY_PATH"]
         if "KALSHI_PRIV_KEY_PATH" in os.environ
         else os.environ["KALSHI_PRIVATE_KEY_PATH"]).read_bytes(), password=None)

def _sig(method, path):
    ts = str(int(time.time() * 1000))
    m = f"{ts}{method}/trade-api/v2{path}".encode()
    s = PRIV.sign(m, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                 salt_length=padding.PSS.DIGEST_LENGTH),
                  hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(s).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}

def rest(method, path, body=None, host=REST):
    req = urllib.request.Request(host + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers=_sig(method, path))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}
    except Exception as e:
        return -1, {"error": repr(e)[:200]}

class Log:
    def __init__(self): self.h=None; self.f=None; self.n=0; self.fn=""
    def w(self, o):
        o["wall_ns"]=time.time_ns(); h=time.strftime("%Y%m%dT%H", time.gmtime())
        if h!=self.h:
            if self.f: self.f.flush(); os.fsync(self.f.fileno()); self.f.close()
            self.h=h; self.fn=f"mm_{h}.ndjson"; self.n=0
            self.f=open(OUT/self.fn,"a")
        self.n+=1; o["src"]=f"{self.fn}#{self.n}"
        self.f.write(json.dumps(o,separators=(",",":"))+"\n"); self.f.flush()
L = Log()

class S:
    rti = {s: collections.deque(maxlen=300) for s in SERIES}
    rti_t = {s: 0.0 for s in SERIES}
    meta = {}                  # mt -> (series, close_s, strike)
    books = {}
    orders = {}                # (mt, side) -> {id, px, t}
    latch = set()              # (mt, side) fills
    open_cost = 0.0
    realized = 0.0
    halted = False
    last_replace = {}
    # D1 fix: /portfolio/fills min_ts is SECONDS.  The 2026-07-25 loss
    # root cause was a milliseconds cursor here -> empty fills forever ->
    # every inventory control read a dead source.
    fills_cursor = int(time.time())
    net_pos = {}          # mt -> {"y": int, "n": int} filled contracts
    tokens = 8.0          # A4 rate limiter (8 req burst, 4/s refill)
    tok_t = time.time()
    rej_429 = 0
    limit_breached = False
    recon_fails = 0       # F2 consecutive /portfolio/positions failures
    settle_zero_streak = 0  # F4 consecutive zero-revenue settlements
    settled_seen = set()    # F4 dedupe of applied settlements
    requote_suppressed = 0  # F5 sub-threshold holds (observability)
    fills_seen = set()      # fill dedupe across WS channel + REST poll
    last_control_cancel = 0.0
    control_error = ""


CTRL = validate_control(
    default_control(
        max_open_cost=MAX_OPEN_COST,
        clip=CLIP,
        max_net=MAX_NET,
        # A live process without an operator control document starts paused.
        paused=(MODE == "live"),
    ),
    hard_max_cost=HARD_MAX_OPEN_COST,
    hard_max_clip=HARD_MAX_CLIP,
    hard_max_net=HARD_MAX_NET,
)
_ctrl_t = 0.0
_control_loaded = False


def write_control_status():
    """Publish the engine's effective control state for the console."""
    try:
        atomic_write_json(STATUS_PATH, {
            "schema_version": STATUS_SCHEMA,
            "observed_at_ns": time.time_ns(),
            "mode": MODE,
            "revision": CTRL["revision"],
            "effective": {
                "max_open_cost": MAX_OPEN_COST,
                "clip": CLIP,
                "max_net": MAX_NET,
                "paused": bool(CTRL["paused"]),
                "kill": bool(CTRL["kill"]),
            },
            "halted": bool(S.halted),
            "limit_breached": bool(S.limit_breached),
            "open_orders": len(S.orders),
            "exposure": round(exposure(), 6),
            "realized": round(S.realized, 6),
            "control_error": S.control_error,
        })
    except Exception as exc:
        # A status-file failure must never weaken the limits already in memory.
        S.control_error = f"status write failed: {exc}"[:200]


def apply_control_document(document):
    """Validate and apply one complete control document.

    The console cannot raise a runtime value beyond the immutable startup hard
    ceilings.  A kill is latched in memory and requires an engine restart after
    the operator clears the file-side kill flag.
    """
    global MAX_OPEN_COST, CLIP, MAX_NET, _control_loaded
    candidate = validate_control(
        document,
        hard_max_cost=HARD_MAX_OPEN_COST,
        hard_max_clip=HARD_MAX_CLIP,
        hard_max_net=HARD_MAX_NET,
    )
    if MODE == "live" and not candidate["paused"]:
        raise ControlError(
            "live resume disabled until exchange order/fill reconciliation exists"
        )
    if _control_loaded:
        if candidate["revision"] < CTRL["revision"]:
            raise ControlError("control revision moved backwards")
        if candidate["revision"] == CTRL["revision"] and candidate != CTRL:
            raise ControlError("control content changed without a new revision")
        if candidate == CTRL:
            return False

    previous = dict(CTRL)
    CTRL.clear()
    CTRL.update(candidate)
    _control_loaded = True
    MAX_OPEN_COST = candidate["max_open_cost"]
    CLIP = candidate["clip"]
    MAX_NET = candidate["max_net"]
    S.control_error = ""
    if candidate["kill"]:
        S.halted = True

    changed = {
        key: candidate[key]
        for key in candidate
        if previous.get(key) != candidate[key]
    }
    L.w({
        "ev": "CONTROL_APPLIED",
        "revision": candidate["revision"],
        "changed": changed,
        "max_open_cost": MAX_OPEN_COST,
        "clip": CLIP,
        "max_net": MAX_NET,
        "paused": candidate["paused"],
        "kill": candidate["kill"],
    })

    # Existing orders retain their original quantity in the risk ledger, but a
    # clip change still drains them before any quote is rebuilt at the new size.
    if previous.get("clip") != candidate["clip"]:
        cancel_all("CONTROL_CLIP_CHANGED")
    if candidate["paused"] or candidate["kill"]:
        cancel_all("CONTROL_" + ("KILL" if candidate["kill"] else "PAUSE"))
    elif exposure() > MAX_OPEN_COST:
        cancel_all("CONTROL_LIMIT_REDUCED")
    S.limit_breached = exposure() > MAX_OPEN_COST
    write_control_status()
    return True


def load_control(force=False):
    """Hot-reload validated operator limits.

    Market callbacks use the one-second throttle; the independent control
    watcher and the final pre-send gate pass ``force=True``.
    """
    global _ctrl_t
    now = time.monotonic()
    if not force and now - _ctrl_t < 1.0:
        return False
    _ctrl_t = now
    try:
        document = read_control(
            CTRL_PATH,
            hard_max_cost=HARD_MAX_OPEN_COST,
            hard_max_clip=HARD_MAX_CLIP,
            hard_max_net=HARD_MAX_NET,
        )
        return apply_control_document(document)
    except FileNotFoundError:
        # Shadow keeps its environment defaults.  A live process treats loss
        # of its control document as an operator-control failure, not as
        # permission to keep quoting on the last remembered limits.
        if MODE == "live" and not S.halted:
            CTRL["paused"] = True
            S.halted = True
            S.control_error = "control document missing; live engine halted"
            cancel_all("CONTROL_MISSING")
            L.w({"ev": "CONTROL_REJECTED", "reason": S.control_error})
            write_control_status()
        return False
    except (ControlError, OSError) as exc:
        message = str(exc)[:200]
        if S.control_error != message:
            S.control_error = message
            if MODE == "live":
                CTRL["paused"] = True
                S.halted = True
                cancel_all("CONTROL_INVALID")
            L.w({"ev": "CONTROL_REJECTED", "reason": message})
            write_control_status()
        return False

def take_token():
    now = time.time()
    S.tokens = min(8.0, S.tokens + (now - S.tok_t) * 4.0)
    S.tok_t = now
    if S.tokens < 1.0:
        return False
    S.tokens -= 1.0
    return True

def exposure():
    """A1: live exposure from OUR OWN book of resting orders + fills,
    computed synchronously — never waits for a poll."""
    resting = sum(
        od["px"] * float(od.get("qty", CLIP)) for od in S.orders.values()
    )
    filled = sum(v["cost"] for v in S.net_pos.values())
    return resting + filled


def within_cap(price, quantity=None):
    """Worst-case reservation check for one additional resting order."""
    quantity = float(CLIP if quantity is None else quantity)
    return exposure() + float(price) * quantity <= MAX_OPEN_COST + 1e-9


EPS=1e-3
def best(bk): return max((p for p,v in bk.items() if (v or 0)>EPS), default=None)


def q_px(bid_e4, ask_e4):
    """Legal quote price: improve 0.1c only in tapered tail bands."""
    in_tail = bid_e4 < 1000 or bid_e4 > 9000
    improvement = IMP if in_tail else 0.0
    return min(bid_e4 / 10000.0 + improvement,
               ask_e4 / 10000.0 - 0.001)


def legal_floor(px):
    """Snap a price DOWN to its band's legal tick (A3: deci-cent only
    <10c / >90c, whole cents in the mid band).  Down = further from the
    market = the conservative direction for a retreated quote."""
    if px < 0.10 or px > 0.90:
        return math.floor(px * 1000 + 1e-9) / 1000
    return math.floor(px * 100 + 1e-9) / 100


def skew_px(px, adverse_ct):
    """F1 inventory skew: retreat the ACCUMULATING side GAMMA_C cents per
    contract of net inventory, snapped down to a legal tick.  The
    offsetting side (adverse_ct <= 0) is never touched — it reduces
    inventory and stays at the baseline quote."""
    if adverse_ct <= 0:
        return px
    return legal_floor(px - GAMMA_C * adverse_ct / 100.0)

def order_place(mt, side, exchange_px, *, risk_px=None, edge_c=None):
    """Submit one order after a final control/risk check.

    ``exchange_px`` is the wire price.  ``risk_px`` is the contract cost used
    by the reservation ledger; they differ when buying NO through a YES ask.
    The returned quantity is captured after the final control reload so later
    clip changes cannot rewrite this order's historical risk.
    """
    # Close the decision->send race: controls and capital are checked again
    # immediately before the code can reach a live POST.
    load_control(force=True)
    if CTRL["paused"] or CTRL["kill"] or S.halted:
        L.w({"ev":"CONTROL_SKIP","mt":mt,"side":side,
             "revision":CTRL["revision"]})
        return None
    quantity = float(CLIP)
    risk_px = float(exchange_px if risk_px is None else risk_px)
    if not within_cap(risk_px, quantity):
        L.w({"ev":"CAP_SKIP","mt":mt,"side":side,"px":exchange_px,
             "risk_px":risk_px,"qty":quantity,"exposure":round(exposure(),4),
             "max_open_cost":MAX_OPEN_COST})
        return None
    if not take_token():                      # A4
        L.w({"ev":"RATE_SKIP","mt":mt,"side":side}); return None
    body = {"ticker": mt, "client_order_id": str(uuid.uuid4()), "side": side,
            "count": CLIP, "price": f"{exchange_px:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "maker", "post_only": True}
    if MODE == "live":
        t0 = time.monotonic()
        code, resp = rest("POST", "/portfolio/events/orders", body, host=V2O)
        ms = round((time.monotonic() - t0) * 1000.0, 1)
        oid = (resp.get("order_id") or (resp.get("order") or {}).get("order_id")
               or resp.get("id"))
        L.w({"ev":"ORDER_ACK" if code in (200,201) else "ORDER_REJ",
             "mt":mt,"side":side,"px":exchange_px,"risk_px":risk_px,
             "qty":quantity,"code":code,"ms":ms,"edge_c":edge_c,
             "oid":oid,"resp":str(resp)[:200]})
        return (oid, quantity) if code in (200,201) and oid else None
    L.w({"ev":"INTENT_PLACE","mt":mt,"side":side,"px":exchange_px,
         "risk_px":risk_px,"qty":quantity,"edge_c":edge_c,"mode":"shadow"})
    return "shadow-"+str(uuid.uuid4())[:8], quantity

def order_cancel(mt, side, oid, reason=""):
    if MODE == "live" and oid and not oid.startswith("shadow-"):
        t0 = time.monotonic()
        code, resp = rest("DELETE", f"/portfolio/events/orders/{oid}", host=V2O)
        ms = round((time.monotonic() - t0) * 1000.0, 1)
        ok = code in (200, 201, 204, 404)     # 404 = already gone
        L.w({"ev":"CANCEL_ACK" if ok else "CANCEL_FAIL",
             "mt":mt,"side":side,"oid":oid,"code":code,"ms":ms,
             "reason":reason})
        return ok
    else:
        L.w({"ev":"INTENT_CANCEL","mt":mt,"side":side,"oid":oid,
             "reason":reason})
    return True

def cancel_all(reason):
    all_canceled = True
    for (mt, side), od in list(S.orders.items()):
        if order_cancel(mt, side, od["id"], reason=reason):
            S.orders.pop((mt, side), None)
        else:
            all_canceled = False
    L.w({"ev":"CANCEL_ALL","reason":reason,
         "complete":all_canceled,"remaining":len(S.orders)})
    return all_canceled

def zone_ok(tte, mid_c):
    # Measured facts (2026-07-25, receipts):
    #  - 15M markets list ~878s (14.6m) pre-close, p10-p90 875-879s,
    #    n=120, zero overlap => quote-from-listing == tte<=~880s always;
    #    the old tte>=1800 guard never fired once (dead code, removed).
    #  - zones from settle-truth map (E2, 610M contracts):
    #    >=600s all strikes; 300-600s only |mid-50|>10; 120-300s tails;
    #    <120s graveyard (-3.5c measured, RED_OK).
    if tte < 120: return False
    if tte >= 600: return True
    if tte >= 300: return abs(mid_c-50) > 10
    return mid_c < 20 or mid_c > 80

def think():
    load_control()
    now = time.time()
    if CTRL.get("kill"):
        S.halted = True
    if CTRL.get("paused") or S.halted:
        if S.orders and now - S.last_control_cancel >= 1.0:
            cancel_all("CONTROL_ENFORCE")
            S.last_control_cancel = now
            write_control_status()
        return
    for mt, (ser, close_s, strike) in list(S.meta.items()):
        tte = close_s - now
        if tte <= 0:
            for side in ("bid","ask_no"):
                od = S.orders.pop((mt,side), None)
                if od: order_cancel(mt, side, od["id"], reason="expired")
            S.meta.pop(mt, None); continue
        ticks = list(S.rti[ser]); bk = S.books.get(mt)
        if len(ticks) < 31 or not bk: continue
        if now - S.rti_t[ser] > 5:
            cancel_all("anchor_stale"); return
        sigma = rp.sigma_from_ticks(ticks)
        yb, nb = best(bk["y"]), best(bk["n"])
        if yb is None or nb is None: continue
        ya = 10000 - nb
        mid_c = (yb + ya) / 200.0
        # F0 (2026-07-25): the kernel IS the side selector.  V6 on 101
        # real fills: kernel edge sign correct (YES +4.27c -> +30.09c,
        # NO -3.87c -> -25.81c) while this engine picked sides from
        # market mid / wind fair and bought the kernel-negative side.
        # The unvalidated wind layer (D6) is stripped; no kernel price,
        # no quotes (fail closed).
        if sigma is None or sigma <= 0:
            continue
        p = rp.p_settle_above(ticks[-1], strike, sigma, tte)
        if p is None:
            continue
        fair_c = 100.0 * p
        if abs(fair_c - mid_c) > SENTINEL_C:
            for side in ("bid","ask_no"):
                od = S.orders.pop((mt,side), None)
                if od: order_cancel(mt, side, od["id"], reason="sentinel")
            L.w({"ev":"SENTINEL_PAUSE","mt":mt,"fv":round(fair_c,1),"mid":mid_c})
            continue
        ok = zone_ok(tte, mid_c)
        # two quotes: buy YES (side "bid" @ yes book) and buy NO
        # (side "ask_no": V2 side=ask == sell yes == buy no at no-price)
        want = {}
        np_ = S.net_pos.get(mt, {"y":0,"n":0,"cost":0.0})
        net = np_["y"] - np_["n"]
        # F1: graduated retreat of the accumulating side (GAMMA_C c/ct),
        # applied BEFORE the hard MAX_NET latch ever binds.
        y_px = skew_px(q_px(yb, ya), net)
        n_px = skew_px(q_px(10000-ya, 10000-yb), -net)
        # F0 side gate: only rest on a side that is CHEAP vs KERNEL fair.
        # RTI drops -> kernel fair drops -> bid side turns rich -> cancel
        # NOW (without waiting for the book); mirror for the NO side.
        edge_bid = fair_c - y_px*100.0
        edge_no  = (100.0 - fair_c) - n_px*100.0
        exp_now = exposure()                                   # A1 sync
        room = exp_now < MAX_OPEN_COST
        if exp_now >= MAX_OPEN_COST:                           # A6
            cancel_all("EXPOSURE_CAP")
            still_breached = exposure() > MAX_OPEN_COST
            if not S.limit_breached or still_breached != S.limit_breached:
                L.w({"ev":"LIMIT_BREACH","exposure":round(exposure(),2),
                     "max_open_cost":MAX_OPEN_COST,
                     "filled_exposure":still_breached})
            S.limit_breached = still_breached
            write_control_status()
            return
        S.limit_breached = False
        # A5 hard inventory latch: |net| >= MAX_NET blocks the side outright
        skew_block_bid = net >= MAX_NET
        skew_block_no  = -net >= MAX_NET
        want["bid"] = ok and (mt,"bid") not in S.latch and y_px >= 0.001 \
                      and room and edge_bid >= MARGIN_C and not skew_block_bid
        want["ask_no"] = ok and (mt,"ask_no") not in S.latch and n_px >= 0.001 \
                      and room and edge_no >= MARGIN_C and not skew_block_no
        for side, w, px in (("bid", want["bid"], y_px), ("ask_no", want["ask_no"], n_px)):
            od = S.orders.get((mt, side))
            if od and w and 0.001 <= abs(od["px"]-px) < MIN_REQUOTE_C/100.0:
                S.requote_suppressed += 1          # F5: hold, don't churn
                continue
            if od and (not w or abs(od["px"]-px) >= 0.001):
                if now - S.last_replace.get((mt,side),0) < 1.0 and w: continue
                if order_cancel(mt, side, od["id"],
                                reason="reprice" if w else "risk_off"):
                    S.orders.pop((mt,side), None)
                    S.last_replace[(mt,side)] = now
                    od = None
                else:
                    L.w({"ev":"CANCEL_RETRY_HOLD","mt":mt,"side":side})
                    continue
            if od is None and w:
                assert (mt, side) not in S.orders, "A2 violated"
                if not within_cap(px, float(CLIP)):
                    L.w({"ev":"CAP_SKIP","mt":mt,"side":side,"px":px,
                         "qty":float(CLIP),"exposure":round(exposure(),4),
                         "max_open_cost":MAX_OPEN_COST})
                    continue
                if side == "bid":
                    placed = order_place(mt, "bid", px, risk_px=px,
                                         edge_c=round(edge_bid, 2))
                else:   # buy NO == rest an ask on the YES book at 1-no_px
                    placed = order_place(
                        mt, "ask", round(1.0 - px, 4), risk_px=px,
                        edge_c=round(edge_no, 2)
                    )
                if placed:
                    oid, quantity = placed
                    S.orders[(mt,side)] = {
                        "id":oid, "px":px, "qty":quantity, "t":now
                    }

def websockets_headers():
    ts = str(int(time.time()*1000))
    m = f"{ts}GET/trade-api/ws/v2".encode()
    s = PRIV.sign(m, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(s).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts}

async def cf_task():
    while True:
        try:
            async with websockets.connect(WS, additional_headers=websockets_headers(),
                                          ping_interval=10) as ws:
                await ws.send(json.dumps({"id":1,"cmd":"subscribe","params":{
                    "channels":["cfbenchmarks_value"],
                    "index_ids":["BRTI","ETHUSD_RTI"]}}))
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") != "cfbenchmarks_value": continue
                    d = json.loads(m["msg"]["data"]); v = float(d["value"])
                    idx = m["msg"]["index_id"]
                    for ser, ix in IDX.items():
                        if ix == idx:
                            S.rti[ser].append(v); S.rti_t[ser] = time.time()
                    think()
        except Exception as e:
            L.w({"ev":"CF_ERR","err":repr(e)[:150]}); await asyncio.sleep(2)

def fetch_open():
    out = {}
    import datetime as dtm
    for ser in SERIES:
        code, d = rest("GET", f"/markets?series_ticker={ser}&status=open&limit=100")
        for mk in (d.get("markets") or []):
            try:
                cs = dtm.datetime.fromisoformat(mk["close_time"].replace("Z","+00:00")).timestamp()
                out[mk["ticker"]] = (ser, cs, float(mk["floor_strike"]))
            except Exception: pass
    return out

MD_SESSION_S = 120.0        # rotate subscription (re-fetch open markets)
MD_RECV_TIMEOUT_S = 15.0    # silent-channel watchdog


async def _md_session(ws):
    """One subscription session; ALWAYS returns within MD_SESSION_S.

    The old `async for raw in ws` checked its 120s budget only when a
    message ARRIVED — after the subscribed market settled, the silent
    channel hung the loop forever, fetch_open() never re-ran and the
    engine sat at markets=0 while the exchange had an open market
    (found live in shadow, 2026-07-26; the earlier 'markets=0 blip').
    """
    t0 = time.time()
    while time.time() - t0 <= MD_SESSION_S:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=MD_RECV_TIMEOUT_S)
        except asyncio.TimeoutError:
            continue
        m = json.loads(raw); t = m.get("type"); msg = m.get("msg",{})
        mt = msg.get("market_ticker","")
        if t == "orderbook_snapshot":
            def lv(a):
                o={}
                for px,q in (a or []): o[int(round(float(px)*10000))]=float(q)
                return o
            S.books[mt]={"y":lv(msg.get("yes_dollars_fp") or msg.get("yes")),
                         "n":lv(msg.get("no_dollars_fp") or msg.get("no"))}
        elif t == "orderbook_delta":
            bk=S.books.setdefault(mt,{"y":{},"n":{}})
            k="y" if msg["side"]=="yes" else "n"
            px=int(round(float(msg["price_dollars"])*10000)) if msg.get("price_dollars") else int(msg.get("price",0))
            dq=float(msg.get("delta_fp") or msg.get("delta",0))
            nv=bk[k].get(px,0)+dq
            if abs(nv)<=EPS: bk[k].pop(px,None)
            else: bk[k][px]=nv
            think()


async def md_task():
    while True:
        try:
            S.meta.update(fetch_open())
            tickers = [mt for mt,(s,cs,_k) in S.meta.items() if cs > time.time()]
            if not tickers: await asyncio.sleep(20); continue
            async with websockets.connect(WS, additional_headers=websockets_headers(),
                                          ping_interval=10) as ws:
                await ws.send(json.dumps({"id":2,"cmd":"subscribe","params":{
                    "channels":["orderbook_delta"],"market_tickers":tickers}}))
                await _md_session(ws)
        except Exception as e:
            L.w({"ev":"MD_ERR","err":repr(e)[:150]}); await asyncio.sleep(2)

# ------------------------------------------------------------ F3 fills
def fill_created_s(f):
    """Fill timestamp in SECONDS regardless of the wire encoding."""
    ts = f.get("created_ts")
    if ts is not None:
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            return None
        return ts // 1000 if ts > 10 ** 12 else ts
    ct = f.get("created_time")
    if ct:
        try:
            import datetime as dtm
            return int(dtm.datetime.fromisoformat(
                str(ct).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError):
            return None
    return None


def fill_price_dollars(f, side):
    """Side-correct fill cost in dollars (cents ints normalized)."""
    px = f.get("yes_price") if side == "bid" else f.get("no_price")
    if px is None:
        px = f.get("price", 0)
    try:
        px = float(px)
    except (TypeError, ValueError):
        return 0.0
    return px / 100.0 if px > 1.0 else px


def ws_fill_to_rest(msg):
    """Normalize a WS 'fill' channel message to the REST fills shape so
    both paths feed the ONE apply_fill ledger."""
    return {"ticker": msg.get("market_ticker") or msg.get("ticker"),
            "side": msg.get("side"), "count": msg.get("count"),
            "yes_price": msg.get("yes_price"),
            "no_price": msg.get("no_price"),
            "created_ts": msg.get("ts") or msg.get("created_ts"),
            "trade_id": msg.get("trade_id")}


def apply_fill(f):
    """One exchange fill: advance the cursor, latch the side, move cost
    from the resting-order reservation to the filled ledger (A1 stays
    conserved — never double-counted, never dropped).  Deduped by
    trade_id so the WS push and the REST poll can both deliver it."""
    key = f.get("trade_id") or (f.get("ticker"), f.get("side"),
                                f.get("count"), f.get("created_ts"),
                                f.get("yes_price"), f.get("no_price"))
    if key in S.fills_seen:
        L.w({"ev": "FILL_DUP_SKIP", "key": str(key)[:120]})
        return
    S.fills_seen.add(key)
    ts = fill_created_s(f)
    if ts is not None:
        S.fills_cursor = max(S.fills_cursor, ts + 1)
    L.w({"ev": "FILL", "raw": {k: f.get(k) for k in (
        "ticker", "side", "count", "price", "yes_price", "no_price",
        "created_ts", "created_time")}})
    mt = f.get("ticker")
    side = "bid" if f.get("side") in ("yes", "bid") else "ask_no"
    S.latch.add((mt, side))
    try:
        n = float(f.get("count", 0))
        px = fill_price_dollars(f, side)
        d = S.net_pos.setdefault(mt, {"y": 0, "n": 0, "cost": 0.0})
        d["y" if side == "bid" else "n"] += n
        d["cost"] += px * n
        S.open_cost = sum(v["cost"] for v in S.net_pos.values())
        od = S.orders.get((mt, side))
        if od is not None:
            od["qty"] = float(od.get("qty", CLIP)) - n
            if od["qty"] <= 1e-9:
                S.orders.pop((mt, side), None)
    except Exception as exc:
        L.w({"ev": "FILL_APPLY_ERR", "err": repr(exc)[:150]})


def fills_visibility_selfcheck():
    """Startup gate (live): prove the fills pipeline can actually SEE
    fills before any quoting.  The D1 bug shape — a newest fill exists
    but a min_ts probe just below it returns empty — must fail here."""
    code, d = rest("GET", "/portfolio/fills?limit=1")
    if code != 200:
        return False, f"fills endpoint HTTP {code}"
    fills = d.get("fills") or []
    if not fills:
        return True, "no historical fills to verify against (new account)"
    ts = fill_created_s(fills[0])
    if ts is None:
        return False, "cannot parse fill timestamp"
    code2, d2 = rest("GET", f"/portfolio/fills?min_ts={ts - 1}&limit=100")
    if code2 != 200:
        return False, f"min_ts probe HTTP {code2}"
    if not (d2.get("fills") or []):
        return False, ("min_ts probe returned empty while a fill exists — "
                       "unit bug (D1 class), refusing to trade blind")
    return True, f"pipeline sees fill at ts={ts}"


# ----------------------------------------------------------- F4 breaker
ECON_HALT_LOSS = float(os.environ.get("MM_ECON_HALT", "-2.0"))  # dollars
ECON_ZERO_STREAK = 2   # consecutive zero-revenue settlements -> halt


def _cents(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def apply_settlement(s):
    """One EXCHANGE settlement record (the only admissible P&L source —
    2026-07-25 lesson: never judge P&L from balance or memory).  Trips
    the economic breaker on (a) two consecutive zero-revenue settlements
    with nonzero cost — the exact incident signature — or (b) cumulative
    realized below ECON_HALT_LOSS.  The halt does not auto-resume."""
    key = (s.get("ticker"), s.get("settled_time"))
    if key in S.settled_seen:
        return False
    S.settled_seen.add(key)
    revenue = _cents(s.get("revenue"))
    cost = _cents(s.get("yes_total_cost")) + _cents(s.get("no_total_cost"))
    pnl = (revenue - cost) / 100.0
    S.realized += pnl
    if revenue <= 0 and cost > 0:
        S.settle_zero_streak += 1
    else:
        S.settle_zero_streak = 0
    L.w({"ev": "SETTLE", "ticker": s.get("ticker"),
         "result": s.get("market_result"), "revenue_c": revenue,
         "cost_c": cost, "pnl_usd": round(pnl, 4),
         "realized_usd": round(S.realized, 4),
         "zero_streak": S.settle_zero_streak})
    reason = None
    if S.settle_zero_streak >= ECON_ZERO_STREAK:
        reason = f"{S.settle_zero_streak} consecutive zero-revenue settlements"
    elif S.realized <= ECON_HALT_LOSS:
        reason = f"realized {S.realized:.2f} <= {ECON_HALT_LOSS:.2f}"
    if reason and not S.halted:
        S.halted = True
        cancel_all("ECON_BREAKER")
        L.w({"ev": "ECON_HALT", "reason": reason,
             "realized_usd": round(S.realized, 4)})
        write_control_status()
    return True


async def settlements_task():
    while True:
        await asyncio.sleep(30)
        if MODE != "live":
            continue
        code, d = rest("GET", "/portfolio/settlements?limit=100")
        if code != 200:
            L.w({"ev": "SETTLE_FETCH_FAIL", "code": code})
            continue
        for s in (d.get("settlements") or []):
            apply_settlement(s)


# --------------------------------------------------------------- F2 recon
RECON_EVERY_S = 5.0
RECON_MAX_DIVERGENCE = 2      # contracts; > this in ANY market -> HALT
RECON_MAX_FAILS = 3           # consecutive fetch failures -> HALT (blind)


def recon_divergences(local, market_positions):
    """Compare local net_pos against exchange truth.  Exchange sign
    convention: position > 0 = long YES, < 0 = long NO.  Returns
    [(ticker, local_net, exchange_net)] beyond tolerance — including
    markets only one side knows about."""
    diffs, seen = [], set()
    for mp in (market_positions or []):
        mt = mp.get("ticker")
        seen.add(mt)
        exch = int(mp.get("position", 0) or 0)
        lp = local.get(mt, {"y": 0, "n": 0})
        lnet = int(lp["y"] - lp["n"])
        if abs(lnet - exch) > RECON_MAX_DIVERGENCE:
            diffs.append((mt, lnet, exch))
    for mt, lp in local.items():
        if mt in seen:
            continue
        lnet = int(lp["y"] - lp["n"])
        if abs(lnet) > RECON_MAX_DIVERGENCE:
            diffs.append((mt, lnet, 0))
    return diffs


def recon_check():
    """One reconciliation pass.  False = engine halted or blind pass."""
    code, d = rest("GET",
                   "/portfolio/positions?settlement_status=unsettled&limit=200")
    if code != 200:
        S.recon_fails += 1
        L.w({"ev": "RECON_FETCH_FAIL", "code": code, "fails": S.recon_fails})
        if S.recon_fails >= RECON_MAX_FAILS and not S.halted:
            S.halted = True
            cancel_all("RECON_BLIND")
            L.w({"ev": "RECON_HALT", "reason": "blind",
                 "fails": S.recon_fails})
            write_control_status()
        return False
    S.recon_fails = 0
    diffs = recon_divergences(S.net_pos, d.get("market_positions"))
    if not diffs:
        L.w({"ev": "RECON_OK",
             "markets": len(d.get("market_positions") or [])})
    if diffs:
        if not S.halted:
            S.halted = True
            cancel_all("RECON_DIVERGENCE")
            L.w({"ev": "RECON_HALT", "reason": "divergence",
                 "diffs": [{"mt": m, "local": l, "exchange": x}
                           for m, l, x in diffs]})
            write_control_status()
        return False
    return True


async def recon_task():
    while True:
        await asyncio.sleep(RECON_EVERY_S)
        if MODE != "live":
            continue
        recon_check()


async def ws_fills_task():
    """Primary fill feed: the private WS 'fill' channel (sub-second),
    with the REST poller kept as reconciliation backstop.  Same code
    path both modes (audit 10.6); shadow simply never receives fills."""
    while True:
        try:
            async with websockets.connect(
                    WS, additional_headers=websockets_headers(),
                    ping_interval=10) as ws:
                await ws.send(json.dumps({"id": 3, "cmd": "subscribe",
                                          "params": {"channels": ["fill"]}}))
                L.w({"ev": "WSFILL_SUB"})
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") != "fill":
                        continue
                    apply_fill(ws_fill_to_rest(m.get("msg") or {}))
        except Exception as e:
            L.w({"ev": "WSFILL_ERR", "err": repr(e)[:150]})
            await asyncio.sleep(2)


async def fills_task():
    while True:
        await asyncio.sleep(3)
        if MODE != "live": continue
        code, d = rest("GET", f"/portfolio/fills?min_ts={S.fills_cursor}&limit=100")
        for f in (d.get("fills") or []):
            apply_fill(f)
        if S.realized <= KILL_LOSS and not S.halted:
            S.halted=True; cancel_all("KILL_LOSS"); L.w({"ev":"KILL","realized":S.realized})

async def beat():
    while True:
        await asyncio.sleep(10)
        load_control(force=True)
        L.w({"ev":"HEALTH","mode":MODE,"halted":S.halted,
             "orders":len(S.orders),"open_cost":round(S.open_cost,2),
             "exposure":round(exposure(),2),
             "control_revision":CTRL["revision"],
             "max_open_cost":MAX_OPEN_COST,"clip":CLIP,"max_net":MAX_NET,
             "paused":CTRL["paused"],"kill":CTRL["kill"],
             "limit_breached":S.limit_breached,
             "requote_suppressed":S.requote_suppressed,
             "realized":round(S.realized,4),
             "recon_fails":S.recon_fails,
             "markets":len(S.meta),
             "rti_age":{s:round(time.time()-S.rti_t[s],1) for s in SERIES}})
        write_control_status()


async def control_task():
    """Independent 250ms control clock, not tied to market-data callbacks."""
    while True:
        load_control(force=True)
        if (CTRL["paused"] or CTRL["kill"] or S.halted) and S.orders:
            now = time.time()
            if now - S.last_control_cancel >= 1.0:
                cancel_all("CONTROL_WATCHER")
                S.last_control_cancel = now
                write_control_status()
        await asyncio.sleep(0.25)


async def main():
    load_control(force=True)
    if MODE == "live":
        ok, why = fills_visibility_selfcheck()
        L.w({"ev": "FILLS_SELFCHECK", "ok": ok, "why": why})
        if not ok:
            S.halted = True
            L.w({"ev": "HALT", "reason": f"fills selfcheck: {why}"})
    write_control_status()
    L.w({"ev":"START","mode":MODE,"series":list(SERIES),"clip":CLIP,
         "improve":IMP,"sentinel_c":SENTINEL_C,"max_open_cost":MAX_OPEN_COST,
         "hard_max_open_cost":HARD_MAX_OPEN_COST,
         "kill_loss":KILL_LOSS})
    await asyncio.gather(
        cf_task(), md_task(), fills_task(), ws_fills_task(), beat(),
        control_task(), recon_task(), settlements_task()
    )

if __name__ == "__main__":
    asyncio.run(main())
