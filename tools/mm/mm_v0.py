#!/usr/bin/env python3
"""mm_v0 — KXBTC15M 自动挂单/撤单做市器(v0,单市场,1 张)。

规则(全部来自 E2 房间图 + T0 普查,大白话):
  1. 开窗就挂:双边 post-only 贴着买一/卖一排队,各 1 张。
  2. 波动分级:阈值动态(最近30分钟位移分布 p75/p95)——小动重定价、中动顺势
     侧后撤 1 分、大动或单边直冲(效率比>=0.8)才全撤冷却 20 秒。
  3. 最后 5 分钟必须离场:T-300s 全撤,本窗口不再挂,等下一个窗口。
  4. 库存闸:净持仓 >= +MAX_INV 停买边;<= -MAX_INV 停卖边。

模式:
  MM_MODE=shadow (默认) 只记日志,不发任何订单。
  MM_MODE=live           真实下单。必须由操作员亲手启动。

其他一切(重定价、状态机、日志收据)两种模式完全一致。
日志:work/mm/v0/mm_v0_<date>.ndjson —— 每个决策一行,可复盘。
"""
import json, time, base64, os, sys, http.client, uuid, datetime as dt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ---------- 配置 ----------
SERIES = os.environ.get("MM_SERIES", "KXBTC15M")
MODE = os.environ.get("MM_MODE", "shadow")          # shadow | live
SIZE = "1.00"                                        # 1 张
EXIT_BEFORE_S = 300                                  # T-5min 硬离场
ENTER_AFTER_S = 30                                   # 开窗 30 秒后才挂(等簿成型)
ADAPT_CENTS = 1.5    # 10 秒净位移超过此值 → 适应态:顺动量一侧后撤 1 分
STORM_CENTS = 4.0    # 净位移超过此值 → 真危险,全撤
EFF_DANGER = 0.8     # 效率比(净位移/路径长度)>= 0.8 = 单边直冲 = 有人知道什么
VOL_COOLDOWN_S = 20
BACKOFF = 0.01       # 适应态后撤幅度(美元)= 1 分
MAX_INV = 5                                          # 净持仓张数上限
REPRICE_TOL = 0.005                                  # 我方价偏离 touch 超过 0.5 分即重挂
LOOP_S = 1.0
MIN_PRICE, MAX_PRICE = 0.03, 0.97                    # 太贴边的盘不做
HOST = "external-api.kalshi.com"
EP = "/trade-api/v2/portfolio/events/orders"

KEY = serialization.load_pem_private_key(
    open(os.path.expanduser("~/.kalshi/private_key.pem"), "rb").read(), None)
KID = os.environ["KALSHI_API_KEY_ID"]

LOGDIR = os.path.expanduser("~/hft-bot/work/mm/v0")
os.makedirs(LOGDIR, exist_ok=True)

def log(**kw):
    kw["ts"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
    kw["mode"] = MODE
    line = json.dumps(kw, separators=(",", ":"))
    print(line, flush=True)
    with open(f"{LOGDIR}/mm_v0_{dt.date.today()}.ndjson", "a") as f:
        f.write(line + "\n")

def hdrs(method, path, body=False):
    ts = str(int(time.time() * 1000))
    sig = base64.b64encode(KEY.sign((ts + method + path).encode(),
          padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                      salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())).decode()
    h = {"KALSHI-ACCESS-KEY": KID, "KALSHI-ACCESS-SIGNATURE": sig,
         "KALSHI-ACCESS-TIMESTAMP": ts}
    if body: h["Content-Type"] = "application/json"
    return h

class Api:
    def __init__(self):
        self.c = None
        self.reconnect()
    def reconnect(self):
        self.c = http.client.HTTPSConnection(HOST, timeout=6)
        self.c.request("GET", "/trade-api/v2/exchange/status")
        self.c.getresponse().read()
    def req(self, method, path, body=None, sign_path=None):
        for attempt in (1, 2):
            try:
                self.c.request(method, path, body,
                               hdrs(method, sign_path or path.split("?")[0], body is not None))
                r = self.c.getresponse()
                return r.status, r.read()
            except Exception as e:
                if attempt == 2: raise
                self.reconnect()

api = Api()

def get_market():
    st, d = api.req("GET", f"/trade-api/v2/markets?series_ticker={SERIES}&status=open&limit=10")
    for m in json.loads(d).get("markets", []):
        b = float(m.get("yes_bid_dollars") or 0); a = float(m.get("yes_ask_dollars") or 0)
        if a > 0:
            close = dt.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            return m["ticker"], b, a, close
    return None, 0, 0, None

def place(ticker, side, price):
    """side: 'bid'=买YES排队, 'ask'=卖YES排队。price=YES 价(美元)。post-only。"""
    body = json.dumps({"ticker": ticker, "side": side, "count": SIZE,
                       "price": f"{price:.4f}", "time_in_force": "good_till_canceled",
                       "self_trade_prevention_type": "taker_at_cross", "post_only": True,
                       "client_order_id": str(uuid.uuid4())})
    if MODE != "live":
        log(act="would_place", ticker=ticker, side=side, price=price)
        return "shadow-" + uuid.uuid4().hex[:12]
    st, d = api.req("POST", EP, body)
    if st in (200, 201):
        oid = (json.loads(d).get("order") or {}).get("order_id")
        log(act="placed", ticker=ticker, side=side, price=price, oid=oid)
        return oid
    log(act="place_rejected", ticker=ticker, side=side, price=price, status=st,
        detail=str(d)[:120])
    return None

def cancel(oid, why):
    if oid is None: return
    if MODE != "live" or oid.startswith("shadow-"):
        log(act="would_cancel", oid=oid, why=why); return
    st, _ = api.req("DELETE", f"{EP}/{oid}", None, sign_path=f"{EP}/{oid}")
    log(act="canceled" if st == 200 else "cancel_failed", oid=oid, why=why, status=st)

def net_position(ticker):
    if MODE != "live": return 0
    p = "/trade-api/v2/portfolio/positions"
    st, d = api.req("GET", p + f"?ticker={ticker}", sign_path=p)
    for pos in json.loads(d).get("market_positions", []):
        if pos.get("ticker") == ticker:
            return int(float(pos.get("position", 0)))
    return 0

def main():
    log(act="start", series=SERIES, size=SIZE, exit_before_s=EXIT_BEFORE_S,
        adapt_floor=ADAPT_CENTS, storm_floor=STORM_CENTS, max_inv=MAX_INV,
        dynamic="p75/p95 of rolling 30min move distribution")
    bid_oid = ask_oid = None
    bid_px = ask_px = None
    cur_ticker = None
    mid_hist = []          # (t, mid) 最近 10 秒
    move_hist = []         # (t, mag) 最近 30 分钟的 10 秒净位移样本 → 动态阈值
    burst_until = 0.0
    while True:
        try:
            t0 = time.time()
            ticker, best_bid, best_ask, close = get_market()
            now = time.time()

            # 窗口翻转:上一窗的单作废(市场已关,交易所自动清,但本地状态要清)
            if ticker != cur_ticker:
                bid_oid = ask_oid = None; bid_px = ask_px = None
                mid_hist = []; cur_ticker = ticker
                log(act="new_window", ticker=ticker)
            if not ticker:
                time.sleep(LOOP_S); continue

            time_left = (close - dt.datetime.now(dt.timezone.utc)).total_seconds()
            window_age = 900 - time_left
            mid = (best_bid + best_ask) / 2 if best_bid > 0 else best_ask
            mid_hist.append((now, mid))
            mid_hist = [(t, m) for t, m in mid_hist if now - t <= 10.5]

            # --- 规则 3:最后 5 分钟,必须离场 ---
            if time_left <= EXIT_BEFORE_S:
                if bid_oid or ask_oid:
                    cancel(bid_oid, "T-5min_exit"); cancel(ask_oid, "T-5min_exit")
                    bid_oid = ask_oid = None
                    log(act="window_exit", ticker=ticker, time_left=int(time_left))
                time.sleep(LOOP_S); continue

            # --- 规则 2:波动分级应对(适应优先,真危险才撤) ---
            # 动了多大(净位移)+ 怎么动的(效率比:1=单边直冲危险,0=来回震荡肥肉)
            moved = (mid - mid_hist[0][1]) * 100          # 有方向,单位:分
            mag = abs(moved)
            path = sum(abs(mid_hist[i+1][1] - mid_hist[i][1])
                       for i in range(len(mid_hist) - 1)) * 100
            eff = (mag / path) if path > 1e-9 else 0.0
            # 动态阈值:市场自己最近 30 分钟的位移分布说了算
            move_hist.append((now, mag))
            move_hist = [(t, m) for t, m in move_hist if now - t <= 1800]
            if len(move_hist) >= 60:
                s = sorted(m for _, m in move_hist)
                adapt_th = max(ADAPT_CENTS * 0.5, s[int(len(s) * 0.75)])
                storm_th = max(STORM_CENTS * 0.5, s[int(len(s) * 0.95)], adapt_th * 1.5)
            else:
                adapt_th, storm_th = ADAPT_CENTS, STORM_CENTS
            lean_bid = lean_ask = False
            if mag >= storm_th or (mag >= adapt_th and eff >= EFF_DANGER):
                # 真危险:大幅或单边直冲 → 全撤 + 冷却
                burst_until = now + VOL_COOLDOWN_S
                if bid_oid or ask_oid:
                    cancel(bid_oid, "storm"); cancel(ask_oid, "storm")
                    bid_oid = ask_oid = None
                log(act="storm_retreat", moved=round(moved, 2), eff=round(eff, 2))
            elif mag >= adapt_th:
                # 适应:价在涨→卖侧容易被行家抬走,卖单后撤 1 分;跌→买侧后撤。
                # 逆动量一侧照常贴 touch 收散户流。
                if moved > 0: lean_ask = True
                else: lean_bid = True
                log(act="adapt", moved=round(moved, 2), eff=round(eff, 2),
                    lean=("ask" if lean_ask else "bid"))
            if now < burst_until:
                time.sleep(LOOP_S); continue

            # --- 挂单条件 ---
            tradeable = (window_age >= ENTER_AFTER_S and best_bid >= MIN_PRICE
                         and best_ask <= MAX_PRICE and best_ask > best_bid)
            if not tradeable:
                if bid_oid or ask_oid:
                    cancel(bid_oid, "not_tradeable"); cancel(ask_oid, "not_tradeable")
                    bid_oid = ask_oid = None
                time.sleep(LOOP_S); continue

            # --- 规则 4:库存闸 ---
            inv = net_position(ticker)
            want_bid = inv < MAX_INV
            want_ask = inv > -MAX_INV

            # --- 规则 1 + 重定价:贴 touch 排队;适应态时顺动量一侧后撤 ---
            tgt_bid = round(max(0.01, best_bid - (BACKOFF if lean_bid else 0)), 4)
            tgt_ask = round(min(0.99, best_ask + (BACKOFF if lean_ask else 0)), 4)
            if want_bid:
                if bid_oid is None or abs((bid_px or 0) - tgt_bid) > REPRICE_TOL:
                    cancel(bid_oid, "reprice")
                    bid_oid = place(ticker, "bid", tgt_bid); bid_px = tgt_bid
            elif bid_oid:
                cancel(bid_oid, "inv_cap"); bid_oid = None
            if want_ask:
                if ask_oid is None or abs((ask_px or 0) - tgt_ask) > REPRICE_TOL:
                    cancel(ask_oid, "reprice")
                    ask_oid = place(ticker, "ask", tgt_ask); ask_px = tgt_ask
            elif ask_oid:
                cancel(ask_oid, "inv_cap"); ask_oid = None

            log(act="tick", ticker=ticker, bid=best_bid, ask=best_ask,
                time_left=int(time_left), inv=inv,
                quoting=[bool(bid_oid), bool(ask_oid)])
            time.sleep(max(0.0, LOOP_S - (time.time() - t0)))
        except KeyboardInterrupt:
            cancel(bid_oid, "shutdown"); cancel(ask_oid, "shutdown")
            log(act="stop"); return
        except Exception as e:
            log(act="error", err=str(e)[:200])
            time.sleep(2)

if __name__ == "__main__":
    if MODE == "live" and os.environ.get("MM_LIVE_CONFIRM") != "YES":
        print("live 模式需要 MM_LIVE_CONFIRM=YES(操作员亲手)"); sys.exit(2)
    main()
