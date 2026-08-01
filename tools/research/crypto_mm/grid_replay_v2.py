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
    # -- orphan-exit family (2026-07-26 shadow decomposition: 536 natural
    # pairs +1.11c avg vs 195 blind 60s taker flattens -12.60c avg; the
    # whole loss is the orphan path).  Both knobs default OFF so every
    # pre-existing arm and the baseline gate are bit-identical.
    orphan_maker_age_s: float = 1e9   # oldest-lot age at which the
                                      # complement push relaxes from the
                                      # profit ceiling to a bounded-loss
                                      # maker exit (still post-only)
    orphan_maker_max_loss_c: float = 0.0  # widest accepted negative lock
    admit_pair_gap_c: float = -1.0    # entry admission: the future
                                      # complement ceiling must land within
                                      # this many cents of the opposite
                                      # book's current best (<0 = off)
    maker_grace_s: float = 0.0    # after age: wait this long before taker
    min_opp_depth_ct: int = 0     # displayed opposite depth required to enter
    # -- round-2 world model (2026-07-27): toxicity shield + harvest gate.
    # shield_usd: an ENTRY fill is refused (quote treated as already
    # cancelled by the fast-anchor pull) when the Binance perp moved more
    # than this many dollars inside [t - shield_win_ms, t - SHIELD_LAT_MS].
    # Model-free dollar threshold (the replay has no fair); exits/complement
    # pushes are never shielded.  0 = off (bit-identical legacy behavior).
    shield_usd: float = 0.0
    shield_win_ms: int = 1500
    # harvest_imb: fresh ENTRY quotes allowed only while |signed taker flow
    # share| over the trailing 60s of this market's own tape is below this
    # (balanced two-way flow = retail hours).  >=1 = off.
    harvest_imb: float = 1.0

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
    # New validation arms.  maker_grace is deliberately a conservative
    # proxy: the replay has no independent exit-order queue, so waiting is
    # never counted as a maker fill; the lot is taker-flattened only after
    # the grace window (or at T-5m).
    Arm(name="pair99_lots1_age90_maker_grace30", mode="tail", refill=True,
        pair=True, maker_grace_s=30.0),
    Arm(name="pair99_lots1_age90_oppdepth5", mode="tail", refill=True,
        pair=True, min_opp_depth_ct=5),
    # Orphan-exit family, aggressive variants (2026-07-26 live-shadow
    # decomposition: all loss sits in blind taker flattens at -12.60c avg
    # vs +1.11c natural pairs; break-even needs 92% completion OR orphan
    # cost under ~3c).  mkexit relaxes the complement ceiling to a bounded
    # negative lock (still post-only maker) before the hard taker deadline;
    # admit_gap refuses entries whose complement ceiling sits far below
    # the opposite best (orphan factories).  TRAIN selection only.
    Arm(name="mkexit_a30_ml1_h120", mode="tail", refill=True, pair=True,
        flatten_age_s=120.0, orphan_maker_age_s=30.0,
        orphan_maker_max_loss_c=1.0),
    Arm(name="mkexit_a30_ml3_h120", mode="tail", refill=True, pair=True,
        flatten_age_s=120.0, orphan_maker_age_s=30.0,
        orphan_maker_max_loss_c=3.0),
    Arm(name="mkexit_a60_ml3_h180", mode="tail", refill=True, pair=True,
        flatten_age_s=180.0, orphan_maker_age_s=60.0,
        orphan_maker_max_loss_c=3.0),
    Arm(name="admit_gap1_age90", mode="tail", refill=True, pair=True,
        admit_pair_gap_c=1.0),
    Arm(name="admit_gap2_age90", mode="tail", refill=True, pair=True,
        admit_pair_gap_c=2.0),
    Arm(name="combo_gap1_mkexit_a30_ml3", mode="tail", refill=True,
        pair=True, flatten_age_s=120.0, orphan_maker_age_s=30.0,
        orphan_maker_max_loss_c=3.0, admit_pair_gap_c=1.0),
)


def _round2_arms():
    """Shield × harvest grid over the round-1 winning orphan arms.
    Base arm list is provisional until the round-1 merge lands; adjust
    ROUND2_BASE then relaunch with GRID2_ARMSET=round2."""
    # Round-1 VALIDATE ranking (TRAIN pair numbers voided as phantom-book
    # artifacts): agemax +0.025 > mkexit_a30_ml1 -0.146 > grace30 -0.154;
    # age30 was WORST (-0.627) — early taker flattens are the damage, not
    # holding.  base_front (+0.321) added: does the shield lift the one
    # already-positive strategy?
    base = {
        "agemax": dict(mode="tail", refill=True, pair=True,
                       flatten_age_s=1e9),
        "mk1": dict(mode="tail", refill=True, pair=True,
                    flatten_age_s=120.0, orphan_maker_age_s=30.0,
                    orphan_maker_max_loss_c=1.0),
        "grace": dict(mode="tail", refill=True, pair=True,
                      maker_grace_s=30.0),
        "front": dict(mode="front"),
    }
    arms = []
    for bname, kw in base.items():
        for s_usd, s_tag in ((0.0, "s0"), (15.0, "s15"), (30.0, "s30")):
            for h_imb, h_tag in ((1.0, "hoff"), (0.65, "h65")):
                arms.append(Arm(name=f"r2_{bname}_{s_tag}_{h_tag}",
                                shield_usd=s_usd, harvest_imb=h_imb, **kw))
    return tuple(arms)


if os.environ.get("GRID2_ARMSET") == "round2":
    GRID_ARMS = _round2_arms()


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
    shield_pulls: int = 0         # entry fills refused by the perp shield
    harvest_blocks: int = 0       # entry quotes refused by the flow gate
    fill_ts: list = field(default_factory=list)   # µs ts of every fill

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
        self.shield_pulls += other.shield_pulls
        self.harvest_blocks += other.harvest_blocks
        self.fill_ts.extend(other.fill_ts)


def best(bk):
    return max((p for p, v in bk.items() if (v or 0) > EPS_QTY), default=None)


class MarketSim:
    """One market, all arms.  Event ordering is e4_front verbatim:
    consume tape up to the book event's ts, apply the book event, then run
    the quoting loop.  Fills happen ONLY inside the tape (strict
    trade-through: cumulative taker volume must exceed queue_ahead+CLIP)."""

    def __init__(self, mt, close_us, res, arms, lat_us, trades,
                 join_lo_us, join_hi_us, withdraw_us, depth=None, perp=None):
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
        self.perp = perp              # (ts_ms_list, px_list) or None
        self.flow = []                # (ts_us, signed_qty) trailing tape
        self.flow_i = 0               # first live index of the 60s window
        self.flow_pos = 0.0           # signed sum inside window
        self.flow_tot = 0.0           # absolute sum inside window

    def arm_state(self, name) -> ArmState:
        return self.states[name]

    # ------------------------------------------------------------- tape
    def _shield_hit(self, arm, st, side_key, t_us):
        """True when the fast-anchor shield would have pulled this ENTRY
        quote before the tape reached it: Binance perp moved more than
        shield_usd inside [t - shield_win_ms, t - SHIELD_LAT_MS].
        Exit/complement pushes are never shielded."""
        if arm.shield_usd <= 0 or self.perp is None:
            return False
        if arm.pair and st.unpaired["n" if side_key == "y" else "y"]:
            return False              # complement push is an exit
        ts_list, px_list = self.perp
        t_ms = t_us // 1000
        import bisect as _b
        i1 = _b.bisect_right(ts_list, t_ms - SHIELD_LAT_MS) - 1
        i0 = _b.bisect_right(ts_list, t_ms - arm.shield_win_ms) - 1
        if i1 < 0 or i0 < 0 or i1 == i0:
            return False
        return abs(px_list[i1] - px_list[i0]) > arm.shield_usd

    def _flow_push(self, t_us, t_qty, taker):
        """Trailing 60s signed taker flow for the harvest gate (O(1) amort)."""
        signed = t_qty if taker == "yes" else -t_qty
        self.flow.append((t_us, signed))
        self.flow_pos += signed
        self.flow_tot += abs(signed)
        cutoff = t_us - 60_000_000
        while self.flow_i < len(self.flow) and self.flow[self.flow_i][0] < cutoff:
            _, s = self.flow[self.flow_i]
            self.flow_pos -= s
            self.flow_tot -= abs(s)
            self.flow_i += 1

    def _harvest_blocked(self, arm) -> bool:
        """Fresh entries only while trailing flow is two-way (retail hours)."""
        if arm.harvest_imb >= 1.0 or self.flow_tot <= 0:
            return False
        return abs(self.flow_pos) / self.flow_tot > arm.harvest_imb

    def _consume_trades(self, upto):
        while self.ti < len(self.tr) and self.tr[self.ti][0] <= upto:
            t_ts, t_px, t_qty, taker = self.tr[self.ti]
            self.ti += 1
            self._depth_sample(t_px, taker)
            self._flow_push(t_ts, t_qty, taker)
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
                            if self._shield_hit(arm, st, side_key, t_ts):
                                st.quotes[side_key] = None
                                st.shield_pulls += 1
                                continue
                            self._fill(arm, st, side_key, qd["lvl"], t_ts)

    def _fill(self, arm, st, side_key, lvl, t_ts):
        st.fills += 1
        st.clusters.add(self.mt)
        st.fill_ts.append(t_ts)
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
        # UNIT PROOF (2026-07-27, do not "fix" to ns again): warehouse
        # ts_utc is MICROSECONDS — min(ts_utc)=1783840365529319 interpreted
        # as us gives year 2026, as ns gives 1970.  close_us and the join
        # windows are also us.  The known-answer baseline gate passes with
        # us semantics; with ns the 90s timer would never fire (~25000h).
        age_us = int(arm.flatten_age_s * 1_000_000)
        for side_key in ("y", "n"):
            lots = st.unpaired[side_key]
            if not lots:
                continue
            lvl, t_fill = lots[0]
            if tte <= self.withdraw or (
                    ts - t_fill >= age_us + int(arm.maker_grace_s * 1_000_000)):
                lots.pop(0)
                self._flatten_lot(st, side_key, lvl)

    def _blocked_by_depth(self, arm, st, side_key, bk):
        """Admission-only displayed-depth gate, evaluated at this book event.

        For a prospective YES entry, the complementary NO book is the
        relevant exit/liquidity side, and vice versa.  We use only the
        currently visible levels (no future tape), so this cannot introduce
        lookahead.  A zero threshold preserves existing semantics.
        """
        if arm.min_opp_depth_ct <= 0:
            return False
        opp_key = "n" if side_key == "y" else "y"
        if st.unpaired[opp_key]:
            return False      # complement push is an EXIT, never depth-gated
        opp = self.books[opp_key]
        visible = sum(max(0, int(v or 0)) for v in opp.values()) / 10000.0
        return visible < arm.min_opp_depth_ct

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
    def _entry_blocked_by_admission(self, arm, st, side_key, b) -> bool:
        """Completability gate for fresh pair ENTRIES only (never blocks
        a complement/exit push).  The future complement ceiling
        (pair_lock_c - entry level) must land within admit_pair_gap_c of
        the opposite book's current best — if completing the pair needs
        a large favorable move, the entry is an orphan factory and is
        skipped.  Gate off (<0) or an exit context -> never blocks."""
        if not arm.pair or arm.admit_pair_gap_c < 0:
            return False
        opp = "n" if side_key == "y" else "y"
        if st.unpaired[opp]:
            return False          # complement push is an exit, never gated
        opp_best = best(self.books[opp])
        if opp_best is None:
            return True           # no displayed complement: not completable
        c_max = int(round(arm.pair_lock_c * 100)) - (b + arm.imp_e4)
        return c_max < opp_best - int(round(arm.admit_pair_gap_c * 100))

    def _blocked_by_cap(self, arm, st, side_key) -> bool:
        if arm.pair:
            return len(st.unpaired[side_key]) >= arm.unpaired_max_lots
        adverse = st.q if side_key == "y" else -st.q
        return arm.refill and adverse >= arm.cap

    def _quote_target(self, arm, st, side_key, b, bk, ts=None):
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
                if (ts is not None
                        and ts - lots[0][1]
                        >= int(arm.orphan_maker_age_s * 1_000_000)):
                    # Orphan maker exit: past the maker age, accept a
                    # bounded negative lock to complete the pair as a
                    # MAKER (fee-free, no book walk) before the hard
                    # taker deadline at flatten_age_s.  A relaxed quote
                    # still joins the tail of its level.
                    ceiling += int(round(arm.orphan_maker_max_loss_c * 100))
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
                if self._blocked_by_depth(arm, st, side_key, bk):
                    if qd is not None and qd["cx"] is None:
                        qd["cx"] = ts + self.lat
                    continue
                if qd is None:
                    if b is not None:
                        if self._entry_blocked_by_admission(
                                arm, st, side_key, b):
                            continue
                        if (self._harvest_blocked(arm)
                                and not (arm.pair and st.unpaired[
                                    "n" if side_key == "y" else "y"])):
                            st.harvest_blocks += 1
                            continue
                        lvl, ahead = self._quote_target(
                            arm, st, side_key, b, bk, ts=ts)
                        st.quotes[side_key] = {"lvl": lvl, "ahead": ahead,
                                               "cx": None}
                        st.placed += 1
                        st.markets.add(self.mt)
                    continue
                target = None
                if b is not None:
                    target, _ = self._quote_target(
                        arm, st, side_key, b, bk, ts=ts)
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


SHIELD_LAT_MS = 150               # our measured chain: recv+decide+cancel
SHIELD_SRC = os.environ.get("GRID2_SHIELD_SRC",
                            "/home/ubuntu/research_fast_anchor/hist")


def load_perp_series(date):
    """(ts_ms_list, px_list) from the Binance vision daily aggTrades zip."""
    import csv
    import io
    import zipfile
    path = os.path.join(SHIELD_SRC, f"BTCUSDT-aggTrades-{date}.zip")
    if not os.path.exists(path):
        return None
    ts_list = []
    px_list = []
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as f:
            rd = csv.reader(io.TextIOWrapper(f, "utf-8"))
            for row in rd:
                try:
                    ts_list.append(int(row[5]))
                    px_list.append(float(row[1]))
                except (ValueError, IndexError):
                    continue          # header row
    return (ts_list, px_list) if ts_list else None


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
        perp = (load_perp_series(date)
                if any(a.shield_usd > 0 for a in arms) else None)
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
            WHERE market_ticker LIKE 'KXBTC15M-%'
            ORDER BY market_ticker, ts_utc, ws_seq""", [lf])
        # Predicate pushdown: only KXBTC15M rows ever did anything (meta is
        # built solely from the KXBTC15M shard; every other market resolves
        # close=0 and is skipped row-by-row in Python).  Filtering in the
        # parquet scan is semantically identical and 3-6x fewer rows.
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
                                    withdraw, depth, perp=perp)
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
    con.execute("SET memory_limit='%s'"
                % os.environ.get("GRID2_MEM", "10GB"))
    lpats = glob.glob(L2_GLOB, recursive=True)
    tpats = glob.glob(TR_GLOB, recursive=True)

    # ---- PARTITION MODE (speed): grid arms only, subset of dates, raw dump.
    # The baseline gate must have PASSED on this exact script+data in a prior
    # full run the same session (recorded in that run's log); partitions are
    # for wall-clock parallelism, never a substitute for the gate.
    part = os.environ.get("GRID2_DATES")
    if part:
        dates = [x.strip() for x in part.split(",") if x.strip()]
        agg, depth = run_dates(con, lpats, tpats, meta, dates, GRID_ARMS,
                               JOIN_LO_US, JOIN_HI_US, WITHDRAW_US)
        dump = {}
        for name, s in agg.items():
            dump[name] = {
                "placed": s.placed, "fills": s.fills, "pnl": s.pnl,
                "pairs": [[mt, p] for mt, p in s.pairs],
                "markets": sorted(s.markets),
                "clusters": sorted(s.clusters),
                "q_peak": s.q_peak,
                "requote_suppressed": s.requote_suppressed,
                "carried_ct": s.carried_ct, "paired_ct": s.paired_ct,
                "flattened_ct": s.flattened_ct,
                "shield_pulls": s.shield_pulls,
                "harvest_blocks": s.harvest_blocks,
                "fill_ts": s.fill_ts,
            }
        pth = os.path.join(OUT, f"partial_{dates[0]}_{dates[-1]}.json")
        json.dump({"dates": dates, "script_sha256": hashlib.sha256(
                       open(os.path.abspath(__file__), "rb").read()
                   ).hexdigest(),
                   "arms": dump,
                   "depth": {str(k): v for k, v in sorted(depth.items())}},
                  open(pth, "w"))
        print("partial written:", pth, flush=True)
        return

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
