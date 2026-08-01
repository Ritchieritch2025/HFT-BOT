# -*- coding: utf-8 -*-
"""E4-TWOLEG — closed-loop maker replay, KXBTC15M. CLEAN REWRITE (no patches).

Question (operator, 2026-07-25): holding to settlement is fast gambling, not
market making. Does a passive opposite-leg closeout keep the edge while
killing the settlement residue?

Arms (per entry mode tail/front, cancel latency 60ms):
  hold    — baseline: inventory marked to real settlement. MUST reproduce
            e4_front exactly (see BASELINE_EXPECT); otherwise VOID.
  close1/2/3 — on entry fill at P, post passive exit ask at P + 1/2/3c;
            inventory-age ladder at 30/60/120s each drops the ask 1c
            (cancel + rejoin tail of the new level, cancel latency applies);
            at T-3m cancel and force-flatten as taker into displayed bids
            (walk the book, official 7% quadratic taker fee, order total
            rounded up to the cent); no bid displayed -> carried to
            settlement.

Exit-leg state machine (one per filled position per arm):

    [entry fill]
        v
    RESTING --(ladder age 30/60/120s: target ask changed)--> CANCELING
      |  ^                                                      |
      |  +---(cancel effective at cx, rejoin tail new level)----+
      |
      |--(taker trade-through at/over ask, tape only)--> CLOSED_PASSIVE
      |--(tte <= 3m, displayed bids exist)-------------> FORCED_FLAT
      |--(tte <= 3m, empty bid book)-------------------> CARRIED
      +--(data ends while open)------------------------> CARRIED

Fill realism carried over unchanged from the audited 10-rule engine
(e4_front.py, agents/audit/ACCEPTANCE_影子做市引擎_成交真实性.md):
  * entry: join tail (queue_ahead = displayed) or front (+0.1c, ahead 0);
  * fills ONLY on real taker trade-through: cumulative taker volume at or
    through the level must strictly exceed queue_ahead + clip;
  * no lookahead; cancels effective after CANCEL_LAT; maker fee 0
    (official 2026-07-07 schedule, series not in non-standard list).
Exit legs obey the same trade-through rule on the opposite book side
(yes-ask at A == no-bid at 100-A). Pessimistic simplifications, all in the
losing direction for close arms: exit quote activates only fill_ts+60ms
after the entry print; ladder/flatten actions quantize to the next book
event; an ask that would cross still only fills on trade-through; exit
fills stop at T-3m even if the cancel hasn't technically landed.

Split: TRAIN 07-12..19 (8d) / VALIDATE 07-20..23 (4d), policy frozen, no
fitted params. Cluster = market (one 15m window). Judged on BOTH
mean net c/contract AND contract-weighted fraction carried into settlement
(gambling residue; hold arm = 1.0 by construction).

Known-answer: graveyard (join tte<5m, no withdrawal) must print deep red
AND match e4_front's exact numbers before anything else runs.
"""
import glob
import hashlib
import json
import math
import os
import random
import datetime as dt
from collections import Counter, defaultdict

# ---------------------------------------------------------------- constants
DATES_TRAIN = [f"2026-07-{d}" for d in range(12, 20)]
DATES_VAL = [f"2026-07-{d}" for d in range(20, 24)]
CLIP = 5 * 10_000            # e4 qty units (5 contracts)
CANCEL_LATS = (60_000,)      # us
MODES = (("tail", 0), ("front", 10))   # (queue posture, improvement e4)
CLOSE_ARMS = {"close1": 100, "close2": 200, "close3": 300}  # ask offset e4
LADDER_US = (30_000_000, 60_000_000, 120_000_000)
FLATTEN_TTE_US = 180_000_000           # T-3m forced flatten
ENTRY_JOIN_LO_US = 600_000_000         # 10m
ENTRY_JOIN_HI_US = 1_800_000_000       # 30m
ENTRY_WITHDRAW_US = 300_000_000        # 5m hard withdraw of entry quotes
TAKER_FEE_RATE = 0.07
BOOT_N, BOOT_SEED = 1000, 1337
TERMINAL = ("CLOSED_PASSIVE", "FORCED_FLAT", "CARRIED")

SNAP_DIR = os.environ.get(
    "E4_SNAP_DIR",
    "/home/ubuntu/h6b_inputs/catalog_normal/snapshot=20260724T213017Z")
L2_GLOB = os.environ.get(
    "E4_L2_GLOB",
    "/home/ubuntu/hft-bot/work/warehouse/facts/orderbooks_full/category=Crypto/**/*.parquet")
TR_GLOB = os.environ.get(
    "E4_TR_GLOB",
    "/home/ubuntu/hft-bot/work/warehouse/facts/trades/category=Crypto/**/*.csv.gz")
OUT = os.environ.get("E4_OUT", "/home/ubuntu/h6b_inputs/e4_twoleg_closeout")

# TRUE baseline = the ORIGINAL e4_front.py run (h6b_inputs/e4_front.log,
# report sha 753adc5d9f6f...). NOT e4_front/E4_REPORT.json — that file was
# overwritten at 20:39Z by the corrupted string-patched e4_twoleg.py, whose
# entry engine drifted (front placed 653 vs original 274; mean 0.311 vs
# 0.321) while its exit arms silently tested nothing. The entry engine here
# must reproduce every one of these numbers or the whole run is VOID.
BASELINE_EXPECT = {
    "known_answer_graveyard_VAL": {
        "front_cancel_60ms": {"quotes_placed": 224, "fills": 192,
                              "fill_rate": 0.8571, "markets_quoted": 96,
                              "clusters_filled": 96, "mean_settle_pnl_c": -3.198},
        "tail_cancel_60ms": {"quotes_placed": 581, "fills": 192,
                             "fill_rate": 0.3305, "markets_quoted": 96,
                             "clusters_filled": 96, "mean_settle_pnl_c": -3.654},
    },
    "main_TRAIN": {
        "front_cancel_60ms": {"quotes_placed": 476, "fills": 360,
                              "fill_rate": 0.7563, "markets_quoted": 180,
                              "clusters_filled": 180, "mean_settle_pnl_c": 0.34},
        "tail_cancel_60ms": {"quotes_placed": 1107, "fills": 360,
                             "fill_rate": 0.3252, "markets_quoted": 180,
                             "clusters_filled": 180, "mean_settle_pnl_c": 0.081},
    },
    "main_VALIDATE": {
        "front_cancel_60ms": {"quotes_placed": 274, "fills": 190,
                              "fill_rate": 0.6934, "markets_quoted": 95,
                              "clusters_filled": 95, "mean_settle_pnl_c": 0.321},
        "tail_cancel_60ms": {"quotes_placed": 722, "fills": 190,
                             "fill_rate": 0.2632, "markets_quoted": 95,
                             "clusters_filled": 95, "mean_settle_pnl_c": 0.0},
    },
}


# ------------------------------------------------- exit-leg state machine
# A position is a plain dict; all prices in e4 "side units" of the position
# (y: yes cents*100, n: no cents*100). The exit ask A rests on the OPPOSITE
# bid book at level 10000 - A (yes-ask == no-bid identity).

def clamp_ask(a_e4):
    return max(100, min(9900, a_e4))


def target_ask_e4(entry_e4, offset_e4, age_us):
    """Ladder: start at entry+offset, drop 1c at each of 30/60/120s."""
    steps = sum(1 for t in LADDER_US if age_us >= t)
    return clamp_ask(entry_e4 + offset_e4 - 100 * steps)


def pos_new(side, mt, entry_e4, fill_ts, offset_e4, opp_book, lat_us):
    a = clamp_ask(entry_e4 + offset_e4)
    return {
        "side": side, "mt": mt, "entry": entry_e4, "fill_ts": fill_ts,
        "offset": offset_e4, "state": "RESTING",
        "A": a, "lvl_opp": 10000 - a,
        "ahead": opp_book.get(10000 - a, 0) or 0,
        "active_from": fill_ts + lat_us, "cx": None,
        "pnl_c": None, "exit_kind": None, "exit_ts": None,
        "closed_e4": 0, "forced_e4": 0, "carried_e4": 0,
    }


def exit_consume_trade(pos, t_ts, t_px_e4, t_qty, taker, close_us):
    """Tape drives passive exit fills — same trade-through rule as entries."""
    if pos["state"] not in ("RESTING", "CANCELING"):
        return
    if t_ts < pos["active_from"]:
        return
    if pos["state"] == "CANCELING" and t_ts >= pos["cx"]:
        return                       # old quote dead, new one not yet placed
    if t_ts >= close_us - FLATTEN_TTE_US:
        return                       # policy: no exit fills past T-3m
    want_taker = "yes" if pos["side"] == "y" else "no"
    if taker != want_taker:
        return
    opp_px = (10000 - t_px_e4) if pos["side"] == "y" else t_px_e4
    if opp_px > pos["lvl_opp"]:
        return
    pos["ahead"] -= t_qty
    if pos["ahead"] < -CLIP:
        pos["state"] = "CLOSED_PASSIVE"
        pos["exit_kind"] = "passive"
        pos["pnl_c"] = (pos["A"] - pos["entry"]) / 100.0
        pos["closed_e4"] = CLIP
        pos["exit_ts"] = t_ts


def walk_book_sell(book, qty_e4):
    """Consume displayed bids best-first. Returns ([(px, qty)], filled_e4)."""
    fills, rem = [], qty_e4
    for p in sorted((p for p, v in book.items() if (v or 0) > 0), reverse=True):
        take = min(rem, book[p])
        fills.append((p, take))
        rem -= take
        if rem <= 0:
            break
    return fills, qty_e4 - rem


def taker_fee_c_per_order(fills):
    """Official quadratic taker fee, order total rounded UP to the cent."""
    dollars = sum(TAKER_FEE_RATE * (q / 10000.0) * (p / 10000.0) * (1 - p / 10000.0)
                  for p, q in fills)
    return float(math.ceil(dollars * 100))


def _settle_e4(pos, res):
    win = (res == "yes") == (pos["side"] == "y")
    return 10000 if win else 0


def flatten_taker(pos, ts, own_book, res):
    """T-3m: cancel exit quote, sell into displayed bids; remainder carried."""
    fills, filled = walk_book_sell(own_book, CLIP)
    rem = CLIP - filled
    total_c = sum((p - pos["entry"]) / 100.0 * (q / 10000.0) for p, q in fills)
    if filled:
        total_c -= taker_fee_c_per_order(fills)
    if rem:
        total_c += (_settle_e4(pos, res) - pos["entry"]) / 100.0 * (rem / 10000.0)
    pos["pnl_c"] = total_c / (CLIP / 10000.0)
    pos["forced_e4"], pos["carried_e4"] = filled, rem
    pos["state"] = "FORCED_FLAT" if filled else "CARRIED"
    pos["exit_kind"] = "taker_flatten" if filled else "carried_no_bid"
    pos["exit_ts"] = ts


def exit_on_event(pos, ts, close_us, opp_book, own_book, lat_us, res):
    """Book-event driver: forced flatten, cancel completion, ladder reprice."""
    if pos["state"] not in ("RESTING", "CANCELING"):
        return
    if close_us - ts <= FLATTEN_TTE_US:
        flatten_taker(pos, ts, own_book, res)
        return
    age = ts - pos["fill_ts"]
    if pos["state"] == "CANCELING":
        if ts >= pos["cx"]:
            a = target_ask_e4(pos["entry"], pos["offset"], age)
            pos["A"], pos["lvl_opp"] = a, 10000 - a
            pos["ahead"] = opp_book.get(10000 - a, 0) or 0
            pos["active_from"], pos["cx"] = ts, None
            pos["state"] = "RESTING"
        return
    if target_ask_e4(pos["entry"], pos["offset"], age) != pos["A"]:
        pos["cx"] = ts + lat_us
        pos["state"] = "CANCELING"


def finalize_pos(pos, res):
    """Data stream ended while still open -> carried to settlement."""
    if pos["state"] in ("RESTING", "CANCELING"):
        pos["pnl_c"] = (_settle_e4(pos, res) - pos["entry"]) / 100.0
        pos["carried_e4"] = CLIP
        pos["state"] = "CARRIED"
        pos["exit_kind"] = "carried_data_end"


def cluster_boot_ci(pairs, n=BOOT_N, seed=BOOT_SEED):
    """95% CI of the mean, bootstrap clustered by market."""
    by = defaultdict(list)
    for mt, p in pairs:
        by[mt].append(p)
    mkts = sorted(by)
    if not mkts:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        s = [p for m in (rng.choice(mkts) for _ in mkts) for p in by[m]]
        means.append(sum(s) / len(s))
    means.sort()
    return [round(means[int(0.025 * n)], 3), round(means[int(0.975 * n)], 3)]


# ------------------------------------------------------------ replay engine
def run_policy(con, lpats, tpats, meta, dates, join_lo_us, join_hi_us,
               withdraw_us, close_arms):
    """Entry engine is e4_front.py verbatim; exit legs are shadow observers.

    Returns (entry_stats_block, arms_block)."""
    stats = {(L, M[0]): {"placed": 0, "filled": 0, "pnl": [], "markets": set(),
                         "clusters_filled": set()}
             for L in CANCEL_LATS for M in MODES}
    acc = {(L, M[0], arm): {"pairs": [], "closed_e4": 0, "forced_e4": 0,
                            "carried_e4": 0, "hold_s": [], "kinds": Counter()}
           for L in CANCEL_LATS for M in MODES
           for arm in ("hold",) + tuple(close_arms)}

    def harvest(st):
        """Market stream ended: freeze entries, drain tape for exits, book."""
        if st is None:
            return
        for V in st["q"]:
            st["q"][V] = {"y": None, "n": None}
        process_trades_until(st, 1 << 62)
        res = st["res"]
        for (L, MNAME), arms in st["pos"].items():
            for arm, plist in arms.items():
                a = acc[(L, MNAME, arm)]
                for pos in plist:
                    finalize_pos(pos, res)
                    assert pos["state"] in TERMINAL
                    assert pos["closed_e4"] + pos["forced_e4"] + pos["carried_e4"] == CLIP
                    a["pairs"].append((pos["mt"], pos["pnl_c"]))
                    a["closed_e4"] += pos["closed_e4"]
                    a["forced_e4"] += pos["forced_e4"]
                    a["carried_e4"] += pos["carried_e4"]
                    a["kinds"][pos["exit_kind"]] += 1
                    if pos["exit_ts"] is not None:
                        a["hold_s"].append((pos["exit_ts"] - pos["fill_ts"]) / 1e6)

    for date in dates:
        lf = [p for p in lpats if f"date={date}" in p]
        tf = [p for p in tpats if f"date={date}" in p]
        if not lf:
            continue
        trades = defaultdict(list)   # mt -> [(ts, px_e4, qty_e4, taker)]
        for mt, ts, px, qty, side in con.execute("""
            SELECT market_ticker, ts_utc, yes_price_e4, count_e4, taker_side
            FROM read_csv(?, header=true, union_by_name=true,
                          types={'taker_side':'VARCHAR'})
            WHERE series_ticker='KXBTC15M'
            ORDER BY market_ticker, ts_utc""", [tf]).fetchall():
            trades[mt].append((int(ts), px, qty or 0, side))
        cur = con.execute("""
            SELECT market_ticker, ts_utc, msg_type, side, price_e4, delta_e4,
                   CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR)
            FROM read_parquet(?, union_by_name=true)
            ORDER BY market_ticker, ts_utc, ws_seq""", [lf])

        state = None

        def new_state(mt):
            close_us, res = meta.get(mt, (0, None))
            return {
                "mt": mt, "close": close_us, "res": res,
                "by": {}, "bn": {},
                "tr": trades.get(mt, []), "ti": 0,
                "q": {(L, M[0]): {"y": None, "n": None}
                      for L in CANCEL_LATS for M in MODES},
                "done": {(L, M[0]): {"y": False, "n": False}
                         for L in CANCEL_LATS for M in MODES},
                "pos": {(L, M[0]): {arm: [] for arm in close_arms}
                        for L in CANCEL_LATS for M in MODES},
            }

        def best(bk):
            return max((p for p, v in bk.items() if (v or 0) > 0), default=None)

        def process_trades_until(st, ts):
            """Advance tape; credit queue consumption + fills (entry & exit)."""
            while st["ti"] < len(st["tr"]) and st["tr"][st["ti"]][0] <= ts:
                t_ts, t_px, t_qty, taker = st["tr"][st["ti"]]
                st["ti"] += 1
                for L in CANCEL_LATS:
                  for MNAME, _imp in MODES:
                    V = (L, MNAME)
                    for side_key, sell_taker in (("y", "no"), ("n", "yes")):
                        qd = st["q"][V][side_key]
                        if qd is None or st["done"][V][side_key]:
                            continue
                        if qd.get("cx") is not None and t_ts >= qd["cx"]:
                            st["q"][V][side_key] = None
                            continue
                        if taker != sell_taker:
                            continue
                        px_side = t_px if side_key == "y" else 10000 - t_px
                        if px_side <= qd["lvl"]:
                            qd["ahead"] -= t_qty
                            if qd["ahead"] < -CLIP:
                                stats[V]["filled"] += 1
                                st["done"][V][side_key] = True
                                entry_c = qd["lvl"] / 100.0
                                win = (st["res"] == "yes") == (side_key == "y")
                                pnl = (100.0 if win else 0.0) - entry_c
                                stats[V]["pnl"].append(pnl)
                                stats[V]["clusters_filled"].add(st["mt"])
                                st["q"][V][side_key] = None
                                # ---- spawn shadow positions (new layer) ----
                                ah = acc[(L, MNAME, "hold")]
                                ah["pairs"].append((st["mt"], pnl))
                                ah["carried_e4"] += CLIP
                                ah["kinds"]["hold_to_settle"] += 1
                                opp = st["bn"] if side_key == "y" else st["by"]
                                for arm, off in close_arms.items():
                                    st["pos"][V][arm].append(pos_new(
                                        side_key, st["mt"], qd["lvl"], t_ts,
                                        off, opp, L))
                    for arm in close_arms:
                        for pos in st["pos"][V][arm]:
                            exit_consume_trade(pos, t_ts, t_px, t_qty, taker,
                                               st["close"])

        while True:
            rows = cur.fetchmany(400_000)
            if not rows:
                break
            for mt, ts, mtype, side, px, dq, yl, nl in rows:
                ts = int(ts)
                if state is None or state["mt"] != mt:
                    harvest(state)
                    state = new_state(mt)
                st = state
                if not st["close"]:
                    continue
                process_trades_until(st, ts)
                if mtype == "snapshot":
                    st["by"], st["bn"] = {}, {}
                    for arr, bk in ((yl, st["by"]), (nl, st["bn"])):
                        if arr:
                            try:
                                for p_, v_ in json.loads(arr):
                                    if p_ is not None:
                                        bk[p_] = v_ or 0
                            except Exception:
                                pass
                else:
                    if px is None:
                        continue
                    bk = st["by"] if side == "yes" else st["bn"]
                    bk[px] = bk.get(px, 0) + (dq or 0)
                tte = st["close"] - ts
                for L in CANCEL_LATS:
                  for MNAME, IMP in MODES:
                    V = (L, MNAME)
                    for side_key, bk in (("y", st["by"]), ("n", st["bn"])):
                        if st["done"][V][side_key]:
                            continue
                        qd = st["q"][V][side_key]
                        b = best(bk)
                        if qd is not None and qd.get("cx") is not None and ts >= qd["cx"]:
                            st["q"][V][side_key] = None
                            qd = None
                        if tte <= withdraw_us:
                            if qd is not None and qd.get("cx") is None:
                                qd["cx"] = ts + L
                            continue
                        if not (join_lo_us <= tte < join_hi_us):
                            continue
                        if qd is None:
                            if b is not None:
                                lvl = b + IMP           # front: improve 0.1c
                                st["q"][V][side_key] = {
                                    "lvl": lvl,
                                    "ahead": 0 if MNAME == "front" else bk.get(b, 0),
                                    "cx": None}
                                stats[V]["placed"] += 1
                                stats[V]["markets"].add(mt)
                        else:
                            base_lvl = qd["lvl"] - IMP
                            if b != base_lvl and qd.get("cx") is None:
                                qd["cx"] = ts + L
                    for arm in close_arms:
                        for pos in st["pos"][V][arm]:
                            opp = st["bn"] if pos["side"] == "y" else st["by"]
                            own = st["by"] if pos["side"] == "y" else st["bn"]
                            exit_on_event(pos, ts, st["close"], opp, own, L,
                                          st["res"])
        harvest(state)
        state = None

    entry_block, arms_block = {}, {}
    for (L, MNAME), s in stats.items():
        key = f"{MNAME}_cancel_{L // 1000}ms"
        pnl = s["pnl"]
        entry_block[key] = {
            "quotes_placed": s["placed"],
            "fills": s["filled"],
            "fill_rate": round(s["filled"] / s["placed"], 4) if s["placed"] else None,
            "markets_quoted": len(s["markets"]),
            "clusters_filled": len(s["clusters_filled"]),
            "mean_settle_pnl_c": round(sum(pnl) / len(pnl), 3) if pnl else None,
            "gray": len(s["clusters_filled"]) < 30,
        }
        ab = arms_block[key] = {}
        for arm in ("hold",) + tuple(close_arms):
            a = acc[(L, MNAME, arm)]
            pnls = [p for _, p in a["pairs"]]
            tot = a["closed_e4"] + a["forced_e4"] + a["carried_e4"]
            hs = sorted(a["hold_s"])
            ab[arm] = {
                "positions": len(a["pairs"]),
                "mean_net_pnl_c": round(sum(pnls) / len(pnls), 3) if pnls else None,
                "ci95_cluster_boot": cluster_boot_ci(a["pairs"]),
                "frac_closed_passive": round(a["closed_e4"] / tot, 4) if tot else None,
                "frac_forced_flat": round(a["forced_e4"] / tot, 4) if tot else None,
                "frac_carried_to_settle": round(a["carried_e4"] / tot, 4) if tot else None,
                "hold_secs_p50": round(hs[len(hs) // 2], 1) if hs else None,
                "exit_kinds": dict(acc[(L, MNAME, arm)]["kinds"]),
            }
    return entry_block, arms_block


def check_baseline(entry_block, expect):
    """Every gated field must match e4_front exactly."""
    diffs = []
    for key, exp in expect.items():
        got = entry_block.get(key, {})
        for f, v in exp.items():
            if got.get(f) != v:
                diffs.append(f"{key}.{f}: expected {v} got {got.get(f)}")
    return diffs


# ------------------------------------------------------------------- main
def main():
    import duckdb
    os.makedirs(OUT, exist_ok=True)

    meta = {}
    d = json.load(open(f"{SNAP_DIR}/shards/KXBTC15M.json"))
    for t, mk in d["markets"].items():
        ct = mk.get("close_time") or mk.get("expected_expiration_time")
        res = str(mk.get("result") or "").lower()
        try:
            cu = int(dt.datetime.fromisoformat(
                str(ct).replace("Z", "+00:00")).timestamp() * 1e6)
        except Exception:
            continue
        if res in ("yes", "no"):
            meta[t] = (cu, res)
    print("settled meta:", len(meta), flush=True)

    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='10GB'")
    lpats = glob.glob(L2_GLOB, recursive=True)
    tpats = glob.glob(TR_GLOB, recursive=True)

    results = {"schema_version": "e4-twoleg-closeout-v1"}
    all_diffs = []

    print("KNOWN-ANSWER graveyard VAL (hold only) ...", flush=True)
    ka_entry, _ = run_policy(con, lpats, tpats, meta, DATES_VAL,
                             0, 300_000_000, -1, {})
    results["known_answer_graveyard_VAL"] = ka_entry
    print(json.dumps(ka_entry, indent=1), flush=True)
    ka_ok = all((c["mean_settle_pnl_c"] is None or c["mean_settle_pnl_c"] < -0.5)
                for c in ka_entry.values() if c["fills"])
    results["known_answer_verdict"] = "RED_OK" if ka_ok else "NOT_RED_ENGINE_SUSPECT"
    all_diffs += check_baseline(
        ka_entry, BASELINE_EXPECT["known_answer_graveyard_VAL"])
    print("known-answer verdict:", results["known_answer_verdict"],
          "| baseline diffs so far:", all_diffs, flush=True)

    if ka_ok and not all_diffs:
        for split, dates in (("TRAIN", DATES_TRAIN), ("VALIDATE", DATES_VAL)):
            print(f"MAIN {split} ...", flush=True)
            entry, arms = run_policy(
                con, lpats, tpats, meta, dates,
                ENTRY_JOIN_LO_US, ENTRY_JOIN_HI_US, ENTRY_WITHDRAW_US,
                CLOSE_ARMS)
            results[f"main_{split}"] = entry
            results[f"arms_{split}"] = arms
            all_diffs += check_baseline(entry, BASELINE_EXPECT[f"main_{split}"])
            print(json.dumps({"entry": entry, "arms": arms}, indent=1), flush=True)

    results["baseline_gate"] = {
        "expected_from": "ORIGINAL e4_front.py log (report sha 753adc5d9f6fbf31)",
        "diffs": all_diffs,
        "pass": not all_diffs,
    }
    results["verdict"] = ("VOID_BASELINE_MISMATCH" if all_diffs else
                          ("VOID_KNOWN_ANSWER" if not ka_ok else "OK"))
    results["generated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    results["policy"] = {
        "clip": 5, "entry": "e4_front verbatim (tail & front, 10m<=tte<30m, "
        "withdraw T-5m, max 1 fill/side/market, maker fee 0)",
        "arms": {"hold": "to settlement (baseline)",
                 **{a: f"passive exit at entry+{o // 100}c, ladder -1c at "
                    "30/60/120s, T-3m taker flatten (7% quadratic fee, "
                    "order rounded up to cent), no-bid -> carried"
                    for a, o in CLOSE_ARMS.items()}},
        "exit_fill_model": "trade-through on opposite bid book, tail join, "
                           "activation fill_ts+cancel_lat, no fills past T-3m",
        "split": "TRAIN 07-12..19 / VALIDATE 07-20..23, cluster=market",
    }
    try:
        results["script_sha256"] = hashlib.sha256(
            open(os.path.abspath(__file__), "rb").read()).hexdigest()
    except Exception:
        pass
    payload = json.dumps(results, sort_keys=True, default=float, indent=1).encode()
    open(f"{OUT}/E4_TWOLEG_REPORT.json", "wb").write(payload)
    json.dump({"ok": results["verdict"] == "OK"}, open(f"{OUT}/DONE.json", "w"))
    print("VERDICT:", results["verdict"], flush=True)
    print("REPORT sha256:", hashlib.sha256(payload).hexdigest(), flush=True)


if __name__ == "__main__":
    main()
