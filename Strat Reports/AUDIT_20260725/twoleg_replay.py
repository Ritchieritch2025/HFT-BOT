# -*- coding: utf-8 -*-
"""Two-leg closeout replay — clean rewrite (no string patching).

Question: after a passive fill, is it better to (a) HOLD to settlement
[current strategy, baseline +0.311c] or (b) post a passive opposite leg
and actively manage the inventory out?

Arms (identical entry logic, only the exit differs):
  hold        : fill -> hold to settlement (0/100)          [BASELINE]
  closeN1/2/3 : fill -> passive exit at entry+N cents, with an age
                ladder (>=30s: N-1, >=60s: 0, >=120s: -1) and a forced
                TAKER flatten at tte<=180s (pays 1.75c).

Entry (same as the validated band-aware policy):
  * zones: tte>=600 all; 300-600 |mid-50|>10; 120-300 tails; <120 none
  * price: tail bands (<10c / >90c) improve +0.1c; mid band join best
  * queue: front-of-queue in tail bands (ahead=0), tail-of-queue in mid
  * fills only from real taker prints through our level
  * cancel latency 60ms; one fill per side per market

Split: TRAIN 07-12..19, VALIDATE 07-20..23. Cluster = market (15m window).
Reported per arm: mean net c/contract, fill rate, closed_passive,
forced_flat, carried_to_settle (= gambling residue), n clusters.
"""
import duckdb, glob, json, hashlib, os
import datetime as dt

TRAIN = [f"2026-07-{d}" for d in range(12, 20)]
VAL = [f"2026-07-{d}" for d in range(20, 24)]
CLIP_E4 = 5 * 10_000            # 5 contracts in e4 units
CANCEL_LAT_US = 60_000
JOIN_LO, JOIN_HI = 120_000_000, 1_800_000_000
FLATTEN_US = 180_000_000
TAKER_FEE_C = 1.75
ARMS = [("hold", None), ("closeN1", 1.0), ("closeN2", 2.0), ("closeN3", 3.0)]
SNAP = "/home/ubuntu/h6b_inputs/catalog_normal/snapshot=20260724T213017Z"
OUT = "/home/ubuntu/h6b_inputs/twoleg"
os.makedirs(OUT, exist_ok=True)

meta = {}
for t, mk in json.load(open(f"{SNAP}/shards/KXBTC15M.json"))["markets"].items():
    ct = mk.get("close_time"); res = str(mk.get("result") or "").lower()
    try:
        cu = int(dt.datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()*1e6)
    except Exception:
        continue
    if res in ("yes", "no"):
        meta[t] = (cu, res)
print("settled markets:", len(meta), flush=True)

con = duckdb.connect(); con.execute("SET threads=4"); con.execute("SET memory_limit='10GB'")
LP = glob.glob("/home/ubuntu/hft-bot/work/warehouse/facts/orderbooks_full/category=Crypto/**/*.parquet", recursive=True)
TP = glob.glob("/home/ubuntu/hft-bot/work/warehouse/facts/trades/category=Crypto/**/*.csv.gz", recursive=True)
EPS = 1e-3

def best(bk):
    return max((p for p, v in bk.items() if (v or 0) > EPS), default=None)

def zone_ok(tte_us, mid_c):
    if tte_us < 120_000_000 or tte_us >= JOIN_HI: return False
    if tte_us >= 600_000_000: return True
    if tte_us >= 300_000_000: return abs(mid_c - 50) > 10
    return mid_c < 20 or mid_c > 80

def quote_level(b_e4):
    """(level_e4, queue_ahead_is_zero) — tail bands improve, mid joins."""
    tail = b_e4 < 1000 or b_e4 > 9000
    return (b_e4 + 10, True) if tail else (b_e4, False)

def exit_target(entry_c, N, age_s):
    if age_s < 30: return entry_c + N
    if age_s < 60: return entry_c + max(N - 1.0, 0.0)
    if age_s < 120: return entry_c
    return entry_c - 1.0

def run(dates, label):
    st = {a: {"placed": 0, "fills": 0, "pnl": [], "clusters": set(),
              "closed": 0, "forced": 0, "carried": 0} for a, _ in ARMS}
    for date in dates:
        lf = [p for p in LP if f"date={date}" in p]
        tf = [p for p in TP if f"date={date}" in p]
        if not lf or not tf: continue
        trades = {}
        for mt, ts, px, qty, side in con.execute("""
            SELECT market_ticker, ts_utc, yes_price_e4, count_e4, taker_side
            FROM read_csv(?, header=true, union_by_name=true,
                          types={'taker_side':'VARCHAR'})
            WHERE series_ticker='KXBTC15M' ORDER BY market_ticker, ts_utc
        """, [tf]).fetchall():
            trades.setdefault(mt, []).append((int(ts), px, qty or 0, side))
        cur = con.execute("""
            SELECT market_ticker, ts_utc, msg_type, side, price_e4, delta_e4,
                   CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR)
            FROM read_parquet(?, union_by_name=true)
            ORDER BY market_ticker, ts_utc, ws_seq""", [lf])
        mt_cur = None; books = None; close_us = 0; res = None
        tr = []; ti = 0
        # per arm per side: quote dict / position dict / done flag
        Q = P = D = None
        def new_market(mt):
            nonlocal books, close_us, res, tr, ti, Q, P, D
            books = {"y": {}, "n": {}}
            close_us, res = meta.get(mt, (0, None))
            tr = trades.get(mt, []); ti = 0
            Q = {a: {"y": None, "n": None} for a, _ in ARMS}
            P = {a: {"y": None, "n": None} for a, _ in ARMS}
            D = {a: {"y": False, "n": False} for a, _ in ARMS}

        def consume_trades(upto_ts):
            nonlocal ti
            while ti < len(tr) and tr[ti][0] <= upto_ts:
                t_ts, t_px, t_qty, taker = tr[ti]; ti += 1
                for arm, N in ARMS:
                    for sk, sell_taker in (("y", "no"), ("n", "yes")):
                        # --- entry leg ---
                        qd = Q[arm][sk]
                        if qd is not None and not D[arm][sk]:
                            if qd.get("cx") is not None and t_ts >= qd["cx"]:
                                Q[arm][sk] = None
                            elif taker == sell_taker:
                                pxs = t_px if sk == "y" else 10000 - t_px
                                if pxs <= qd["lvl"]:
                                    qd["ahead"] -= t_qty
                                    if qd["ahead"] < -CLIP_E4:
                                        st[arm]["fills"] += 1
                                        st[arm]["clusters"].add(mt_cur)
                                        e_c = qd["lvl"] / 100.0
                                        Q[arm][sk] = None
                                        if N is None:
                                            D[arm][sk] = True
                                            win = (res == "yes") == (sk == "y")
                                            st[arm]["pnl"].append(
                                                (100.0 if win else 0.0) - e_c)
                                            st[arm]["carried"] += 1
                                        else:
                                            P[arm][sk] = {"e": e_c, "t": t_ts,
                                                          "N": N}
                                        continue
                        # --- exit leg (passive offer lifted by a buyer) ---
                        po = P[arm][sk]
                        if po is not None:
                            buy_taker = "yes" if sk == "y" else "no"
                            if taker == buy_taker:
                                pxs = (t_px if sk == "y" else 10000 - t_px) / 100.0
                                tgt = exit_target(po["e"], po["N"],
                                                  (t_ts - po["t"]) / 1e6)
                                if pxs >= tgt - 1e-9:
                                    st[arm]["pnl"].append(tgt - po["e"])
                                    st[arm]["closed"] += 1
                                    P[arm][sk] = None; D[arm][sk] = True

        while True:
            rows = cur.fetchmany(400_000)
            if not rows: break
            for mt, ts, mtype, side, px, dq, yl, nl in rows:
                ts = int(ts)
                if mt != mt_cur:
                    mt_cur = mt; new_market(mt)
                if not close_us: continue
                consume_trades(ts)
                if mtype == "snapshot":
                    books = {"y": {}, "n": {}}
                    for arr, k in ((yl, "y"), (nl, "n")):
                        if arr:
                            try:
                                for p_, v_ in json.loads(arr):
                                    if p_ is not None: books[k][p_] = v_ or 0
                            except Exception: pass
                    continue
                if px is None or side not in ("yes", "no"): continue
                bk = books["y" if side == "yes" else "n"]
                nv = bk.get(px, 0) + (dq or 0)
                if abs(nv) <= EPS: bk.pop(px, None)
                else: bk[px] = nv
                tte = close_us - ts
                yb, nb = best(books["y"]), best(books["n"])
                if yb is None or nb is None: continue
                ya = 10000 - nb
                mid_c = (yb + ya) / 200.0
                ok = zone_ok(tte, mid_c)
                for arm, N in ARMS:
                    for sk, b_e4 in (("y", yb), ("n", nb)):
                        if D[arm][sk]: continue
                        po = P[arm][sk]
                        # forced taker flatten near expiry
                        if po is not None and N is not None and tte <= FLATTEN_US:
                            opp = ya if sk == "y" else 10000 - yb
                            exit_c = (yb if sk == "y" else nb) / 100.0
                            st[arm]["pnl"].append(exit_c - po["e"] - TAKER_FEE_C)
                            st[arm]["forced"] += 1
                            P[arm][sk] = None; D[arm][sk] = True
                            continue
                        if po is not None: continue    # busy holding
                        lvl, front = quote_level(b_e4)
                        qd = Q[arm][sk]
                        if qd is not None and qd.get("cx") is not None and ts >= qd["cx"]:
                            Q[arm][sk] = None; qd = None
                        if not ok or tte < 120_000_000:
                            if qd is not None and qd.get("cx") is None:
                                qd["cx"] = ts + CANCEL_LAT_US
                            continue
                        if qd is None:
                            Q[arm][sk] = {"lvl": lvl,
                                          "ahead": 0 if front else bk.get(b_e4, 0),
                                          "cx": None}
                            st[arm]["placed"] += 1
                        elif qd["lvl"] != lvl and qd.get("cx") is None:
                            qd["cx"] = ts + CANCEL_LAT_US
            # end rows
        # settle leftovers held by close (close arms only)
        for arm, N in ARMS:
            if N is None: continue
            for sk in ("y", "n"):
                po = P[arm][sk] if P else None
                if po is not None:
                    win = (res == "yes") == (sk == "y")
                    st[arm]["pnl"].append((100.0 if win else 0.0) - po["e"])
                    st[arm]["carried"] += 1
        print(f"{label} {date} done", flush=True)
    out = {}
    for arm, _ in ARMS:
        s = st[arm]; pnl = s["pnl"]
        out[arm] = {
            "quotes_placed": s["placed"], "fills": s["fills"],
            "fill_rate": round(s["fills"]/s["placed"], 4) if s["placed"] else None,
            "n_closed_pnl": len(pnl),
            "mean_net_c": round(sum(pnl)/len(pnl), 3) if pnl else None,
            "closed_passive": s["closed"], "forced_flat": s["forced"],
            "carried_to_settle": s["carried"],
            "gambling_residue": (round(s["carried"]/len(pnl), 3) if pnl else None),
            "clusters": len(s["clusters"]),
        }
    return out

res_all = {"schema_version": "twoleg-replay-v1",
           "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
           "arms": [a for a, _ in ARMS]}
print("=== TRAIN ===", flush=True)
res_all["TRAIN"] = run(TRAIN, "TRAIN")
print(json.dumps(res_all["TRAIN"], indent=1), flush=True)
print("=== VALIDATE ===", flush=True)
res_all["VALIDATE"] = run(VAL, "VAL")
print(json.dumps(res_all["VALIDATE"], indent=1), flush=True)
payload = json.dumps(res_all, sort_keys=True, default=float, indent=1).encode()
open(f"{OUT}/TWOLEG_REPORT.json", "wb").write(payload)
json.dump({"ok": True}, open(f"{OUT}/DONE.json", "w"))
print("sha256:", hashlib.sha256(payload).hexdigest())
