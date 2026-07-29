#!/usr/bin/env python3
"""quote_core_v2 — the operator-spec quoting policy (2026-07-28).

    盘口状态 (book, depth, flow, anchor+sigma, inventory, tte)
            ↓  连续计算两侧尺寸
    bid 挂多少 / ask 挂多少

Pure function, no I/O, no clocks: the replay harness and (later) the live
engine feed the same state vector.  Prices are anchored to the BOOK; the
model fair only gates net edge.  The core never predicts direction — it
sizes, shrinks and withdraws.

Units: cents for prices, contracts for sizes, seconds for time.
Sides: "bid" buys YES; "ask_no" buys NO (sells YES exposure).
"""
from dataclasses import dataclass, field


@dataclass
class Params:
    clip: float = 3.0            # max size per side per refresh
    q_max: float = 9.0           # inventory bound (contracts)
    flow_win_s: float = 30.0     # taker-flow lookback
    flow_imb_k: float = 1.4      # shrink strength on one-sided tape
                                 # (fraction-based: balanced=1, one-way->0)
    imb_scale: float = 0.7       # book-imbalance shrink strength (0..1)
    tte_full_s: float = 420.0    # full size above this
    tte_min_s: float = 90.0      # zero size below this
    entry_cutoff_s: float = 240.0  # no |q|-increasing quotes below this
    min_net_edge_c: float = 0.10   # required edge after AS+fee to stand
    improve_extra_c: float = 0.50  # extra net edge required to improve 1c
    as_base_c: float = 0.35      # adverse-selection charge, calm book
    as_flow_c: float = 2.00      # extra AS at a fully one-sided tape
    as_sigma_c: float = 0.15     # extra AS per unit sigma_c (cents)
    maker_fee_c: float = 0.0
    drift_veto_c: float = 0.8    # mid moved this much against a side in
                                 # the drift window -> that side is OFF
    min_spread_c: float = 0.8    # thinner books have no maker economics
    micro_tilt: float = 0.6      # microprice tilt strength from depth imb
    # completion / bail (the three knives live in the policy now)
    lock_take_age_s: float = 20.0
    lock_take_min_c: float = 0.3
    bail_age_s: float = 120.0
    taker_fee_coef_c: float = 7.0  # fee = coef * p * (1-p) cents/contract
    # completion-price ladder: the reduction quote may not pay more than
    # basis-implied breakeven minus required_lock(age); the requirement
    # decays from +ladder_lock0_c to -ladder_maxloss_c across bail_age.
    ladder_lock0_c: float = 0.5
    ladder_maxloss_c: float = 2.0


@dataclass
class MarketState:
    yes_bid_c: float             # best YES bid (cents), None -> no book
    yes_ask_c: float             # best YES ask (cents)
    bid_depth: float             # displayed qty at yes_bid
    ask_depth: float             # displayed qty at yes_ask
    flow_buy: float              # taker-bought-YES contracts in window
    flow_sell: float             # taker-sold-YES contracts in window
    fair_c: float                # model fair (None if unready)
    sigma_c: float               # per-contract vol proxy (cents)
    q: float                     # net inventory (YES-equivalent contracts)
    tte_s: float
    unpaired_age_s: float = 0.0  # age of oldest unpaired lot (0 if flat)
    basis_c: float = None        # cost of the oldest unpaired lot
    mid_drift_c: float = 0.0     # mid change over drift window (signed, c)


@dataclass
class Quotes:
    bid_px_c: float = None
    bid_sz: float = 0.0
    ask_px_c: float = None       # price to BUY NO, in NO cents
    ask_sz: float = 0.0
    take_side: str = None        # "bid"/"ask_no": IOC completion/bail order
    take_px_c: float = None
    diag: dict = field(default_factory=dict)


def taker_fee_c(px_c, p: Params):
    prob = min(max(px_c / 100.0, 0.01), 0.99)
    return p.taker_fee_coef_c * prob * (1.0 - prob)


def compute(st: MarketState, p: Params = None) -> Quotes:
    p = p or Params()
    out = Quotes()
    if st.yes_bid_c is None or st.yes_ask_c is None:
        return out
    spread_c = st.yes_ask_c - st.yes_bid_c

    # ---- overall size taper by time ----
    if st.tte_s <= p.tte_min_s:
        f_tte = 0.0
    elif st.tte_s >= p.tte_full_s:
        f_tte = 1.0
    else:
        f_tte = (st.tte_s - p.tte_min_s) / (p.tte_full_s - p.tte_min_s)

    # ---- flow: shrink the side the tape is attacking ----
    # taker sells (flow_sell) hit bids -> danger for our bid;
    # taker buys hit asks -> danger for our ask_no (short-YES side).
    # Fraction-based: absolute tape volume varies by orders of magnitude
    # across markets; what predicts adverse selection is one-sidedness.
    tot_flow = st.flow_buy + st.flow_sell
    if tot_flow > 0:
        frac_sell = st.flow_sell / tot_flow      # 0.5 = balanced
        frac_buy = st.flow_buy / tot_flow
    else:
        frac_sell = frac_buy = 0.5
    one_way_bid = max(0.0, (frac_sell - 0.5) * 2.0)   # 0..1
    one_way_ask = max(0.0, (frac_buy - 0.5) * 2.0)
    f_flow_bid = max(0.0, 1.0 - p.flow_imb_k * one_way_bid)
    f_flow_ask = max(0.0, 1.0 - p.flow_imb_k * one_way_ask)

    # ---- book imbalance: stacked bids predict up-moves -> danger for
    # the short side; stacked asks -> danger for the long side ----
    tot = st.bid_depth + st.ask_depth
    imb = (st.bid_depth - st.ask_depth) / tot if tot > 0 else 0.0
    f_imb_bid = 1.0 - p.imb_scale * max(0.0, -imb)
    f_imb_ask = 1.0 - p.imb_scale * max(0.0, imb)

    # ---- inventory: shrink the side that grows |q| ----
    # The FIRST clip of inventory is the business (a maker must be able
    # to open); the penalty ramps only on STACKING beyond one clip
    # (2026-07-29 pilot: clip=1/q_max=2 halved every first entry and,
    # with the integer floor, the core never quoted at all).
    def f_inv(side):
        q_after = st.q + p.clip if side == "bid" else st.q - p.clip
        if abs(q_after) <= abs(st.q):          # reducing: never shrunk
            return 1.0
        over = max(0.0, abs(q_after) - p.clip)
        span = max(p.q_max - p.clip, 1e-9)
        return max(0.0, 1.0 - over / span)

    # ---- price + edge gate per side ----
    # ---- micro-fair: the BOOK is the anchor.  Mid plus a depth tilt
    # (stacked bids push micro-fair up).  The model fair is only a sanity
    # clamp: micro-fair is trusted within +-3c of it when both exist.
    mid_c = (st.yes_bid_c + st.yes_ask_c) / 2.0
    micro = mid_c + p.micro_tilt * imb * (spread_c / 2.0)
    if st.fair_c is not None:
        micro = min(max(micro, st.fair_c - 3.0), st.fair_c + 3.0)

    def side_quote(side):
        one_way = one_way_bid if side == "bid" else one_way_ask
        as_c = (p.as_base_c
                + p.as_flow_c * one_way
                + p.as_sigma_c * st.sigma_c)
        # momentum veto: mid moving against this side = off
        if side == "bid" and st.mid_drift_c <= -p.drift_veto_c:
            return None, 0.0, as_c
        if side == "ask_no" and st.mid_drift_c >= p.drift_veto_c:
            return None, 0.0, as_c
        if spread_c < p.min_spread_c:
            return None, 0.0, as_c
        if side == "bid":
            join_px = st.yes_bid_c
            edge_join = micro - join_px
            improve_px = join_px + 1.0
            edge_improve = micro - improve_px
        else:
            no_bid = 100.0 - st.yes_ask_c      # best NO bid in NO cents
            join_px = no_bid
            edge_join = (100.0 - micro) - join_px
            improve_px = join_px + 1.0
            edge_improve = (100.0 - micro) - improve_px
        net_join = edge_join - as_c - p.maker_fee_c
        net_improve = edge_improve - as_c - p.maker_fee_c
        if spread_c >= 2.0 and net_improve >= (p.min_net_edge_c
                                               + p.improve_extra_c):
            return improve_px, net_improve, as_c
        if net_join >= p.min_net_edge_c:
            return join_px, net_join, as_c
        return None, 0.0, as_c

    bid_px, bid_net, as_bid = side_quote("bid")
    ask_px, ask_net, as_ask = side_quote("ask_no")

    entry_ok = st.tte_s >= p.entry_cutoff_s

    def grows(side):
        return abs((st.q + p.clip) if side == "bid"
                   else (st.q - p.clip)) > abs(st.q)

    def final_size(side, px):
        if px is None:
            return 0.0
        if grows(side) and not entry_ok:
            return 0.0
        f_fl = f_flow_bid if side == "bid" else f_flow_ask
        f_im = f_imb_bid if side == "bid" else f_imb_ask
        sz = p.clip * f_tte * f_fl * f_im * f_inv(side)
        n = int(sz + 0.5)                     # round, don't floor: at
        return float(min(n, int(p.clip))) if n >= 1 else 0.0  # clip=1 a
        # floored fraction silenced the core entirely (2026-07-29 pilot)

    out.bid_px_c, out.bid_sz = bid_px, final_size("bid", bid_px)
    out.ask_px_c, out.ask_sz = ask_px, final_size("ask_no", ask_px)

    # ---- reduction ladder: closing inventory gets MORE urgent near the
    # close, never less -- but never at ANY price.  The completion quote
    # joins the touch only while touch <= breakeven - required_lock(age);
    # the requirement decays from +lock0 to -maxloss across bail_age,
    # after which the knives IOC.  This is the price ladder the July-28
    # whipsaw night demanded: patient first, tolerant later, never
    # machine-gunning drift losses.
    if abs(st.q) >= 1.0 and st.basis_c is not None:
        age = st.unpaired_age_s
        frac = min(1.0, age / max(p.bail_age_s, 1.0))
        required_lock = (p.ladder_lock0_c
                         - frac * (p.ladder_lock0_c + p.ladder_maxloss_c))
        red_side = "ask_no" if st.q > 0 else "bid"
        red_sz = float(int(min(p.clip, abs(st.q))))
        if red_side == "ask_no":
            touch_px = 100.0 - st.yes_ask_c        # NO-side join price
        else:
            touch_px = st.yes_bid_c
        cap_px = 100.0 - st.basis_c - required_lock
        if touch_px <= cap_px + 1e-9:
            if red_side == "bid":
                if out.bid_sz < red_sz:
                    out.bid_px_c, out.bid_sz = touch_px, red_sz
            else:
                if out.ask_sz < red_sz:
                    out.ask_px_c, out.ask_sz = touch_px, red_sz
        else:
            # The cap binds EVERY reduction quote, whatever path priced
            # it: completing above breakeven+tolerance is the machine-gun
            # drift loss this ladder exists to stop.
            if red_side == "bid" and out.bid_px_c is not None \
                    and out.bid_px_c > cap_px:
                out.bid_px_c, out.bid_sz = None, 0.0
            if red_side == "ask_no" and out.ask_px_c is not None \
                    and out.ask_px_c > cap_px:
                out.ask_px_c, out.ask_sz = None, 0.0
        out.diag["ladder"] = (round(required_lock, 2), round(touch_px, 1),
                              round(cap_px, 1))

    # ---- knives: completion / bail on held inventory ----
    if abs(st.q) > 1e-9 and st.unpaired_age_s > 0:
        if st.q > 0:      # long YES: exit by selling at the YES bid
            proceeds = st.yes_bid_c
            take_side, take_px = "ask_no", 100.0 - st.yes_bid_c
        else:             # long NO: exit by selling NO at the NO bid
            proceeds = 100.0 - st.yes_ask_c
            take_side, take_px = "bid", st.yes_ask_c
        fee = taker_fee_c(proceeds, p)
        # lock-take needs the basis, which the caller owns; the policy
        # only signals WHEN cutting is allowed -- the harness/engine
        # checks profitability for lock_take and cuts unconditionally
        # at bail age.
        if st.unpaired_age_s >= p.bail_age_s:
            out.take_side, out.take_px_c = take_side, take_px
            out.diag["take_reason"] = "bail"
        elif st.unpaired_age_s >= p.lock_take_age_s:
            out.take_side, out.take_px_c = take_side, take_px
            out.diag["take_reason"] = "lock_take_if_profitable"
            out.diag["take_fee_c"] = fee
    out.diag.update(dict(f_tte=round(f_tte, 3),
                         f_flow=(round(f_flow_bid, 3), round(f_flow_ask, 3)),
                         imb=round(imb, 3),
                         net=(None if bid_px is None else round(bid_net, 2),
                              None if ask_px is None else round(ask_net, 2)),
                         as_c=(round(as_bid, 2), round(as_ask, 2)),
                         spread_c=spread_c))
    return out
