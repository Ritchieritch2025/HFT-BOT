# -*- coding: utf-8 -*-
"""GRID-REPLAY v2 — inventory-aware maker replay, KXBTC15M, 12 days.

Successor to the VOIDED grid_replay.py (2026-07-25): that run's base arm
dropped the one-fill-per-side latch and printed 199,482 fills / −1.642c
against a true baseline of 190 fills / +0.32c.  v2 is built so that class
of failure CANNOT pass silently:

  * The entry kernel is a hook-parameterized port of the e4_front engine
    (the only engine that has reproduced the original baseline on every
    field).  With neutral parameters the hooks are identities.
  * BASELINE GATE: before any grid arm runs, the neutral arms replay the
    known-answer graveyard + TRAIN + VALIDATE and every field
    (quotes_placed / fills / fill_rate / markets_quoted / clusters_filled
    / mean_settle_pnl_c) is asserted against e4's BASELINE_EXPECT —
    VALIDATE front = 274 placed / 190 fills / 95 markets / +0.321c.
    ANY drift -> non-zero exit, no arm output is written.
  * Grid arms may only move quotes in the PESSIMISTIC direction: skew
    retreats (never improves) the accumulating side and a retreated
    quote joins the tail of the displayed queue at its level.

Arms grid: refill (re-arm after fills, replacing the latch) x hard
per-market inventory cap x inventory-retreat skew gamma (cents/contract)
x min-requote threshold (F5 evidence) x the PAIRING family (operator
doctrine 2026-07-26, 吃差价 pivot): both sides always quoted; after one
side fills the opposite side is pushed to the pair-cost ceiling
(<=99c locks >=1c, Kalshi nets the position so capital releases);
unpaired lots taker-flatten at age ~90s or at T-5m, never carried into
the death zone.  This is DISTINCT from the killed e4_twoleg family
(fixed offset from own entry, hold-side exit, losers kept to T-3m).

Run (EC2 prod, data lives there):
  cd ~/hft-bot && python3 tools/research/crypto_mm/grid_replay_v2.py
Exit status 0 = gate passed and report written; 1 = gate FAILED, run void.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from tools.research.crypto_mm.e4_twoleg_closeout import (
        BASELINE_EXPECT, check_baseline, cluster_boot_ci,
        taker_fee_c_per_order, walk_book_sell)
except ImportError:                               # running from crypto_mm/
    from e4_twoleg_closeout import (
        BASELINE_EXPECT, check_baseline, cluster_boot_ci,
        taker_fee_c_per_order, walk_book_sell)

# ---------------------------------------------------------------- constants
CLIP = 5 * 10_000                 # e4 units (5 contracts)
CLIP_CT = 5                       # contracts per fill
LAT_US = 60_000
DATES_TRAIN = [f"2026-07-{d}" for d in range(12, 20)]
DATES_VAL = [f"2026-07-{d}" for d in range(20, 24)]
JOIN_LO_US = 600_000_000          # 10m
JOIN_HI_US = 1_800_000_000        # 30m
WITHDRAW_US = 300_000_000         # 5m hard withdraw
KA_JOIN_LO_US = 0                 # known-answer graveyard: join to the end
KA_JOIN_HI_US = 300_000_000
KA_WITHDRAW_US = -1               # never withdraw
EPS_QTY = 0                       # e4 best(): (v or 0) > 0

SNAP_DIR = os.environ.get(
    "E4_SNAP_DIR",
    "/home/ubuntu/h6b_inputs/catalog_normal/snapshot=20260724T213017Z")
L2_GLOB = os.environ.get(
    "E4_L2_GLOB",
    "/home/ubuntu/hft-bot/work/warehouse/facts/orderbooks_full/category=Crypto/**/*.parquet")
TR_GLOB = os.environ.get(
    "E4_TR_GLOB",
    "/home/ubuntu/hft-bot/work/warehouse/facts/trades/category=Crypto/**/*.csv.gz")
OUT = os.environ.get("GRID2_OUT", "/home/ubuntu/h6b_inputs/grid_v2")


# ------------------------------------------------------------------- arms
@dataclass(frozen=True)
class Arm:
    """One policy variant.  Neutral == e4_front semantics exactly."""
    name: str
    mode: str                     # "tail" (join best) | "front" (+0.1c)
    refill: bool = False          # re-arm a side after a fill (no latch)
    cap: int = 999                # per-market |net contracts| ceiling
    gamma_c: float = 0.0          # retreat cents per contract of inventory
    requote_min_c: float = 0.0    # F5: suppress reprices below this
    # -- pairing family (operator doctrine 2026-07-26, 吃差价 pivot).
    # NOT the killed e4_twoleg family: that one priced a same-inventory
    # exit at a fixed offset from its OWN entry; here the opposite side
    # is an independent ENTRY pushed to the pair-cost ceiling, both
    # sides always live, and unpaired lots taker-flatten young.
    pair: bool = False            # pair-off opposite fills, push + flatten
    pair_lock_c: float = 99.0     # max pair cost, cents (99 locks >=1c)
    unpaired_max_lots: int = 1    # per-side unpaired lot ceiling
    flatten_age_s: float = 90.0   # unpaired age -> taker flatten

    @property
    def imp_e4(self) -> int:
        return 10 if self.mode == "front" else 0


NEUTRAL_ARMS = (
    Arm(name="tail", mode="tail"),
    Arm(name="front", mode="front"),
)

GRID_ARMS = (
    Arm(name="base_tail", mode="tail"),
    Arm(name="base_front", mode="front"),
    Arm(name="refill_nocap", mode="tail", refill=True, cap=999),
    Arm(name="refill_cap5", mode="tail", refill=True, cap=5),
    Arm(name="refill_cap10", mode="tail", refill=True, cap=10),
    Arm(name="refill_cap10_front", mode="front", refill=True, cap=10),
    Arm(name="skew02_cap10", mode="tail", refill=True, cap=10, gamma_c=0.2),
    Arm(name="skew05_cap10", mode="tail", refill=True, cap=10, gamma_c=0.5),
    Arm(name="skew10_cap10", mode="tail", refill=True, cap=10, gamma_c=1.0),
    Arm(name="skew05_cap10_front", mode="front", refill=True, cap=10,
        gamma_c=0.5),
    Arm(name="thr05", mode="tail", requote_min_c=0.5),
    Arm(name="thr05_front", mode="front", requote_min_c=0.5),
    Arm(name="pair99_lots1_age90", mode="tail", refill=True, pair=True),
    Arm(name="pair99_lots2_age90", mode="tail", refill=True, pair=True,
        unpaired_max_lots=2),
    Arm(name="pair98_lots1_age90", mode="tail", refill=True, pair=True,
        pair_lock_c=98.0),
    Arm(name="pair99_lots1_age30", mode="tail", refill=True, pair=True,
        flatten_age_s=30.0),
    Arm(name="pair99_lots1_agemax", mode="tail", refill=True, pair=True,
        flatten_age_s=1e9),       # isolates the push; T-5m flatten only
    Arm(name="pair99_lots1_age90_front", mode="front", refill=True,
        pair=True),
)


@dataclass
class ArmState:
    """Per-arm tallies.  One instance per market; merge() aggregates."""
    quotes: dict = field(default_factory=lambda: {"y": None, "n": None})
    done: dict = field(default_factory=lambda: {"y": False, "n": False})
    q: int = 0                    # net contracts (+ = long YES)
    q_peak: int = 0
    placed: int = 0
    fills: int = 0
    pnl: list = field(default_factory=list)
    pairs: list = field(default_factory=list)     # (mt, pnl) for boot CI
    markets: set = field(default_factory=set)
    clusters: set = field(default_factory=set)
    requote_suppressed: int = 0
    carried_ct: int = 0           # contracts alive at market end
    unpaired: dict = field(default_factory=lambda: {"y": [], "n": []})
    paired_ct: int = 0            # contracts netted by opposite fills
    flattened_ct: int = 0         # contracts closed by taker flatten

    def merge(self, other: "ArmState") -> None:
        self.placed += other.placed
        self.fills += other.fills
        self.pnl.extend(other.pnl)
        self.pairs.extend(other.pairs)
        self.markets |= other.markets
        self.clusters |= other.clusters
        self.q_peak = max(self.q_peak, other.q_peak)
        self.requote_suppressed += other.requote_suppressed
        self.carried_ct += other.carried_ct
        self.paired_ct += other.paired_ct
        self.flattened_ct += other.flattened_ct


def best(bk):
    return max((p for p, v in bk.items() if (v or 0) > EPS_QTY), default=None)


class MarketSim:
    """One market, all arms.  Event ordering is e4_front verbatim:
    consume tape up to the book event's ts, apply the book event, then run
    the quoting loop.  Fills happen ONLY inside the tape (strict
    trade-through: cumulative taker volume must exceed queue_ahead+CLIP)."""

    def __init__(self, mt, close_us, res, arms, lat_us, trades,
                 join_lo_us, join_hi_us, withdraw_us, depth=None):
        self.mt = mt
        self.close = close_us
        self.res = res
        self.arms = arms
        self.lat = lat_us
        self.tr = trades
        self.ti = 0
        self.books = {"y": {}, "n": {}}
        self.join_lo = join_lo_us
        self.join_hi = join_hi_us
        self.withdraw = withdraw_us
        self.states = {a.name: ArmState() for a in arms}
        self.depth = depth if depth is not None else Counter()

    def arm_state(self, name) -> ArmState:
        return self.states[name]

    # ------------------------------------------------------------- tape
    def _consume_trades(self, upto):
        while self.ti < len(self.tr) and self.tr[self.ti][0] <= upto:
            t_ts, t_px, t_qty, taker = self.tr[self.ti]
            self.ti += 1
            self._depth_sample(t_px, taker)
            for arm in self.arms:
                st = self.states[arm.name]
                for side_key, sell_taker in (("y", "no"), ("n", "yes")):
                    if not arm.refill and st.done[side_key]:
                        continue
                    qd = st.quotes[side_key]
                    if qd is None:
                        continue
                    if qd["cx"] is not None and t_ts >= qd["cx"]:
                        st.quotes[side_key] = None
                        continue
                    if taker != sell_taker:
                        continue
                    px_side = t_px if side_key == "y" else 10000 - t_px
                    if px_side <= qd["lvl"]:
                        qd["ahead"] -= t_qty
                        if qd["ahead"] < -CLIP:
                            self._fill(arm, st, side_key, qd["lvl"], t_ts)

    def _fill(self, arm, st, side_key, lvl, t_ts):
        st.fills += 1
        st.clusters.add(self.mt)
        st.quotes[side_key] = None
        st.q += CLIP_CT if side_key == "y" else -CLIP_CT
        st.q_peak = max(st.q_peak, abs(st.q))
        if not arm.refill:
            st.done[side_key] = True
        if arm.pair:
            opp = "n" if side_key == "y" else "y"
            if st.unpaired[opp]:
                # FIFO: pair against the OLDEST opposite lot.  Locked
                # P&L = 100 - both entries; booked as c/contract halves
                # so the mean stays per-contract comparable to hold arms.
                e_opp, _ts0 = st.unpaired[opp].pop(0)
                locked = (10000 - lvl - e_opp) / 100.0
                st.pnl.extend((locked / 2.0, locked / 2.0))
                st.pairs.extend(((self.mt, locked / 2.0),
                                 (self.mt, locked / 2.0)))
                st.paired_ct += 2 * CLIP_CT
            else:
                st.unpaired[side_key].append((lvl, t_ts))
            return
        entry_c = lvl / 100.0
        win = (self.res == "yes") == (side_key == "y")
        pnl = (100.0 if win else 0.0) - entry_c
        st.pnl.append(pnl)
        st.pairs.append((self.mt, pnl))

    def _flatten_lot(self, st, side_key, entry_e4):
        """Taker-flatten one unpaired lot into displayed same-side bids
        (walk best-first, official 7% quadratic fee); the undisplayed
        remainder settles.  At most one lot per book event — displayed
        liquidity is never double-counted inside a single event."""
        own = self.books[side_key]
        fills, filled = walk_book_sell(own, CLIP)
        rem = CLIP - filled
        total_c = sum((p - entry_e4) / 100.0 * (q / 10000.0)
                      for p, q in fills)
        if filled:
            total_c -= taker_fee_c_per_order(fills)
        if rem:
            win = (self.res == "yes") == (side_key == "y")
            total_c += ((10000 if win else 0) - entry_e4) / 100.0 \
                * (rem / 10000.0)
        pnl = total_c / (CLIP / 10000.0)
        st.pnl.append(pnl)
        st.pairs.append((self.mt, pnl))
        st.flattened_ct += int(filled / 10000)
        st.carried_ct += int(rem / 10000)
        st.q += -CLIP_CT if side_key == "y" else CLIP_CT

    def _age_flatten(self, arm, st, ts, tte):
        """Unpaired lots die young: at flatten_age_s, or unconditionally
        once inside the T-5m withdraw window (never carry into the
        death zone)."""
        age_us = int(arm.flatten_age_s * 1_000_000)
        for side_key in ("y", "n"):
            lots = st.unpaired[side_key]
            if not lots:
                continue
            lvl, t_fill = lots[0]
            if ts - t_fill >= age_us or tte <= self.withdraw:
                lots.pop(0)
                self._flatten_lot(st, side_key, lvl)

    def _depth_sample(self, t_px, taker):
        """Trade-through depth beyond best, in 0.1c bins (gamma evidence)."""
        if taker == "no":
            b = best(self.books["y"])
            if b is not None and t_px < b:
                self.depth[min(100, b - t_px) // 10] += 1
        elif taker == "yes":
            b = best(self.books["n"])
            if b is not None and (10000 - t_px) < b:
                self.depth[min(100, b - (10000 - t_px)) // 10] += 1

    # ------------------------------------------------------- quoting hooks
    def _blocked_by_cap(self, arm, st, side_key) -> bool:
        if arm.pair:
            return len(st.unpaired[side_key]) >= arm.unpaired_max_lots
        adverse = st.q if side_key == "y" else -st.q
        return arm.refill and adverse >= arm.cap

    def _quote_target(self, arm, st, side_key, b, bk):
        """(level, queue_ahead) for a fresh quote.  Neutral == e4:
        tail joins best (ahead = displayed), front improves +0.1c
        (ahead = 0).  Inventory retreat only moves the ACCUMULATING side
        away from the market; a retreated quote is a tail join at its
        level — both choices pessimistic for the arm."""
        base = b + arm.imp_e4
        if arm.pair:
            opp = "n" if side_key == "y" else "y"
            lots = st.unpaired[opp]
            if lots:
                # Push toward the pair-cost ceiling for the OLDEST lot:
                # the highest price that still locks the pair, clamped a
                # whole cent under the crossing point (post-only).  If
                # the displayed best already exceeds the ceiling, rest
                # AT the ceiling (tail of its level) — the lock is never
                # violated for a faster fill.
                ceiling = int(round(arm.pair_lock_c * 100)) - lots[0][0]
                opp_best = best(self.books[opp])
                push = ceiling if opp_best is None else \
                    min(ceiling, (10000 - opp_best) - 100)
                lvl = max(100, min(push, 9900))
                return lvl, (bk.get(lvl, 0) or 0)
        adverse = st.q if side_key == "y" else -st.q
        retreat = int(round(arm.gamma_c * adverse * 100)) if adverse > 0 else 0
        if retreat > 0:
            lvl = max(10, base - retreat)
            return lvl, (bk.get(lvl, 0) or 0)
        if arm.mode == "front":
            return base, 0
        return base, (bk.get(b, 0) or 0)

    # ------------------------------------------------------------- events
    def on_book_event(self, ts, mtype, side, px, dq, y_levels, n_levels):
        if not self.close:
            return
        self._consume_trades(ts)
        if mtype == "snapshot":
            self.books = {"y": {}, "n": {}}
            for arr, key in ((y_levels, "y"), (n_levels, "n")):
                for p_, v_ in (arr or []):
                    if p_ is not None:
                        self.books[key][p_] = v_ or 0
        else:
            if px is None or side not in ("yes", "no"):
                return
            bk = self.books["y" if side == "yes" else "n"]
            bk[px] = bk.get(px, 0) + (dq or 0)
        tte = self.close - ts
        for arm in self.arms:
            st = self.states[arm.name]
            if arm.pair:
                self._age_flatten(arm, st, ts, tte)
            for side_key in ("y", "n"):
                if not arm.refill and st.done[side_key]:
                    continue
                bk = self.books[side_key]
                qd = st.quotes[side_key]
                b = best(bk)
                if qd is not None and qd["cx"] is not None and ts >= qd["cx"]:
                    st.quotes[side_key] = None
                    qd = None
                if tte <= self.withdraw:
                    if qd is not None and qd["cx"] is None:
                        qd["cx"] = ts + self.lat
                    continue
                if not (self.join_lo <= tte < self.join_hi):
                    continue
                if self._blocked_by_cap(arm, st, side_key):
                    if qd is not None and qd["cx"] is None:
                        qd["cx"] = ts + self.lat
                    continue
                if qd is None:
                    if b is not None:
                        lvl, ahead = self._quote_target(arm, st, side_key, b, bk)
                        st.quotes[side_key] = {"lvl": lvl, "ahead": ahead,
                                               "cx": None}
                        st.placed += 1
                        st.markets.add(self.mt)
                    continue
                target = None
                if b is not None:
                    target, _ = self._quote_target(arm, st, side_key, b, bk)
                if target != qd["lvl"] and qd["cx"] is None:
                    if (target is not None and arm.requote_min_c > 0
                            and abs(target - qd["lvl"])
                            < int(round(arm.requote_min_c * 100))):
                        st.requote_suppressed += 1
                        continue
                    qd["cx"] = ts + self.lat

    def finalize(self):
        """Market stream ended: entry quotes die, inventory carries.
        e4 harvest kills entry quotes BEFORE draining the tape, so the
        drained tail can never produce entry fills — same here."""
        for arm in self.arms:
            st = self.states[arm.name]
            st.quotes = {"y": None, "n": None}
            if arm.pair:
                # Unpaired lots at data end settle (carried_no_exit).
                for side_key in ("y", "n"):
                    for lvl, _t in st.unpaired[side_key]:
                        win = (self.res == "yes") == (side_key == "y")
                        pnl = (100.0 if win else 0.0) - lvl / 100.0
                        st.pnl.append(pnl)
                        st.pairs.append((self.mt, pnl))
                        st.carried_ct += CLIP_CT
                    st.unpaired[side_key] = []
                continue
            st.carried_ct += abs(st.q)


# --------------------------------------------------------------- reporting
def entry_stats_block(states, lat_us):
    """e4-shaped entry block; gate fields only."""
    out = {}
    for name, s in states.items():
        out[name] = {
            "quotes_placed": s.placed,
            "fills": s.fills,
            "fill_rate": round(s.fills / s.placed, 4) if s.placed else None,
            "markets_quoted": len(s.markets),
            "clusters_filled": len(s.clusters),
            "mean_settle_pnl_c": (round(sum(s.pnl) / len(s.pnl), 3)
                                  if s.pnl else None),
        }
    return out


def arm_report_block(states):
    out = {}
    for name, s in states.items():
        blk = entry_stats_block({name: s}, LAT_US)[name]
        filled_ct = s.fills * CLIP_CT
        blk.update({
            "ci95_cluster_boot": cluster_boot_ci(s.pairs),
            "q_peak_contracts": s.q_peak,
            "carried_contracts": s.carried_ct,
            "paired_contracts": s.paired_ct,
            "flattened_contracts": s.flattened_ct,
            "pct_carried_of_filled": (round(s.carried_ct / filled_ct, 4)
                                      if filled_ct else None),
            "requote_suppressed": s.requote_suppressed,
            "gray": len(s.clusters) < 30,
        })
        out[name] = blk
    return out


def gate_or_die(got_block, expect_block, label):
    """Every gated field must match exactly or the whole run is VOID."""
    diffs = check_baseline(got_block, expect_block)
    if diffs:
        print(f"BASELINE GATE FAILED [{label}] — RUN VOID", flush=True)
        for d in diffs:
            print("  DIFF:", d, flush=True)
        sys.exit(1)
    print(f"baseline gate OK [{label}]", flush=True)


# ------------------------------------------------------------------ driver
def run_dates(con, lpats, tpats, meta, dates, arms,
              join_lo, join_hi, withdraw, gate_keyed=False):
    agg = {a.name: ArmState() for a in arms}
    depth = Counter()
    for date in dates:
        lf = [p for p in lpats if f"date={date}" in p]
        tf = [p for p in tpats if f"date={date}" in p]
        if not lf:
            continue
        trades = {}
        for mt, ts, px, qty, side in con.execute("""
            SELECT market_ticker, ts_utc, yes_price_e4, count_e4, taker_side
            FROM read_csv(?, header=true, union_by_name=true,
                          types={'taker_side':'VARCHAR'})
            WHERE series_ticker='KXBTC15M'
            ORDER BY market_ticker, ts_utc""", [tf]).fetchall():
            trades.setdefault(mt, []).append((int(ts), px, qty or 0, side))
        cur = con.execute("""
            SELECT market_ticker, ts_utc, msg_type, side, price_e4, delta_e4,
                   CAST(yes_levels AS VARCHAR), CAST(no_levels AS VARCHAR)
            FROM read_parquet(?, union_by_name=true)
            ORDER BY market_ticker, ts_utc, ws_seq""", [lf])
        sim = None

        def harvest(s):
            if s is None:
                return
            s.finalize()
            for name, st in s.states.items():
                agg[name].merge(st)
                agg[name].carried_ct += 0    # merged via merge()

        while True:
            rows = cur.fetchmany(400_000)
            if not rows:
                break
            for mt, ts, mtype, side, px, dq, yl, nl in rows:
                ts = int(ts)
                if sim is None or sim.mt != mt:
                    harvest(sim)
                    close_us, res = meta.get(mt, (0, None))
                    sim = MarketSim(mt, close_us, res, list(arms), LAT_US,
                                    trades.get(mt, []), join_lo, join_hi,
                                    withdraw, depth)
                yl_p = nl_p = None
                if mtype == "snapshot":
                    try:
                        yl_p = json.loads(yl) if yl else []
                        nl_p = json.loads(nl) if nl else []
                    except Exception:
                        yl_p, nl_p = [], []
                sim.on_book_event(ts, mtype, side, px, dq, yl_p, nl_p)
        harvest(sim)
        print(f"  {date} done", flush=True)
    if gate_keyed:
        keyed = {f"{n}_cancel_{LAT_US // 1000}ms": s for n, s in agg.items()}
        return keyed, depth
    return agg, depth


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

    results = {"schema_version": "grid-replay-v2"}

    # ---- BASELINE GATE: neutral arms must reproduce e4 on every field ----
    print("GATE 1/3 known-answer graveyard VAL ...", flush=True)
    blk, _ = run_dates(con, lpats, tpats, meta, DATES_VAL, NEUTRAL_ARMS,
                       KA_JOIN_LO_US, KA_JOIN_HI_US, KA_WITHDRAW_US,
                       gate_keyed=True)
    ka_block = entry_stats_block(blk, LAT_US)
    results["gate_known_answer_graveyard_VAL"] = ka_block
    ka_red = all((c["mean_settle_pnl_c"] is None or c["mean_settle_pnl_c"] < -0.5)
                 for c in ka_block.values() if c["fills"])
    if not ka_red:
        print("KNOWN-ANSWER NOT RED — ENGINE SUSPECT, RUN VOID", flush=True)
        sys.exit(1)
    gate_or_die(ka_block, BASELINE_EXPECT["known_answer_graveyard_VAL"],
                "known_answer_graveyard_VAL")

    for label, dates in (("main_TRAIN", DATES_TRAIN),
                         ("main_VALIDATE", DATES_VAL)):
        print(f"GATE {'2' if 'TRAIN' in label else '3'}/3 {label} ...",
              flush=True)
        blk, _ = run_dates(con, lpats, tpats, meta, dates, NEUTRAL_ARMS,
                           JOIN_LO_US, JOIN_HI_US, WITHDRAW_US,
                           gate_keyed=True)
        eb = entry_stats_block(blk, LAT_US)
        results[f"gate_{label}"] = eb
        gate_or_die(eb, BASELINE_EXPECT[label], label)

    # ---- grid arms (only reached with the gate green) ----
    for label, dates in (("TRAIN", DATES_TRAIN), ("VALIDATE", DATES_VAL)):
        print(f"GRID {label} ...", flush=True)
        agg, depth = run_dates(con, lpats, tpats, meta, dates, GRID_ARMS,
                               JOIN_LO_US, JOIN_HI_US, WITHDRAW_US)
        results[f"grid_{label}"] = arm_report_block(agg)
        results[f"depth_hist_0p1c_bins_{label}"] = {
            str(k): v for k, v in sorted(depth.items())}
        print(json.dumps(results[f"grid_{label}"], indent=1), flush=True)

    results["baseline_gate"] = {
        "expected_from": "e4_twoleg_closeout.BASELINE_EXPECT "
                         "(ORIGINAL e4_front.py, report sha 753adc5d9f6fbf31)",
        "pass": True,
    }
    results["arms"] = [vars(a) | {"imp_e4": a.imp_e4} for a in GRID_ARMS]
    results["policy_notes"] = {
        "entry": "e4_front verbatim semantics via neutral hooks; refill arms "
                 "replace the latch with cap+skew; retreat is one-sided and "
                 "tail-joins its level (pessimistic)",
        "exit_legs": "fixed-offset closeout family killed by "
                     "e4_twoleg_closeout; the pair arms are a different "
                     "shape: opposite-side entry pushed to the pair-cost "
                     "ceiling + age/T-5m taker flatten of unpaired lots",
        "split": "TRAIN 07-12..19 / VALIDATE 07-20..23, cluster=market",
    }
    results["generated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        results["script_sha256"] = hashlib.sha256(
            open(os.path.abspath(__file__), "rb").read()).hexdigest()
    except Exception:
        pass
    payload = json.dumps(results, sort_keys=True, default=float,
                         indent=1).encode()
    open(f"{OUT}/GRID2_REPORT.json", "wb").write(payload)
    json.dump({"ok": True}, open(f"{OUT}/DONE.json", "w"))
    print("VERDICT: OK — gate passed, report written", flush=True)
    print("REPORT sha256:", hashlib.sha256(payload).hexdigest(), flush=True)


if __name__ == "__main__":
    main()
