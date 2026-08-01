# -*- coding: utf-8 -*-
"""Inventory-aware market-making grid replay (KXBTC15M, 12 days).

Adds the three missing pieces to the validated entry policy and grids
them together so the parameter set is self-consistent:

  1. SKEW (Avellaneda-Stoikov style): quote centre shifts against
     inventory.  shift_c = gamma * q * dPdS_scale, applied in either
     LINEAR cents or LOGIT space (A/B tested).
  2. HARD inventory cap: stop quoting the accumulating side at |q|>=cap.
  3. EXIT LEG: on fill, post passive opposite leg at entry+N with an age
     ladder (30/60/120s), forced taker flatten at tte<=180s (1.75c).

Entry policy identical to the validated band-aware baseline:
zones (>=600 all / 300-600 |mid-50|>10 / 120-300 tails / <120 none),
tail bands improve +0.1c and sit front-of-queue, mid band joins at the
back, cancel latency 60ms, fills only from real taker prints.

Baseline arm ("base") = no skew, no cap, no exit leg -> must reproduce
the known VALIDATE numbers (front +0.32c / 190 fills / 95 markets),
otherwise the whole run is discarded.
"""
import duckdb, glob, json, hashlib, os, math
import datetime as dt

TRAIN = [f"2026-07-{d}" for d in range(12, 20)]
VAL = [f"2026-07-{d}" for d in range(20, 24)]
CLIP_E4 = 5 * 10_000
LAT_US = 60_000
FLAT_US = 180_000_000
TAKER_C = 1.75
SNAP = "/home/ubuntu/h6b_inputs/catalog_normal/snapshot=20260724T213017Z"
OUT = "/home/ubuntu/h6b_inputs/grid"
os.makedirs(OUT, exist_ok=True)

# (name, gamma, space, cap, exitN)
ARMS = [
    ("base",            0.0, "lin",   999, None),
    ("skewLo",          0.5, "lin",   999, None),
    ("skewHi",          1.5, "lin",   999, None),
    ("skewLogit",       1.5, "logit", 999, None),
    ("cap6",            0.0, "lin",     6, None),
    ("skewHi_cap6",     1.5, "lin",     6, None),
    ("full_N2",         1.5, "lin",     6,  2.0),
    ("full_N1_logit",   1.5, "logit",   6,  1.0),
]

meta = {}
for t, mk in json.load(open(f"{SNAP}/shards/KXBTC15M.json"))["markets"].items():
    ct = mk.get("close_time"); res = str(mk.get("result") or "").lower()
    try:
        cu = int(dt.datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()*1e6)
    except Exception:
        continue
    if res in ("yes", "no"):
        meta[t] = (cu, res)

con = duckdb.connect(); con.execute("SET threads=4"); con.execute("SET memory_limit='10GB'")
LP = glob.glob("/home/ubuntu/hft-bot/work/warehouse/facts/orderbooks_full/category=Crypto/**/*.parquet", recursive=True)
TP = glob.glob("/home/ubuntu/hft-bot/work/warehouse/facts/trades/category=Crypto/**/*.csv.gz", recursive=True)
EPS = 1e-3

def best(bk):
    return max((p for p, v in bk.items() if (v or 0) > EPS), default=None)

def zone_ok(tte, mid_c):
    if tte < 120_000_000 or tte >= 1_800_000_000: return False
    if tte >= 600_000_000: return True
    if tte >= 300_000_000: return abs(mid_c - 50) > 10
    return mid_c < 20 or mid_c > 80

def logit(p):
    p = min(max(p, 0.001), 0.999)
    return math.log(p / (1 - p))

def inv_logit(x):
    return 1.0 / (1.0 + math.exp(-x))

def skew_shift(gamma, q, space, lvl_c):
    """Cents to shift our quote against inventory q (+q = long YES)."""
    if gamma == 0.0 or q == 0: return 0.0
    if space == "lin":
        return -gamma * q * 0.25            # 0.25c per contract per gamma
    p = lvl_c / 100.0
    z = logit(p) - gamma * q * 0.02         # logit-space shift
    return (inv_logit(z) * 100.0) - lvl_c

def run(dates, label):
    S = {a[0]: {"placed": 0, "fills": 0, "pnl": [], "mkts": set(),
                "closed": 0, "forced": 0, "carried": 0, "qpeak": 0}
         for a in ARMS}
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
        mt_cur=None; books=None; close_us=0; res=None; tr=[]; ti=0
        Q=P=NQ=None
        def newm(mt):
            nonlocal books, close_us, res, tr, ti, Q, P, NQ
            books={"y":{},"n":{}}; close_us,res = meta.get(mt,(0,None))
            tr = trades.get(mt,[]); ti=0
            Q={a[0]:{"y":None,"n":None} for a in ARMS}
            P={a[0]:[] for a in ARMS}          # open lots [{e,t,side,N}]
            NQ={a[0]:0 for a in ARMS}          # net position (contracts)
        def consume(upto):
            nonlocal ti
            while ti < len(tr) and tr[ti][0] <= upto:
                t_ts, t_px, t_qty, taker = tr[ti]; ti += 1
                for name, gamma, space, cap, exN in ARMS:
                    for sk, sell_taker in (("y","no"),("n","yes")):
                        qd = Q[name][sk]
                        if qd is not None:
                            if qd.get("cx") is not None and t_ts >= qd["cx"]:
                                Q[name][sk] = None
                            elif taker == sell_taker:
                                pxs = t_px if sk=="y" else 10000-t_px
                                if pxs <= qd["lvl"]:
                                    qd["ahead"] -= t_qty
                                    if qd["ahead"] < -CLIP_E4:
                                        S[name]["fills"] += 1
                                        S[name]["mkts"].add(mt_cur)
                                        e_c = qd["lvl"]/100.0
                                        Q[name][sk] = None
                                        NQ[name] += 5 if sk=="y" else -5
                                        S[name]["qpeak"] = max(S[name]["qpeak"], abs(NQ[name]))
                                        if exN is None:
                                            win = (res=="yes")==(sk=="y")
                                            S[name]["pnl"].append((100.0 if win else 0.0)-e_c)
                                            S[name]["carried"] += 1
                                            NQ[name] -= 5 if sk=="y" else -5
                                        else:
                                            P[name].append({"e":e_c,"t":t_ts,"sk":sk,"N":exN})
                                        continue
                        # exit legs
                        if exN is not None:
                            keep=[]
                            for lot in P[name]:
                                if lot["sk"]!=sk: keep.append(lot); continue
                                buy_taker = "yes" if sk=="y" else "no"
                                done=False
                                if taker == buy_taker:
                                    pxs=(t_px if sk=="y" else 10000-t_px)/100.0
                                    age=(t_ts-lot["t"])/1e6
                                    N=lot["N"]
                                    tgt=(lot["e"]+N if age<30 else
                                         lot["e"]+max(N-1,0) if age<60 else
                                         lot["e"] if age<120 else lot["e"]-1)
                                    if pxs >= tgt-1e-9:
                                        S[name]["pnl"].append(tgt-lot["e"])
                                        S[name]["closed"] += 1
                                        NQ[name] -= 5 if sk=="y" else -5
                                        done=True
                                if not done: keep.append(lot)
                            P[name]=keep
        while True:
            rows = cur.fetchmany(400_000)
            if not rows: break
            for mt, ts, mtype, side, px, dq, yl, nl in rows:
                ts=int(ts)
                if mt != mt_cur: mt_cur=mt; newm(mt)
                if not close_us: continue
                consume(ts)
                if mtype=="snapshot":
                    books={"y":{},"n":{}}
                    for arr,k in ((yl,"y"),(nl,"n")):
                        if arr:
                            try:
                                for p_,v_ in json.loads(arr):
                                    if p_ is not None: books[k][p_]=v_ or 0
                            except Exception: pass
                    continue
                if px is None or side not in ("yes","no"): continue
                bk=books["y" if side=="yes" else "n"]
                nv=bk.get(px,0)+(dq or 0)
                if abs(nv)<=EPS: bk.pop(px,None)
                else: bk[px]=nv
                tte=close_us-ts
                yb,nb=best(books["y"]),best(books["n"])
                if yb is None or nb is None: continue
                ya=10000-nb; mid=(yb+ya)/200.0
                ok=zone_ok(tte,mid)
                for name,gamma,space,cap,exN in ARMS:
                    q=NQ[name]
                    # forced flatten
                    if exN is not None and tte<=FLAT_US and P[name]:
                        for lot in P[name]:
                            exit_c=(yb if lot["sk"]=="y" else nb)/100.0
                            S[name]["pnl"].append(exit_c-lot["e"]-TAKER_C)
                            S[name]["forced"] += 1
                        NQ[name]=0; P[name]=[]
                    for sk,b_e4 in (("y",yb),("n",nb)):
                        # hard cap: stop adding to the accumulating side
                        if (sk=="y" and q>=cap) or (sk=="n" and -q>=cap):
                            qd=Q[name][sk]
                            if qd is not None and qd.get("cx") is None:
                                qd["cx"]=ts+LAT_US
                            continue
                        tail = b_e4<1000 or b_e4>9000
                        base_lvl = b_e4 + (10 if tail else 0)
                        sh = skew_shift(gamma, q if sk=="y" else -q, space,
                                        base_lvl/100.0)
                        lvl = int(round(base_lvl + sh*100))
                        lvl = max(10, min(lvl, 9990))
                        qd=Q[name][sk]
                        if qd is not None and qd.get("cx") is not None and ts>=qd["cx"]:
                            Q[name][sk]=None; qd=None
                        if not ok:
                            if qd is not None and qd.get("cx") is None: qd["cx"]=ts+LAT_US
                            continue
                        if qd is None:
                            Q[name][sk]={"lvl":lvl,"ahead":0 if tail else bk.get(b_e4,0),"cx":None}
                            S[name]["placed"] += 1
                        elif qd["lvl"]!=lvl and qd.get("cx") is None:
                            qd["cx"]=ts+LAT_US
        # settle leftovers
        for name,_g,_s,_c,exN in ARMS:
            if exN is None or not P: continue
            for lot in P[name]:
                win=(res=="yes")==(lot["sk"]=="y")
                S[name]["pnl"].append((100.0 if win else 0.0)-lot["e"])
                S[name]["carried"] += 1
            P[name]=[]
        print(f"{label} {date} done", flush=True)
    out={}
    for name,_g,_sp,_c,_e in ARMS:
        s=S[name]; pnl=s["pnl"]
        out[name]={"placed":s["placed"],"fills":s["fills"],
            "fill_rate":round(s["fills"]/s["placed"],4) if s["placed"] else None,
            "n_pnl":len(pnl),
            "mean_net_c":round(sum(pnl)/len(pnl),3) if pnl else None,
            "closed":s["closed"],"forced":s["forced"],"carried":s["carried"],
            "gambling_residue":round(s["carried"]/len(pnl),3) if pnl else None,
            "q_peak":s["qpeak"],"markets":len(s["mkts"])}
    return out

R={"schema":"grid-replay-v1","utc":dt.datetime.now(dt.timezone.utc).isoformat(),
   "arms":[a[0] for a in ARMS]}
print("=== TRAIN ===",flush=True); R["TRAIN"]=run(TRAIN,"TR")
print(json.dumps(R["TRAIN"],indent=1),flush=True)
print("=== VALIDATE ===",flush=True); R["VALIDATE"]=run(VAL,"VA")
print(json.dumps(R["VALIDATE"],indent=1),flush=True)
pay=json.dumps(R,sort_keys=True,default=float,indent=1).encode()
open(f"{OUT}/GRID_REPORT.json","wb").write(pay)
json.dump({"ok":True},open(f"{OUT}/DONE.json","w"))
print("sha256:",hashlib.sha256(pay).hexdigest())
