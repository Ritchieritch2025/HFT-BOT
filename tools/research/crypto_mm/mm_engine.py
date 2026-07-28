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
import asyncio, base64, collections, copy, fcntl, glob, hashlib, json, math, os, sys, time, uuid
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# Resolve sibling modules from the deployed bundle, not from an unrelated
# copy in /home/ubuntu.  A canary receipt hashes this directory as one unit.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rti_pricing as rp
import mm_budget_adapter as mba
import mm_candidate_adapter as mca
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
# Deadman (hardcode-audit red line 2026-07-27): every live maker order
# carries an exchange-side expiration so an engine death cannot leave
# zombie quotes resting.  The engine renews healthy orders before expiry;
# 0 disables (must be explicit).  Engineering param, conservative default ON.
ORDER_TTL_S = float(os.environ.get("MM_ORDER_TTL_S", "90"))
# --- fast-anchor shield, A-stage (defence only: cancels, never prices).
# Reads the independent 24/7 anchor recorders' hourly NDJSON tails (same data
# plane trick as hydrate_rti_from_capture) so no new socket enters the hot
# path.  Binance perp led BRTI by 1s at corr 0.846 / beta 0.822 (2026-07-26
# census, n=2644); Coinbase reaches us ~57ms sooner (p50 14ms vs 71ms) and is
# itself an index constituent.  A quote is pulled when the predicted fair move
# against it exceeds its own remaining edge -- a parameterless rule.
ANCHOR_SHIELD = os.environ.get("MM_ANCHOR_SHIELD", "0") == "1"
ANCHOR_GLOB = os.environ.get(
    "MM_ANCHOR_GLOB", "/home/ubuntu/research_fast_anchor")
ANCHOR_BETA_BN = float(os.environ.get("MM_ANCHOR_BETA_BN", "0.82"))
ANCHOR_BETA_CB = float(os.environ.get("MM_ANCHOR_BETA_CB", "0.74"))
ANCHOR_MAX_AGE_S = float(os.environ.get("MM_ANCHOR_MAX_AGE_S", "5"))
ANCHOR_REFRESH_S = float(os.environ.get("MM_ANCHOR_REFRESH_S", "0.5"))
# Lag window for the unpriced-move estimate.  Source: E14 lead-lag measured
# Binance/Coinbase leading the Kalshi mid by ~1s; BRTI publication adds a
# fraction more.  dx = anchor(now) - anchor(now - LAG) is the move the
# fair value CANNOT have priced yet.
ANCHOR_LAG_S = float(os.environ.get("MM_ANCHOR_LAG_S", "1.5"))
# Same-side rapid-fire brake (operator speed directive 2026-07-27):
# two same-side fills inside BRAKE_N_S seconds -> pause that side
# BRAKE_HOLD_S (a sweep is eating the side we rest on; stop reloading).
BRAKE_N_S = float(os.environ.get("MM_BRAKE_N_S", "10"))
BRAKE_HOLD_S = float(os.environ.get("MM_BRAKE_HOLD_S", "30"))
# Approval #5 exit-loop surgery (2026-07-27).  EXIT_DELTA reuses the entry
# threshold family; the graveyard hard-flatten horizon comes from the A-S
# risk layer ruling (T<threshold & q!=0 -> unconditional close).
FLATTEN_TTE_S = float(os.environ.get("MM_FLATTEN_TTE_S", "60"))
TAKER_FEE_COEF_C = float(os.environ.get("MM_TAKER_FEE_COEF_C", "7.0"))
# --- transduction layer (operator ruling 2026-07-27: nothing fixed; every
# cent-denominated decision is a function of the contract's own volatility).
#
#   sigma_contract(tau) = |dP/dS| * sigma_S * sqrt(tau)      [cents]
#
# is what one tau-second move is worth IN THIS CONTRACT right now, so a
# single dimensionless knob prices the whole curve: a 5c contract and a 50c
# contract get thresholds proportional to their own risk instead of a shared
# absolute cent count.  MM_SIGMA_SCALE=0 restores the legacy fixed cents.
SIGMA_SCALE = os.environ.get("MM_SIGMA_SCALE", "1") == "1"
SIGMA_TAU_S = float(os.environ.get("MM_SIGMA_TAU_S", "30"))
MARGIN_M = float(os.environ.get("MM_MARGIN_M", "1.0"))     # entry, in sigma
GAMMA_K = float(os.environ.get("MM_GAMMA_K", "0.35"))      # inventory skew
MARGIN_FLOOR_C = float(os.environ.get("MM_MARGIN_FLOOR_C", "0.2"))
POS_MANAGE = os.environ.get("MM_POS_MANAGE", "1") == "1"
# Zone bands: the graveyard floor is settlement-truth; the rest are
# strategy choices that must be re-measurable under the current engine.
ZONE_GRAVEYARD_S = float(os.environ.get("MM_ZONE_GRAVEYARD_S", "120"))
ZONE_ALL_S = float(os.environ.get("MM_ZONE_ALL_S", "600"))
ZONE_MID_S = float(os.environ.get("MM_ZONE_MID_S", "300"))
ZONE_MID_EXCLUDE_C = float(os.environ.get("MM_ZONE_MID_EXCLUDE_C", "10"))
ZONE_TAIL_ONLY = os.environ.get("MM_ZONE_TAIL_ONLY", "1") == "1"
ZONE_TAIL_LOW_C = float(os.environ.get("MM_ZONE_TAIL_LOW_C", "20"))
ZONE_TAIL_HIGH_C = float(os.environ.get("MM_ZONE_TAIL_HIGH_C", "80"))
# Too-good-to-be-true guard.  A quote that fills far below our own fair is
# almost never a bargain: it means the index moved and our fair is the stale
# number.  Live 2026-07-27: bought YES at 36c against a 47c fair (11c
# "edge") mid-crash, then the complement could only be bought at 97.9c.
# Cap the admissible edge at MAX_EDGE_SIGMA sigma-units; 0 disables.
MAX_EDGE_SIGMA = float(os.environ.get("MM_MAX_EDGE_SIGMA", "2.0"))
# Bounded-loss maker exit for aged orphans.  The complement leg is normally
# pinned at the profit ceiling (99c - entry), so a lot that cannot pair just
# sits there.  Past ORPHAN_MAKER_AGE_S the ceiling relaxes by up to
# ORPHAN_MAKER_MAX_LOSS_C, still post-only and still fee-free -- a 1-3c
# maker exit is a fraction of the 12.6c the retired taker timer averaged,
# and unlike that timer it never crosses the spread.
ORPHAN_MAKER_AGE_S = float(os.environ.get("MM_ORPHAN_MAKER_AGE_S", "45"))
ORPHAN_MAKER_MAX_LOSS_C = float(
    os.environ.get("MM_ORPHAN_MAKER_MAX_LOSS_C", "3.0"))
# --- toxicity table (measured 2026-07-26, wired 2026-07-27): fills are not
# equal (markout -1.76c one side vs -0.12c the other; cheap-outcome buys
# clean, expensive-outcome buys -2..-4c).  The engine learns that surface
# online and prices it into the entry threshold per (side, price zone); a
# bucket that stops hurting decays back to zero.  Causal: a fill is scored
# only after TOX_HORIZON_S.  Off by default.
TOX_ENABLE = os.environ.get("MM_TOX", "0") == "1"
TOX_HORIZON_S = float(os.environ.get("MM_TOX_HORIZON_S", "30"))
TOX_HALFLIFE_FILLS = float(os.environ.get("MM_TOX_HALFLIFE_FILLS", "30"))
TOX_MIN_N = int(os.environ.get("MM_TOX_MIN_N", "20"))
TOX_MAX_ADDON_C = float(os.environ.get("MM_TOX_MAX_ADDON_C", "5"))
# Shrinkage strength: a bucket needs ~TOX_PRIOR_N fills before it speaks
# mostly for itself instead of borrowing its side-pooled mean.
TOX_PRIOR_N = float(os.environ.get("MM_TOX_PRIOR_N", "25"))
# The surcharge may never dwarf the volatility it is pricing.
TOX_MAX_SIGMA_FRAC = float(os.environ.get("MM_TOX_MAX_SIGMA_FRAC", "0.5"))
# Warm-start prior from the public tape (tape_shape_study.py: 3.0M scored
# trades, 56 buckets, se 0.02-0.4c).  Path empty = cold start as before.
TOX_PRIOR_FILE = os.environ.get("MM_TOX_PRIOR_FILE", "")
# Effective sample weight given to the prior.  Deliberately far below the
# study's real n (up to 337k): the prior should dominate an empty table but
# still yield to a few hundred live observations if the live market
# disagrees with history.
TOX_PRIOR_EFF_N = float(os.environ.get("MM_TOX_PRIOR_EFF_N", "100"))
# Approval #1: peers allowed to share this API key's market-data quota.
MAX_PEER_ENGINES = int(os.environ.get("MM_MAX_PEER_ENGINES", "0"))
# Tail doctrine (operator 2026-07-26): in the tails, buying the CHEAP
# outcome is long convexity with loss capped at the entry price, while
# selling it is writing tail insurance into a jump process measured at
# 224x the Gaussian rate.  Only the cheap side is admitted inside the
# tail bands; 0 restores symmetric quoting.
TAIL_CHEAP_ONLY = os.environ.get("MM_TAIL_CHEAP_ONLY", "0") == "1"
# Tail bands are two independent edges (the book is not symmetric) plus the
# price above which an outcome stops being "cheap".  Restored 2026-07-27
# after a truncated edit left them referenced-but-undefined.
TAIL_LOW_C = float(os.environ.get("MM_TAIL_LOW_C", "20"))
TAIL_HIGH_C = float(os.environ.get("MM_TAIL_HIGH_C", "80"))
TAIL_CHEAP_MAX_C = float(os.environ.get("MM_TAIL_CHEAP_MAX_C", "50"))
# Pairing closed loop (audit 2026-07-26 must-list; operator 吃差价 pivot).
# OFF by default until the grid_replay_v2 pair-arm verdict; flip with
# MM_PAIR=1 for shadow evidence runs.
PAIR = os.environ.get("MM_PAIR", "0") == "1"
PAIR_LOCK_C = float(os.environ.get("MM_PAIR_LOCK_C", "99"))  # max pair cost
# A pair is not worth creating merely because it is <=99c.  Keep a
# configurable net edge buffer so a 98.9c pair is rejected by default rather
# than tying up capital for a microscopic gross edge.
PAIR_MIN_EDGE_C = float(os.environ.get("MM_PAIR_MIN_EDGE_C", "0.5"))
UNPAIRED_MAX_CT = float(os.environ.get("MM_UNPAIRED_MAX_CT", "2"))  # 1 clip
# Three-knife surgery (operator 修复令 2026-07-28, after the overnight
# whipsaw: pair locks +$9.67 vs orphan-settlement tax ~-$13):
#   knife 1: an unpaired lot whose opposite best ALREADY pays basis+fee+
#            LOCK_TAKE_MIN_C is free money -- IOC it at the touch instead
#            of waiting as a maker (87% of exit intents never filled).
#   knife 2: MM_UNPAIRED_AGE_S re-armed (120s) -- the existing timed IOC
#            cut, disabled at 100000 since the speed directive.
#   knife 3: no NEW first legs inside ENTRY_CUTOFF_TTE_S of close; a leg
#            born there cannot be paired in time.  Exits untouched.
ENTRY_CUTOFF_TTE_S = float(os.environ.get("MM_ENTRY_CUTOFF_TTE_S", "240"))
LOCK_TAKE_AGE_S = float(os.environ.get("MM_LOCK_TAKE_AGE_S", "20"))
LOCK_TAKE_MIN_C = float(os.environ.get("MM_LOCK_TAKE_MIN_C", "0.3"))
UNPAIRED_AGE_S = float(os.environ.get("MM_UNPAIRED_AGE_S", "90"))
# Pair-cycle admission.  Unlike the legacy per-leg kernel selector, this
# stages BOTH complementary maker orders before either fills.  It stays off
# by default and is shadow-only until a sequential replay plus an independent
# validation day clear promotion.
PAIR_PREQUOTE = os.environ.get("MM_PAIR_PREQUOTE", "0") == "1"
PAIR_MIN_DEPTH_CT = float(
    os.environ.get("MM_PAIR_MIN_DEPTH_CT", "1081.01"))
SHADOW_QUEUE_SIM = os.environ.get("MM_SHADOW_QUEUE_SIM", "0") == "1"
ONLY_TICKER = os.environ.get("MM_ONLY_TICKER", "").strip()
MAX_CYCLES = int(os.environ.get("MM_MAX_CYCLES", "0"))
REQUIRE_FLAT_START = os.environ.get("MM_REQUIRE_FLAT_START", "0") == "1"
GAMMA_C = float(os.environ.get("MM_GAMMA", "0.5"))  # F1 skew retreat, cents/contract
MIN_REQUOTE_C = float(os.environ.get("MM_MIN_REQUOTE", "0.5"))  # F5, cents:
    # hold a WANTED resting order unless the target moved at least this
    # far (D7: 18,854 rate blocks vs 232 orders from 0.1c-move requoting).
    # Risk-off cancels (side no longer wanted) bypass the threshold.
                                                    # derivation: docs/research_reports/
                                                    # MM_SKEW_GAMMA_DERIVATION.md
MIN_QUOTE_AGE_S = float(os.environ.get("MM_MIN_QUOTE_AGE_S", "15"))
MAX_OPEN_COST = float(os.environ.get("MM_MAX_COST", "50"))
# Pair completion is risk-reducing, but it still consumes collateral until the
# venue releases it.  Reserve only a fraction of the normal open-cost budget
# for pair completion; the live control can make this smaller.
PAIR_MAX_LOCKED_COST = float(os.environ.get(
    "MM_PAIR_MAX_LOCKED_COST", str(max(1.0, 0.25 * MAX_OPEN_COST))))
MAX_NET = int(os.environ.get("MM_MAX_NET", "6"))
HARD_MAX_OPEN_COST = float(os.environ.get("MM_HARD_MAX_COST", "200"))
HARD_MAX_CLIP = os.environ.get("MM_HARD_MAX_CLIP", "20.00")
HARD_MAX_NET = int(os.environ.get("MM_HARD_MAX_NET", "100"))
KILL_LOSS = float(os.environ.get("MM_KILL_LOSS", "-50"))
# Strategy-line tag prefixed onto every client_order_id so portfolio fills
# reconcile to exactly one line; an unprefixed/unknown fill is an incident
# (see _coord mailbox 20260727T0145 gate 1/3).
LINE_TAG = os.environ.get("MM_LINE_TAG", "C15")
RTI_SHORT_WINDOW_S = float(os.environ.get("MM_RTI_SHORT_WINDOW_S", "60"))
RTI_LONG_WINDOW_S = float(os.environ.get("MM_RTI_LONG_WINDOW_S", "300"))
RTI_MIN_COVERAGE = float(os.environ.get("MM_RTI_MIN_COVERAGE", "0.999"))
RTI_MAX_GAP_S = float(os.environ.get("MM_RTI_MAX_GAP_S", "1.5"))
RTI_SEAM_MAX_ATTEMPTS = int(os.environ.get("MM_RTI_SEAM_MAX_ATTEMPTS", "20"))
RTI_MAX_SOURCE_AGE_S = float(
    os.environ.get("MM_RTI_MAX_SOURCE_AGE_S", "2.0"))
RTI_HISTORY_TICKS = max(
    int(os.environ.get("MM_RTI_HISTORY_TICKS", "1200")),
    int(math.ceil(RTI_LONG_WINDOW_S)) + 2,
)
CF_CAPTURE_GLOB = os.environ.get(
    "MM_CF_CAPTURE_GLOB",
    "/home/ubuntu/hft-bot/work/live/cfbenchmarks/date=*/cfb_*.ndjson",
)
OUT = Path(os.environ.get("MM_OUT", "/home/ubuntu/h6b_inputs/mm_engine"))
OUT.mkdir(parents=True, exist_ok=True)
CTRL_PATH = Path(os.environ.get(
    "MM_CTRL", "/home/ubuntu/h6b_inputs/mm_control.json"))
STATUS_PATH = Path(os.environ.get(
    "MM_STATUS", "/home/ubuntu/h6b_inputs/mm_control_status.json"))
# Absolute cross-restart loss budget.  These values are intentionally not
# runtime controls: changing the anchor/floor creates a different budget and
# requires a separate operator initialization, never an engine restart.
# Re-anchored 2026-07-27T22 per operator speed directive (full account for
# testing, floor $2.00 as the control ration): equity at re-anchor $17.2794.
BUDGET_ANCHOR_MICRO_USD = 17_279_400       # $17.279400
BUDGET_MAX_LOSS_MICRO_USD = 15_279_400     # $15.279400 (operator 2026-07-27)
BUDGET_FLOOR_MICRO_USD = 2_000_000         # $2.000000 control ration
BUDGET_PATH = Path(os.environ.get(
    "MM_BUDGET_PATH", "/home/ubuntu/h6b_inputs/mm_budget.json"))
BUDGET_SUBACCOUNT = 0
BUDGET_EXCHANGE_INDEX = 0
BUDGET_MONITOR_S = 1.0
# Live ignition stays disabled until a read-only three-state account probe has
# proved how portfolio_value and resting-order collateral compose with
# available balance for this exact API/account contract.
BUDGET_ACCOUNTING_CONTRACT_VERIFIED = (
    os.environ.get("MM_BUDGET_ACCOUNTING_CONTRACT_VERIFIED", "0") == "1"
)
# Frozen at import so tests that temporarily patch MODE do not accidentally
# opt into live-only disk/network requirements.  A real MM_MODE=live process
# cannot route without completing budget ignition.
BUDGET_ENFORCE = MODE == "live"

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
    # Kalshi signs ts+method+PATH with the query string EXCLUDED; signing
    # the full path 401s every parameterized GET (found by the fills
    # selfcheck at live ignition 2026-07-26 — D1's second half).
    req = urllib.request.Request(host + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers=_sig(method, path.split("?")[0]))
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
    # fast-anchor shield state (A-stage): newest feed snapshot, the snapshot
    # latched when the current fair was computed, and refresh throttle.
    anchor = {"bn": None, "cb": None}
    anchor_mark = {}
    anchor_mark_src = {}
    anchor_last_refresh = 0.0
    rti = {s: collections.deque(maxlen=RTI_HISTORY_TICKS) for s in SERIES}
    rti_src_ms = {
        s: collections.deque(maxlen=RTI_HISTORY_TICKS) for s in SERIES
    }
    rti_t = {s: 0.0 for s in SERIES}
    # Official raw-tick [close-60s, close) accumulator, keyed to its exact
    # quarter-hour close: {sum, n, close_ms, source_ms, avg}.  The exchange's
    # similarly named last_60 field is shifted and is validation-only.
    rti_lock = {s: None for s in SERIES}
    cf_sid = None
    cf_seq = None
    cf_stream_ok = False
    # Hydrate-to-live seam repair (2026-07-28 trip fix): a >0.3s hole at the
    # splice point fails the 0.999-coverage sigma gate for the full 300s
    # window -- measured 300.8s of zero quoting after every restart.  The
    # recorder kept the missing ticks; splice them in instead of waiting.
    rti_seam_from_ms = {}      # ser -> last hydrated source_ms (None=closed)
    rti_seam_attempts = {}     # ser -> bridge attempts so far
    recon_suspect = set()      # markets diverging on the LAST recon pass
    last_budget_trip_err = ""  # dedupe key for repeated blind-trip spam
    last_budget_trip_log = 0.0
    budget_snapshot_fails = 0  # consecutive 1Hz monitor snapshot failures
    pricing_unready = {}       # ser -> reason pricing_state last returned None
    last_pricing_unready = {}  # mt -> last PRICING_UNREADY receipt wall time
    cf_session_ticks = {
        s: collections.deque(maxlen=61) for s in SERIES
    }
    # Public taker flow used to estimate whether BOTH maker legs can finish.
    # Tuple: (source_ms, maker_side_hit, yes_price_dollars, contracts).
    public_trades = collections.defaultdict(
        lambda: collections.deque(maxlen=20_000))
    public_trade_ids = set()
    public_trade_id_fifo = collections.deque(maxlen=50_000)
    meta = {}                  # mt -> (series, close_s, strike)
    books = {}
    orders = {}                # (mt, side) -> {id, px, t}
    # POST is economically live before its HTTP response is trustworthy.
    # Reserve it first so a timeout can never disappear from the risk book.
    pending_new = {}           # (mt, side) -> pre-ACK reservation
    place_not_before = {}      # (mt, side) -> retry throttle after reject
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
    fills_selfcheck_ok = False   # F3 startup probe result (live gate)
    contract_selfcheck_ok = False  # incident-#3 schema gate (live gate)
    flat_start_ok = False
    last_recon_ok_mono = None    # F2 last successful recon (live gate)
    session_start_ts = time.time()  # F4 scope: only THIS session's settles
    settle_zero_streak = 0  # F4 consecutive zero-revenue settlements
    settled_seen = set()    # F4 dedupe of applied settlements
    # mt -> side -> [(px, ts, qty, allocated_fee_dollars)]
    unpaired = {}
    pair_locked_by_market = collections.defaultdict(float)
    pair_locked_total = 0.0
    cycle_active = set()
    completed_cycles = 0
    canary_done = False
    flatten_sent = {}       # (mt, side) -> last IOC send ts (cooldown)
    flatten_pending = {}    # orphan (mt, side) -> unresolved IOC metadata
    unknown_orders = {}     # order_id -> terminal state still unproven
    requote_suppressed = 0  # F5 sub-threshold holds (observability)
    fills_seen = set()      # fill dedupe across WS channel + REST poll
    filled_by_order = collections.defaultdict(float)
    last_eval = {}          # mt -> last QUOTE_EVAL wall time (1/s throttle)
    last_pos_log = {}       # mt -> last POSITION_REVAL wall time
    last_pos_action = {}    # mt -> last logged reval action
    user_data_as_of_max = None   # monotonic high-water (race #6)
    last_boot_skip = {}     # mt -> last BOOT_SKIP receipt
    exit_intent = {}        # mt -> {side, target_c, action, ts}
    sigma_hist = collections.deque(maxlen=600)  # slow-sigma ref
    gate_counts = {}        # attribution: which gate fired
    unpaused_mono = None    # when live trading last became active
    evals_since_unpause = 0
    last_eval_mono = None
    want_unmet = {}         # (mt, side) -> first mono ts want went unserved
    survival_alarms = 0
    block_reasons = {}      # reason -> count (approval #1)
    side_evals = 0
    side_wants = 0
    order_tombstones = {}   # oid -> mono ts of local release
    recent_fill_mono = {}   # mt -> mono ts of latest local fill
    side_fill_times = {}    # (mt, side) -> [mono ts]
    side_brake = {}         # (mt, side) -> blocked-until mono
    anchor_hist = {"bn": collections.deque(maxlen=120),
                   "cb": collections.deque(maxlen=120)}
    tox = {}                # bucket -> {ewma, n}
    tox_pending = []        # fills awaiting markout horizon
    last_fair_c = {}        # mt -> newest kernel fair
    last_eval_state = {}    # mt -> snapshot for FILL context
    last_entry_source_ms = {}  # entry decisions run once per BRTI source tick
    last_sentinel_log = {}  # mt -> last SENTINEL_PAUSE receipt
    last_control_cancel = 0.0
    control_error = ""
    # Generation-stable absolute-budget integration.  Every economically
    # relevant local reservation/position mutation advances the generation.
    risk_generation = 0
    budget_guard = None
    budget_startup_ok = False
    budget_previous_updated_ts = None
    budget_previous_user_data_as_of_s = None
    budget_remote_orders = []
    live_writer_lock_fd = None
    live_writer_lock_path = None


def risk_generation_bump():
    """Advance the local risk epoch after one economic state transition."""
    if type(S.risk_generation) is not int or S.risk_generation < 0:
        # A corrupt generation is itself unsafe.  Restore monotonic typing,
        # then let the next generation-stable assessment fail closed if it
        # overlapped this repair.
        S.risk_generation = 0
    S.risk_generation += 1
    return S.risk_generation


def risk_map_set(mapping, key, value):
    mapping[key] = value
    risk_generation_bump()
    return value


def risk_map_pop(mapping, key):
    if key not in mapping:
        return None
    value = mapping.pop(key)
    risk_generation_bump()
    return value


def risk_map_touch(mapping, key):
    """Mark an in-place quantity/economics mutation in a tracked map."""
    if key not in mapping:
        raise KeyError(key)
    risk_generation_bump()


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


def peer_engine_pids():
    """Other mm_engine.py processes on this host.

    Approval #1 (2026-07-27): every engine process opens Kalshi market-data
    websockets with the SAME API key.  The 08:00 incident: six shadow
    instances plus live made seven connections and the live engine -- the
    last to connect -- was silently starved (zero QUOTE_EVAL while shadows
    evaluated 5,494/hour).  Live therefore refuses to START while peers
    hold connections; MM_MAX_PEER_ENGINES (default 0, source: that
    incident) permits a documented number of peers for deliberate
    live+shadow layouts.
    """
    peers = []
    me = os.getpid()
    for pdir in glob.glob("/proc/[0-9]*"):
        try:
            pid = int(pdir.rsplit("/", 1)[1])
            if pid == me:
                continue
            cmd = open(pdir + "/cmdline", "rb").read().decode(
                errors="ignore")
        except (OSError, ValueError):
            continue
        # A peer is a RUNNING ENGINE, not any process that mentions the
        # file: the reset script's sanity `py_compile mm_engine.py` was
        # detected as a peer and halted the very engine it had just
        # started (observed 2026-07-27T22, twice).
        if ("mm_engine.py" in cmd and "py_compile" not in cmd
                and "pytest" not in cmd and "grep" not in cmd):
            peers.append(pid)
    return peers


def credential_exclusivity_violation():
    """(violated, peers).  Only meaningful for live mode at startup."""
    peers = peer_engine_pids()
    return len(peers) > MAX_PEER_ENGINES, peers


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
            "pending_new": len(S.pending_new),
            "exposure": round(exposure(), 6),
            "realized": round(S.realized, 6),
            "risk_generation": S.risk_generation,
            "budget_enforced": BUDGET_ENFORCE,
            "budget_accounting_contract_verified":
                BUDGET_ACCOUNTING_CONTRACT_VERIFIED,
            "budget_startup_ok": S.budget_startup_ok,
            "budget_latched": bool(
                S.budget_guard is not None
                and S.budget_guard.record.latched
            ),
            "budget_floor_micro_usd": BUDGET_FLOOR_MICRO_USD,
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
        # Evidence-based ignition interlock: unpausing live requires the
        # fills-visibility self-check to have PASSED and a reconciliation
        # success within the last 30s IN THIS PROCESS.  Both pipelines
        # were dead the whole session on 2026-07-25.
        recon_age = (None if S.last_recon_ok_mono is None
                     else time.monotonic() - S.last_recon_ok_mono)
        budget_ready = (
            not BUDGET_ENFORCE
            or (
                S.budget_guard is not None
                and S.budget_startup_ok
                and not S.budget_guard.record.latched
            )
        )
        if (not S.fills_selfcheck_ok or not S.contract_selfcheck_ok
                or (REQUIRE_FLAT_START and not S.flat_start_ok)
                or not budget_ready
                or recon_age is None or recon_age > 30.0):
            # STABLE text only: this message is the reject-dedupe key, and
            # embedding the live recon_age made every rejection "new",
            # re-running cancel_all 4x/second (2026-07-28T02:48 tape).
            raise ControlError(
                "live resume disabled: requires fills+contract selfchecks "
                "PASS and recon success <30s old "
                f"(fills={S.fills_selfcheck_ok}, "
                f"contract={S.contract_selfcheck_ok}, "
                f"flat_start={S.flat_start_ok}, "
                f"budget_ready={budget_ready}, "
                f"recon_fresh={recon_age is not None and recon_age <= 30.0})")
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
    # Deadlock guard (2026-07-28T04:05 tape): clip was raised to 3 while
    # UNPAIRED_MAX_CT stayed 2 ("1 clip" doctrine), so EVERY entry failed
    # the unpaired cap -- 1215 wants, 0 orders, in silence.  The mismatch
    # must scream.
    try:
        if float(CLIP) > UNPAIRED_MAX_CT + 1e-9:
            L.w({"ev": "CONFIG_WARN",
                 "why": "clip exceeds unpaired cap: every entry will be "
                        "blocked by UNPAIRED_CAP_SKIP",
                 "clip": str(CLIP), "unpaired_max_ct": UNPAIRED_MAX_CT})
    except (TypeError, ValueError):
        pass
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
    pending = sum(
        od["px"] * float(od["qty"]) for od in S.pending_new.values()
    )
    filled = sum(v["cost"] for v in S.net_pos.values())
    return resting + pending + filled


def pair_capital_limit():
    """Maximum total open collateral allowed while completing a pair."""
    return min(float(MAX_OPEN_COST), float(PAIR_MAX_LOCKED_COST))


def pair_exit_within_cap(mt, side, px, qty):
    """Check replacement capital without double-counting the old exit order."""
    old = S.orders.get((mt, side))
    old_cost = 0.0 if old is None else float(old.get("px", 0.0)) * float(
        old.get("qty", 0.0))
    return (exposure() - old_cost + float(px) * float(qty)
            <= pair_capital_limit() + 1e-9)


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


def taker_fee_usd(price, count):
    """July-2026 general taker fee, including centicent round-up.

    Kalshi rounds upward so ``position cost + fee`` lands on $0.0001.
    Decimal arithmetic keeps shadow losses and budget reservations
    conservative for fractional contract quantities.
    """
    p = Decimal(str(price))
    c = Decimal(str(count))
    if (not p.is_finite() or not c.is_finite()
            or p <= 0 or p >= 1 or c <= 0):
        raise ValueError("invalid taker fee inputs")
    position_cost = p * c
    raw_fee = Decimal("0.07") * c * p * (Decimal(1) - p)
    rounded_total = (position_cost + raw_fee).quantize(
        Decimal("0.0001"), rounding=ROUND_CEILING)
    return float(rounded_total - position_cost)


def sigma_contract_c(ps, tau_s=None):
    """One tau-second move expressed in THIS contract's cents.

    dP/dS converts an index dollar into contract cents; sigma_S*sqrt(tau)
    is the index move over tau.  Returns None when pricing is unavailable,
    which callers must treat as "fall back to the fixed-cent legacy".
    """
    if not ps:
        return None
    dpds = ps.get("dp_ds_c_per_usd")
    sigma_s = ps.get("sigma")
    if dpds is None or sigma_s is None or sigma_s <= 0:
        return None
    tau = SIGMA_TAU_S if tau_s is None else tau_s
    return abs(dpds) * sigma_s * math.sqrt(max(tau, 0.0))


def margin_c(ps):
    """Entry threshold in cents: MARGIN_M sigma-units, floored.

    With MM_SIGMA_SCALE=0 (or no pricing) this is the legacy fixed cents,
    so the transduction can be switched off without touching call sites."""
    if not SIGMA_SCALE:
        return MARGIN_C
    sc = sigma_contract_c(ps)
    if sc is None:
        return MARGIN_C
    return max(MARGIN_FLOOR_C, MARGIN_M * sc)


def sigma_c_slow(ps):
    """Contract sigma from a SLOW volatility reference.

    Deliberately not the live sigma.  A crash raises sigma, so any ceiling
    anchored on the current value widens exactly when it should tighten --
    the 36c fill on 2026-07-27 happened in a minute when live sigma was
    rising, so a live-sigma cap would have been at its loosest right then.
    Uses the trailing median of recent sigma observations instead, which
    barely moves inside one crash.
    """
    dpds = (ps or {}).get("dp_ds_c_per_usd")
    if dpds is None:
        return None
    hist = S.sigma_hist
    if not hist:
        return None
    ordered = sorted(hist)
    med = ordered[len(ordered) // 2]
    if med <= 0:
        return None
    return abs(dpds) * med * math.sqrt(max(SIGMA_TAU_S, 0.0))


def edge_within_cap(edge_c, ps):
    """False when an apparent edge is TOO large to be real.

    Symmetric to the entry threshold: an edge below margin is not worth
    taking, and an edge far above what this contract can plausibly misprice
    means OUR fair is the stale number (the index moved and the kernel has
    not seen it).  The floor uses live sigma (pricing must react); the
    CEILING uses slow sigma (a crash must not buy permission to trade the
    very dislocation it creates).
    """
    if MAX_EDGE_SIGMA <= 0 or edge_c is None:
        return True
    sc = sigma_c_slow(ps)
    if sc is None:
        sc = sigma_contract_c(ps)          # cold start: better than nothing
    if sc is None or sc <= 0:
        return True
    within = float(edge_c) <= MAX_EDGE_SIGMA * sc
    if not within:
        S.gate_counts["edge_cap"] = S.gate_counts.get("edge_cap", 0) + 1
    return within


def gamma_c_dyn(ps):
    """Inventory retreat per contract, in sigma-units (GLFT shape: the
    penalty for holding scales with what the position can move, not with
    a constant)."""
    if not SIGMA_SCALE:
        return GAMMA_C
    sc = sigma_contract_c(ps)
    if sc is None:
        return GAMMA_C
    return max(0.0, GAMMA_K * sc)


def skew_px(px, adverse_ct, gamma=None):
    """F1 inventory skew: retreat the ACCUMULATING side GAMMA_C cents per
    contract of net inventory, snapped down to a legal tick.  The
    offsetting side (adverse_ct <= 0) is never touched — it reduces
    inventory and stays at the baseline quote."""
    if adverse_ct <= 0:
        return px
    g = GAMMA_C if gamma is None else gamma
    return legal_floor(px - g * adverse_ct / 100.0)

# ---------------------------------------------------------- pairing loop
def unpaired_ct(mt, side):
    lots = S.unpaired.get(mt)
    return sum(lot[2] for lot in lots[side]) if lots else 0.0


def inventory_worst_loss(mt, *, add_side=None, add_qty=0.0,
                         add_cost=0.0):
    """Exact two-outcome terminal loss for current residual inventory.

    Pair-mode ``net_pos`` contains only unmatched economic lots because
    ``pair_off`` removes a YES/NO pair as soon as it locks.  This makes the
    worst loss simply total basis minus the smaller terminal payout.
    """
    d = S.net_pos.get(mt, {"y": 0.0, "n": 0.0, "cost": 0.0})
    y, n, cost = float(d["y"]), float(d["n"]), float(d["cost"])
    if add_side == "bid":
        y += float(add_qty)
    elif add_side == "ask_no":
        n += float(add_qty)
    cost += float(add_cost)
    return max(0.0, cost - min(y, n))


def tox_zone(entry_c):
    for hi, name in ((10, "0-10"), (20, "10-20"), (35, "20-35"),
                     (65, "35-65"), (80, "65-80"), (90, "80-90")):
        if entry_c < hi:
            return name
    return "90-100"


def tox_gamma_band(sigma_c):
    """Fixed sigma_c bands, cents.  Band strings match the tape study keys
    exactly so priors load without translation."""
    if sigma_c is None:
        return "gna"
    if sigma_c < 0.5:
        return "g1(<0.5c)"
    if sigma_c < 1.5:
        return "g2(0.5-1.5)"
    if sigma_c < 3.0:
        return "g3(1.5-3)"
    if sigma_c < 6.0:
        return "g4(3-6)"
    return "g5(>6c)"


def tox_bucket(side, entry_c, sigma_c=None):
    """Toxicity bucket key: direction x price zone x sigma_c band.

    Third dimension history: tte replaced by sigma_c on 2026-07-27 after
    the gamma study (3.0M public trades) showed toxicity is MONOTONE in
    sigma_c within every zone and the direction FLIPS by zone -- cheap-side
    buyers turn toxic as sigma_c rises (sniper profit = information lead x
    dP/dS needs powder), expensive-side buyers turn toxic as sigma_c FALLS
    (informed accumulate in calm; panic chases favorites in chaos).  tte
    bands were a coarse shadow of this: sigma_c already contains tte and
    volatility in one number the engine computes every tick.
    """
    if entry_c is None:
        return None
    return f"{side}|{tox_zone(float(entry_c))}|{tox_gamma_band(sigma_c)}"


_TOX_MIRROR = {"0-10": "90-100", "10-20": "80-90", "20-35": "65-80",
               "35-65": "35-65", "65-80": "20-35", "80-90": "10-20",
               "90-100": "0-10"}


def tox_load_prior(path):
    """Preload S.tox from the public-tape study.

    Mapping is a MIRROR, not an identity: the study keys by the TAKER's
    side and price, while the engine keys by OUR entry.  When we rest a
    YES bid at px, the counterparty who hits it is a taker buying NO at
    (100-px) -- so engine bucket (bid, zone(px), tte) inherits study
    bucket (no, zone(100-px), tte).  Signs already agree: the study's
    maker_markout is measured from the maker's chair, which is ours.

    Returns the number of buckets loaded; never raises (a broken prior
    file must not stop the engine -- it just cold-starts).
    """
    try:
        prior = json.load(open(path))
    except Exception as exc:
        L.w({"ev": "TOX_PRIOR_ERR", "err": repr(exc)[:120]})
        return 0
    loaded = 0
    for our_side, taker_side in (("bid", "no"), ("ask_no", "yes")):
        for our_zone, taker_zone in _TOX_MIRROR.items():
            for tteb in ("g1(<0.5c)", "g2(0.5-1.5)",
                         "g3(1.5-3)", "g4(3-6)", "g5(>6c)"):
                row = prior.get(f"{taker_side}|{taker_zone}|{tteb}")
                if not row:
                    continue
                n_eff = min(float(row.get("n", 0)), TOX_PRIOR_EFF_N)
                if n_eff <= 0:
                    continue
                se = float(row.get("se", 0.0))
                var = (se * se) * float(row.get("n", 1))
                S.tox[f"{our_side}|{our_zone}|{tteb}"] = {
                    "ewma": float(row["maker_markout_c"]),
                    "ewvar": var, "n": n_eff}
                loaded += 1
    L.w({"ev": "TOX_PRIOR_LOADED", "path": path, "buckets": loaded,
         "eff_n": TOX_PRIOR_EFF_N})
    return loaded


def tox_observe(bucket, markout_c):
    """Fold one realised markout into the bucket, tracking DISPERSION too.

    Causal by construction: a markout is only knowable TOX_HORIZON_S after
    the fill.  We keep an EWMA of the level and an EWMA of the squared
    deviation, because the surcharge must shrink when the evidence is
    noisy -- a mean of -3c built from wildly scattered outcomes is much
    weaker evidence than the same mean from tight ones.
    """
    if not TOX_ENABLE or bucket is None or markout_c is None:
        return
    st = S.tox.setdefault(bucket, {"ewma": 0.0, "ewvar": 0.0, "n": 0})
    alpha = 1.0 - 0.5 ** (1.0 / max(TOX_HALFLIFE_FILLS, 1.0))
    x = float(markout_c)
    prev = st["ewma"]
    st["ewma"] = prev + alpha * (x - prev)
    st["ewvar"] += alpha * ((x - prev) * (x - st["ewma"]) - st["ewvar"])
    st["n"] += 1


def _tox_pooled(side):
    """(mean, var, n) pooled over one side, then globally.  A thin bucket
    borrows strength from its side rather than inventing a number."""
    for keyfn in (lambda k: k.startswith(f"{side}|"), lambda k: True):
        rows = [v for k, v in S.tox.items() if keyfn(k) and v["n"] > 0]
        n = sum(r["n"] for r in rows)
        if n > 0:
            mean = sum(r["ewma"] * r["n"] for r in rows) / n
            var = sum(max(r["ewvar"], 0.0) * r["n"] for r in rows) / n
            return mean, var, n
    return 0.0, 0.0, 0


def tox_addon_c(side, quote_c, sigma_c=None, redundancy=0.0):
    """Expected adverse cost of THIS kind of fill, shrunk by how much we
    actually know, net of what other signals already charge for.

    Not a lookup table.  Four state-dependent factors, all continuous:

      level      the bucket's EWMA markout, shrunk toward its side-pooled
                 mean with weight n/(n+TOX_PRIOR_N) -- a 2-fill bucket is
                 mostly prior, a 200-fill bucket is mostly itself;
      certainty  scaled by |mean| / (|mean| + stderr): a mean that is small
                 relative to its own scatter earns almost no surcharge;
      redundancy the caller passes what sigma/anchor/flow already price in;
                 the table only charges for the residual it explains alone,
                 so the same risk is never billed twice;
      cap        never more than TOX_MAX_ADDON_C, and never more than
                 TOX_MAX_SIGMA_FRAC of the contract's own sigma when known
                 -- the surcharge cannot dwarf the risk it is pricing.

    Returns 0.0 whenever the evidence does not support a number.
    """
    if not TOX_ENABLE:
        return 0.0
    bucket = tox_bucket(side, quote_c, sigma_c)
    st = S.tox.get(bucket) or {"ewma": 0.0, "ewvar": 0.0, "n": 0}
    p_mean, p_var, p_n = _tox_pooled(side)
    n = st["n"]
    if n + p_n <= 0:
        return 0.0
    # 1. shrink the bucket toward its pooled mean by sample weight
    w = n / (n + max(TOX_PRIOR_N, 1e-9))
    mean = w * st["ewma"] + (1.0 - w) * p_mean
    # 2. certainty factor from the standard error of that mean
    var = max(st["ewvar"] if n > 1 else p_var, 0.0)
    stderr = math.sqrt(var / max(n, 1)) if var > 0 else 0.0
    if mean >= 0.0:
        return 0.0                      # profitable buckets buy no discount
    loss = -mean
    certainty = loss / (loss + stderr) if (loss + stderr) > 0 else 0.0
    raw = loss * certainty
    # 3. subtract what other signals already charge for the same risk
    net = max(0.0, raw - max(0.0, float(redundancy)))
    # 4. caps: absolute, and relative to the contract's own volatility
    cap = TOX_MAX_ADDON_C
    if sigma_c is not None and sigma_c > 0:
        cap = min(cap, TOX_MAX_SIGMA_FRAC * float(sigma_c))
    return min(net, cap)


def tox_settle_pending(now_s=None):
    """Realise markouts whose horizon has elapsed.

    Each fill parks (bucket, entry price, fair-at-fill) and is scored once
    the horizon passes, using the CURRENT fair for that market.  Nothing is
    scored early, so the table can never peek at its own future.
    """
    if not TOX_ENABLE or not S.tox_pending:
        return
    now_s = time.time() if now_s is None else now_s
    keep = []
    for rec in S.tox_pending:
        if now_s - rec["t"] < TOX_HORIZON_S:
            keep.append(rec)
            continue
        fair_now = S.last_fair_c.get(rec["mt"])
        if fair_now is None:
            continue                      # market gone dark: drop, never guess
        mark = (fair_now if rec["side"] == "bid" else 100.0 - fair_now)
        mo = mark - rec["entry_c"]
        tox_observe(rec["bucket"], mo)
        L.w({"ev": "TOX_OBSERVE", "mt": rec["mt"], "bucket": rec["bucket"],
             "entry_c": round(rec["entry_c"], 3),
             "mark_c": round(mark, 3), "markout_c": round(mo, 3),
             "horizon_s": TOX_HORIZON_S})
    S.tox_pending = keep


def position_reval(mt, fair_c, ps, best_bid_c=None):
    """Re-price residual inventory against the CURRENT fair, each event.

    WHY THERE IS NO TAKE-PROFIT / STOP-LOSS HERE (operator proof
    2026-07-27).  p is a martingale, so for a driftless walk the
    probability of touching +a before -b is exactly b/(a+b) and

        EV = b/(a+b)*a - a/(a+b)*b = 0

    identically, for every choice of a and b.  The hit probability
    compensates the payoff by construction, so any take/stop pair is a
    zero-EV bet that then pays the spread and any taker fee -- which is
    the closed-form explanation for the twoleg family's -1.2..-1.9c.
    Cost basis is sunk and invisible to the market; it must never appear
    in a decision.

    So exiting uses the SAME edge test as entering, only mirrored:
        enter when  market < fair - threshold
        exit  when  market > fair + threshold
    Both are +EV actions against the same fair.  The remaining legitimate
    reason to close is capital: the lot is occupying the budget needed for
    new business -- that lever belongs to the capital layer, not to sigma.
    """
    d = S.net_pos.get(mt)
    if not d:
        return None
    y, n, cost = float(d["y"]), float(d["n"]), float(d["cost"])
    resid = y - n
    if abs(resid) < 1e-9 or fair_c is None:
        return None
    side = "bid" if resid > 0 else "ask_no"
    qty = abs(resid)
    # Marked at fair, in this leg's own orientation.
    mark_c = fair_c if side == "bid" else (100.0 - fair_c)
    # What the market currently pays to take this leg off our hands.
    offer_c = None
    if best_bid_c is not None:
        offer_c = float(best_bid_c)
    sc = sigma_contract_c(ps)
    thr_c = margin_c(ps)          # same threshold family as entry
    # Basis: accounting + the approval-#5 cost trigger; must exist before
    # the action ladder below.
    basis_c = (cost / qty) * 100.0 if qty > 0 else None
    action = "hold"
    edge_out_c = None
    if offer_c is not None:
        # Selling at offer_c an asset worth mark_c: our edge is the excess
        # the market is paying over fair.  Cost basis deliberately absent
        # from THIS trigger.
        edge_out_c = offer_c - mark_c
        if edge_out_c >= thr_c:
            action = "sell_rich"
        elif (basis_c is not None and offer_c >= basis_c + thr_c):
            # Approval #5 item 2 (operator-ordered): the market paying
            # cost-plus-threshold also fires the exit path.  Same maker
            # mechanics; separate label so the two triggers stay
            # attributable in the exit-realization column.
            action = "sell_cost"
    # Exit target in THIS LEG's own scale: never sell below fair+thr,
    # chase the book when it pays better (approval #5 item 1).
    exit_target_c = None
    if action in ("sell_rich", "sell_cost"):
        exit_target_c = max(mark_c + thr_c,
                            offer_c if offer_c is not None else 0.0)
    return {"side": side, "qty": qty,
            "exit_target_c": (round(exit_target_c, 3)
                              if exit_target_c is not None else None),
            "mark_c": round(mark_c, 3),
            "offer_c": (round(offer_c, 3) if offer_c is not None else None),
            "edge_out_c": (round(edge_out_c, 3)
                           if edge_out_c is not None else None),
            "threshold_c": round(thr_c, 3),
            "sigma_c": (round(sc, 3) if sc is not None else None),
            "basis_c_accounting_only": (round(basis_c, 3)
                                        if basis_c is not None else None),
            "action": action}


def risk_reducing_fill(mt, side, quantity, risk_px, fee_reserve=0.0):
    """True only when the proposed fill cannot increase terminal loss."""
    quantity = float(quantity)
    if quantity <= 0:
        return False
    before = inventory_worst_loss(mt)
    after = inventory_worst_loss(
        mt,
        add_side=side,
        add_qty=quantity,
        add_cost=float(risk_px) * quantity + float(fee_reserve),
    )
    return after <= before + 1e-9


def side_blocked(mt, side):
    """Entry gate.  Legacy mode: the permanent one-fill-per-side latch.
    Pair mode: a side facing opposite unpaired lots is an EXIT leg and
    is always allowed; entries block at one clip of unpaired inventory;
    and in live, position on the book without a fresh exchange
    confirmation (recon <15s) blocks NEW entries — funds re-arm only
    after the exchange has agreed the book is what we think it is."""
    if not PAIR:
        return (mt, side) in S.latch
    if (mt, side) in S.pending_new:
        return True
    opp = "ask_no" if side == "bid" else "bid"
    if unpaired_ct(mt, opp) > 0:
        return False
    # Operator 2026-07-27 speed directive: concurrent cycles up to the
    # net cap (was: hard one-leg latch) -- cycle COUNT is where sample
    # efficiency lives.  Guarded by a rapid-fire brake: >=2 same-side
    # fills inside BRAKE_N_S means a sweep is eating the side we rest on;
    # stop reloading into it for BRAKE_HOLD_S.
    if unpaired_ct(mt, side) >= MAX_NET - 1e-9:
        return True
    if time.monotonic() < S.side_brake.get((mt, side), 0.0):
        return True
    if MODE == "live":
        stale = (S.last_recon_ok_mono is None
                 or time.monotonic() - S.last_recon_ok_mono > 15.0)
        if stale and any(v["y"] or v["n"] for v in S.net_pos.values()):
            return True
    return False


def cycle_admission_gate(
        mt, y_px, n_px, y_touch, n_touch, *,
        entry_pricing_ok, zone, net):
    """Return ``(admit, reason)`` for one new two-sided pair cycle.

    Every input is observable before either leg fills.  In particular the
    gate is symmetric in YES/NO depth; using the eventual first-filled side
    would be look-ahead.  Admission is all-or-none: the bundle must fit the
    pair-cost, inventory, unresolved-order, and capital constraints before
    the first POST is attempted.
    """
    if not (PAIR and PAIR_PREQUOTE):
        return False, "disabled"
    if not entry_pricing_ok:
        return False, "pricing"
    if not zone:
        return False, "zone"
    if not (math.isfinite(y_px) and math.isfinite(n_px)
            and y_px > 0.0 and n_px > 0.0):
        return False, "price"
    # The ceiling is a gross price ceiling; require an additional positive
    # net-edge buffer before committing collateral to a two-leg bundle.
    if (y_px + n_px) * 100.0 > PAIR_LOCK_C - PAIR_MIN_EDGE_C + 1e-9:
        return False, "pair_sum"
    if min(float(y_touch), float(n_touch)) + 1e-9 < PAIR_MIN_DEPTH_CT:
        return False, "depth"
    qty = float(CLIP)
    if qty <= 0.0 or qty > UNPAIRED_MAX_CT + 1e-9:
        return False, "clip"
    if abs(float(net)) > 1e-9:
        return False, "inventory"
    pos = S.net_pos.get(mt, {"y": 0.0, "n": 0.0, "cost": 0.0})
    if (abs(float(pos.get("y", 0.0))) > 1e-9
            or abs(float(pos.get("n", 0.0))) > 1e-9
            or abs(float(pos.get("cost", 0.0))) > 1e-9
            or unpaired_ct(mt, "bid") > 1e-9
            or unpaired_ct(mt, "ask_no") > 1e-9):
        return False, "inventory"
    if side_blocked(mt, "bid") or side_blocked(mt, "ask_no"):
        return False, "side_blocked"
    if any(key[0] == mt for key in S.pending_new):
        return False, "pending"
    if any(key[0] == mt for key in S.flatten_pending):
        return False, "flatten_pending"
    if any(value.get("ticker") == mt
           for value in S.unknown_orders.values()):
        return False, "unknown"
    if (float(net) + qty > MAX_NET + 1e-9
            or float(net) - qty < -MAX_NET - 1e-9):
        return False, "net_cap"
    bundle_cost = qty * (float(y_px) + float(n_px))
    if exposure() + bundle_cost > pair_capital_limit() + 1e-9:
        return False, "capital"
    return True, "admit"


def _safe_resting_pair_exit(mt, exit_side, entry_lots):
    """Price of an already-resting exact counterpart, else ``None``.

    Keeping this order preserves queue age after the first leg fills.  A
    quantity mismatch (notably a partial first-leg fill), an unresolved
    order, or a price that fails the pair cap forces the ordinary
    cancel/resize/reprice path.
    """
    od = S.orders.get((mt, exit_side))
    if od is None or not entry_lots:
        return None
    oid = od.get("id")
    if oid in S.unknown_orders or (mt, exit_side) in S.pending_new:
        return None
    residual = sum(float(lot[2]) for lot in entry_lots)
    if abs(float(od.get("qty", 0.0)) - residual) > 1e-9:
        return None
    worst_entry_px = max(float(lot[0]) for lot in entry_lots)
    resting_px = float(od.get("px", 0.0))
    if ((worst_entry_px + resting_px) * 100.0
            > PAIR_LOCK_C + 1e-9):
        return None
    return resting_px


def orphan_relax_c(lot_ts, now_s=None):
    """How many cents of loss the complement quote may accept, by lot age.

    0 while the lot is young (hold the profit ceiling), then ramping to
    ORPHAN_MAKER_MAX_LOSS_C so the exit becomes reachable instead of
    sitting at a price the book will never come to.  Raising the ceiling
    means paying MORE for the complement, i.e. accepting a smaller (then
    negative) lock -- always as a maker, never crossing.
    """
    if ORPHAN_MAKER_MAX_LOSS_C <= 0 or ORPHAN_MAKER_AGE_S <= 0:
        return 0.0
    now_s = time.time() if now_s is None else now_s
    age = now_s - float(lot_ts)
    if age <= ORPHAN_MAKER_AGE_S:
        return 0.0
    ramp = min(1.0, (age - ORPHAN_MAKER_AGE_S) / ORPHAN_MAKER_AGE_S)
    return ORPHAN_MAKER_MAX_LOSS_C * ramp


def pair_push_prices(mt, y_px, n_px, yb, ya):
    """Pair mode: push the leg OPPOSITE the oldest unpaired lot up to
    the pair-cost ceiling (PAIR_LOCK_C - entry locks >= the complement),
    clamped one cent under the cross so post_only survives.  Returns
    (y_px, n_px, exit_bid, exit_no)."""
    lots = S.unpaired.get(mt)
    if not lots:
        return y_px, n_px, False, False
    exit_bid = exit_no = False
    if lots["bid"]:                    # long unpaired YES -> push NO leg
        resting_no = _safe_resting_pair_exit(mt, "ask_no", lots["bid"])
        if resting_no is not None:
            n_px = resting_no
        else:
            ceil_no = ((PAIR_LOCK_C - PAIR_MIN_EDGE_C
                        + orphan_relax_c(lots["bid"][0][1])) / 100.0
                       - lots["bid"][0][0])
            no_ask = 1.0 - yb / 10000.0
            n_px = legal_floor(max(0.0, min(ceil_no, no_ask - 0.01)))
        # A complement quote is optional risk reduction, not a forced buy.
        # If the remaining capital budget cannot carry it, leave the lot to
        # the bounded orphan-exit path instead of creating more locked stock.
        intent = S.exit_intent.get(mt)
        if intent and intent.get("side") == "bid" and intent.get("target_c"):
            # Approval #5 item 1: never sell the YES leg below fair+thr --
            # buying its complement above 100-(target) is the same thing.
            # Floor at 1c: a cap below the exchange minimum is not a quote,
            # it is a silent outage (placement lamp, 2026-07-28T00:00).
            cap_exit = legal_floor(
                max(0.01, 1.0 - float(intent["target_c"]) / 100.0))
            n_px = min(n_px, cap_exit)
        exit_no = (n_px > 0.0 and pair_exit_within_cap(
            mt, "ask_no", n_px, unpaired_ct(mt, "bid")))
    if lots["ask_no"]:                 # long unpaired NO -> push YES leg
        resting_yes = _safe_resting_pair_exit(mt, "bid", lots["ask_no"])
        if resting_yes is not None:
            y_px = resting_yes
        else:
            ceil_yes = ((PAIR_LOCK_C - PAIR_MIN_EDGE_C
                         + orphan_relax_c(lots["ask_no"][0][1])) / 100.0
                        - lots["ask_no"][0][0])
            yes_ask = ya / 10000.0
            y_px = legal_floor(max(0.0, min(ceil_yes, yes_ask - 0.01)))
        intent = S.exit_intent.get(mt)
        if intent and intent.get("side") == "ask_no" \
                and intent.get("target_c"):
            cap_exit = legal_floor(
                max(0.01, 1.0 - float(intent["target_c"]) / 100.0))
            y_px = min(y_px, cap_exit)
        exit_bid = (y_px > 0.0 and pair_exit_within_cap(
            mt, "bid", y_px, unpaired_ct(mt, "ask_no")))
    return y_px, n_px, exit_bid, exit_no


def pair_off(mt, side, n, px, ts, fee_total=0.0):
    """FIFO-net a fill against opposite unpaired lots.  A netted YES/NO
    pair returns $1/contract from the exchange (positions net;
    settlement only sees the net) — realized locks 100c minus both
    entries and the collateral leaves the ledger NOW, which is the
    whole 资金释放 point.  The remainder becomes an unpaired lot."""
    lots = S.unpaired.setdefault(mt, {"bid": [], "ask_no": []})
    opp = "ask_no" if side == "bid" else "bid"
    rem = n
    fee_per_ct = fee_total / n if n > 0 else 0.0
    paired_here = 0.0
    while rem > 0 and lots[opp]:
        opx, ots, oct_, ofee = lots[opp][0]
        take = min(rem, oct_)
        this_fee = fee_per_ct * take
        opp_fee = ofee * (take / oct_) if oct_ > 0 else 0.0
        locked = (1.0 - px - opx) * take - this_fee - opp_fee
        S.realized += locked
        S.pair_locked_by_market[mt] += locked
        S.pair_locked_total += locked
        paired_here += take
        d = S.net_pos.setdefault(mt, {"y": 0, "n": 0, "cost": 0.0})
        d["y"] -= take
        d["n"] -= take
        d["cost"] -= (px + opx) * take + this_fee + opp_fee
        if abs(d["cost"]) < 1e-9:
            d["cost"] = 0.0
        L.w({"ev": "PAIR_LOCK", "mt": mt, "ct": take,
             "px_a": opx, "px_b": px, "locked_usd": round(locked, 4),
             "pair_wait_s": round(max(0.0, float(ts) - float(ots)), 3),
             "fee_usd": round(this_fee + opp_fee, 6),
             "realized": round(S.realized, 4)})
        if take >= oct_ - 1e-9:
            lots[opp].pop(0)
        else:
            lots[opp][0] = (opx, ots, oct_ - take, ofee - opp_fee)
        rem -= take
    if rem > 1e-9:
        remaining_fee = fee_per_ct * rem
        lots[side].append((px, ts, rem, remaining_fee))
        S.cycle_active.add(mt)
        # Attribution: orphan count is one of the three numbers this run
        # must produce (target: orphan cost under 3c, judged after >=30).
        S.gate_counts["orphans"] = S.gate_counts.get("orphans", 0) + 1
    if paired_here > 0:
        # A cycle is terminal only when no remainder exists on either side.
        if (unpaired_ct(mt, "bid") <= 1e-9
                and unpaired_ct(mt, "ask_no") <= 1e-9
                and mt in S.cycle_active):
            S.cycle_active.discard(mt)
            S.completed_cycles += 1
            if MAX_CYCLES and S.completed_cycles >= MAX_CYCLES:
                S.canary_done = True
            L.w({"ev": "PAIR_CYCLE_DONE", "mt": mt,
                 "completed_cycles": S.completed_cycles,
                 "pair_locked_total": round(S.pair_locked_total, 6)})
    S.open_cost = sum(v["cost"] for v in S.net_pos.values())


def flatten_due(now_s):
    """(mt, side) list of unpaired lots past UNPAIRED_AGE_S, with a 5s
    IOC-resend cooldown so the 1s task never spams the book."""
    if not PAIR:
        return []
    due = []
    for mt, lots in S.unpaired.items():
        for side in ("bid", "ask_no"):
            if not lots[side]:
                continue
            if now_s - lots[side][0][1] < UNPAIRED_AGE_S:
                continue
            if (mt, side) in S.flatten_pending:
                continue
            if now_s - S.flatten_sent.get((mt, side), 0.0) < 5.0:
                continue
            due.append((mt, side))
    return due


def flatten_lot_taker(mt, side, now_s, cross=0.01, reason="unpaired_age"):
    """Cut an unpaired lot: IOC-cross the OPPOSITE side ``cross`` dollars
    through its best (7% taker fee accepted — a bounded loss beats
    settlement gambling).  The resulting fill flows back through
    apply_fill and pairs off, so the loss books through the ONE ledger.
    Knife 1 calls this with cross=0.0/reason="lock_take" to harvest a
    completion the book is already paying for."""
    bk = S.books.get(mt)
    lots = S.unpaired.get(mt)
    if not bk or not lots or not lots[side]:
        return
    yb, nb = best(bk["y"]), best(bk["n"])
    _px, _ts, ct, _fee = lots[side][0]
    if side == "bid":                  # long YES -> buy NO at its ask
        if yb is None:
            # Empty opposite book: no exit exists at ANY price (graveyard
            # with nobody bidding the losing side).  Receipt + cooldown --
            # the silent 1/s retry spam of 2026-07-27T22:14 taught us that
            # an impossible cut must SAY so, once per cooldown.
            S.flatten_sent[(mt, side)] = now_s
            L.w({"ev": "FLATTEN_IMPOSSIBLE", "mt": mt, "side": side,
                 "why": "no_yes_bid"})
            return
        no_px = min(0.99, (1.0 - yb / 10000.0) + cross)
        wire_side, wire_px = "ask", round(1.0 - no_px, 4)
    else:                              # long NO -> buy YES at its ask
        if nb is None:
            S.flatten_sent[(mt, side)] = now_s
            L.w({"ev": "FLATTEN_IMPOSSIBLE", "mt": mt, "side": side,
                 "why": "no_no_bid"})
            return
        yes_px = min(0.99, (1.0 - nb / 10000.0) + cross)
        wire_side, wire_px = "bid", round(yes_px, 4)
    # A resting maker hedge and an IOC hedge must never coexist: both can
    # fill and turn a flat position into a new orphan in the other direction.
    exit_side = "ask_no" if side == "bid" else "bid"
    resting_exit = S.orders.get((mt, exit_side))
    if resting_exit:
        if not order_cancel(
                mt, exit_side, resting_exit["id"],
                reason="cancel_maker_before_ioc"):
            return
        risk_map_pop(S.orders, (mt, exit_side))
    S.flatten_sent[(mt, side)] = now_s
    order_taker(mt, wire_side, wire_px, ct, reason=reason)


def lock_take_due(now_s):
    """Knife 1 scanner: unpaired lots whose OPPOSITE best already pays
    basis + taker fee + LOCK_TAKE_MIN_C.  Completing at the touch is a
    guaranteed positive lock; waiting as a maker was how 87% of exit
    intents died unfilled and became settlement roulette."""
    if not PAIR:
        return []
    due = []
    for mt, lots in S.unpaired.items():
        bk = S.books.get(mt)
        if not bk:
            continue
        yb, nb = best(bk["y"]), best(bk["n"])
        for side in ("bid", "ask_no"):
            if not lots[side]:
                continue
            px0, ts0, _ct0, _f0 = lots[side][0]
            if now_s - ts0 < LOCK_TAKE_AGE_S:
                continue
            if (mt, side) in S.flatten_pending:
                continue
            if now_s - S.flatten_sent.get((mt, side), 0.0) < 5.0:
                continue
            opp_best = yb if side == "bid" else nb
            if opp_best is None:
                continue
            proceeds_c = opp_best / 100.0
            fee_c = taker_fee_usd(proceeds_c / 100.0, 1.0) * 100.0
            net_c = proceeds_c - px0 * 100.0 - fee_c
            if net_c >= LOCK_TAKE_MIN_C:
                L.w({"ev": "LOCK_TAKE", "mt": mt, "side": side,
                     "basis_c": round(px0 * 100.0, 2),
                     "proceeds_c": round(proceeds_c, 2),
                     "fee_c": round(fee_c, 2), "net_c": round(net_c, 2)})
                due.append((mt, side))
    return due


def order_taker(mt, side, exchange_px, count, reason=""):
    """IOC risk-cut order.  Deliberately not capped by within_cap: it
    strictly reduces unpaired inventory and is bounded by
    UNPAIRED_MAX_CT; its cost books through apply_fill like any fill."""
    orphan_side = "bid" if side == "ask" else "ask_no"
    pending_key = (mt, orphan_side)
    if pending_key in S.flatten_pending:
        L.w({"ev": "TAKER_PENDING_HOLD", "mt": mt, "side": side,
             "pending": S.flatten_pending[pending_key]})
        return False
    if not take_token():
        L.w({"ev": "RATE_SKIP", "mt": mt, "side": side})
        return False
    client_order_id = f"{LINE_TAG}-{uuid.uuid4()}"
    engine_side = "ask_no" if side == "ask" else "bid"
    risk_px = (
        1.0 - float(exchange_px)
        if engine_side == "ask_no" else float(exchange_px)
    )
    fee_reserve = taker_fee_usd(risk_px, count)
    candidate = mba.CandidateIntent(
        reference=client_order_id,
        ticker=mt,
        engine_side=engine_side,
        risk_px=risk_px,
        quantity=float(count),
        fee_reserve=fee_reserve,
    )
    body = {"ticker": mt, "client_order_id": client_order_id,
            "side": side, "count": f"{count:.2f}",
            "price": f"{exchange_px:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "reduce_only": True}
    if MODE == "live":
        if not budget_preflight_candidate(candidate):
            L.w({"ev": "BUDGET_POST_REJECT", "kind": "ioc", "mt": mt,
                 "side": engine_side, "risk_px": risk_px,
                 "qty": float(count), "fee_reserve": fee_reserve})
            return False
        risk_map_set(S.flatten_pending, pending_key, {
            "client_order_id": client_order_id,
            "qty": float(count),
            "sent_at": time.time(),
            "order_id": None,
            "risk_px": risk_px,
            "remaining_risk_qty": float(count),
            "fee_reserve": fee_reserve,
        })
        code, resp = rest("POST", "/portfolio/events/orders", body, host=V2O)
        oid = (resp.get("order_id") or
               (resp.get("order") or {}).get("order_id") or resp.get("id"))
        S.flatten_pending[pending_key]["order_id"] = oid
        risk_map_touch(S.flatten_pending, pending_key)
        L.w({"ev": "TAKER_ACK" if code in (200, 201) else "TAKER_REJ",
             "mt": mt, "side": side, "px": exchange_px,
             "count": count, "code": code, "reason": reason,
             "oid": oid, "client_order_id": client_order_id,
             "resp": str(resp)[:200]})
        if code not in (200, 201) and "insufficient_balance" in str(resp) \
                and not S.halted:
            S.halted = True
            cancel_all("INSUFFICIENT_BALANCE")
            L.w({"ev": "HALT", "reason": "insufficient_balance on taker "
                 "cut — local ledger untrusted"})
            write_control_status()
        if code not in (200, 201):
            # A timeout/5xx is ambiguous and therefore remains pending.
            # A definitive 4xx is known rejected and can release the pending
            # marker, except insufficient_balance which has already halted.
            if 400 <= code < 500:
                risk_map_pop(S.flatten_pending, pending_key)
            if code == 429:
                S.flatten_sent[pending_key] = time.time()
            elif code < 0 or code >= 500:
                S.halted = True
                L.w({"ev": "HALT", "reason":
                     "ambiguous IOC submit — retaining pending marker"})
                write_control_status()
            return False
        # Create-order V2 returns the final IOC fill/remaining counts.
        # Reconcile its fills now; zero and partial fills are both terminal
        # when remaining_count is zero, so the residual may retry later.
        order_result = (resp.get("order") or resp
                        if isinstance(resp, dict) else {})
        fill_count = qty_of(order_result, "fill_count")
        remaining = qty_of(order_result, "remaining_count")
        if oid and fill_count is not None and remaining is not None:
            if remaining <= 1e-9:
                pending = S.flatten_pending[pending_key]
                pending["terminal"] = True
                pending["expected_fill"] = fill_count
                pending["remaining_risk_qty"] = max(0.0, float(fill_count))
                pending["fee_reserve"] = (
                    taker_fee_usd(risk_px, fill_count)
                    if fill_count > 1e-9 else 0.0
                )
                risk_map_touch(S.flatten_pending, pending_key)
                # A zero-fill IOC is already fully reconciled by its terminal
                # create response.  A partial/full fill stays pending until
                # the matching fill event reaches the one ledger.
                if fill_count <= 1e-9:
                    risk_map_pop(S.flatten_pending, pending_key)
                else:
                    poll_fills_once(order_id=oid)
                    if abs(S.filled_by_order.get(oid, 0.0)
                           - fill_count) <= 1e-9:
                        risk_map_pop(S.flatten_pending, pending_key)
                return True
        S.halted = True
        L.w({"ev": "IOC_UNKNOWN", "mt": mt, "side": side, "oid": oid,
             "fill_count": fill_count, "remaining_count": remaining,
             "applied_fills": S.filled_by_order.get(oid, 0.0) if oid else 0.0})
        write_control_status()
        return code in (200, 201)
    L.w({"ev": "INTENT_TAKER", "mt": mt, "side": side, "px": exchange_px,
         "count": count, "reason": reason, "mode": "shadow"})
    shadow_oid = "shadow-ioc-" + str(uuid.uuid4())[:8]
    risk_map_set(S.flatten_pending, pending_key, {
        "client_order_id": client_order_id,
        "qty": float(count),
        "sent_at": time.time(),
        "order_id": shadow_oid,
        "risk_px": risk_px,
        "remaining_risk_qty": float(count),
        "fee_reserve": fee_reserve,
    })
    if SHADOW_QUEUE_SIM:
        # Conservative shadow IOC: charge the submitted limit price (not a
        # possible better execution) and the official quadratic taker fee.
        outcome = "no" if side == "ask" else "yes"
        risk_px = (1.0 - float(exchange_px)
                   if outcome == "no" else float(exchange_px))
        fee = taker_fee_usd(risk_px, count)
        pending = S.flatten_pending[pending_key]
        pending["terminal"] = True
        pending["expected_fill"] = float(count)
        risk_map_touch(S.flatten_pending, pending_key)
        sim_id = f"{shadow_oid}:{time.time_ns()}"
        L.w({"ev": "SHADOW_TAKER_FILL_SIM", "mt": mt, "side": side,
             "outcome_side": outcome, "risk_px": risk_px,
             "count": float(count), "fee_usd": round(fee, 8),
             "reason": reason, "order_id": shadow_oid})
        apply_fill({
            "fill_id": sim_id,
            "trade_id": sim_id,
            "order_id": shadow_oid,
            "ticker": mt,
            "outcome_side": outcome,
            "book_side": "ask" if outcome == "no" else "bid",
            "side": outcome,
            "count_fp": f"{float(count):.8f}",
            "yes_price_dollars": (
                f"{1.0 - risk_px:.4f}" if outcome == "no"
                else f"{risk_px:.4f}"),
            "no_price_dollars": (
                f"{risk_px:.4f}" if outcome == "no"
                else f"{1.0 - risk_px:.4f}"),
            "created_ts": int(time.time() * 1000),
            "fee_cost": fee,
        })
    return True


def taker_exit_justified(mt, side, ps, tte):
    """Approval #5 item 3: the ONLY sanctioned taker exit is the explicit
    comparison  hold_cost > taker_fee + spread.

        hold_cost_c  ~ q * gamma * sigma_c(remaining)   (A-S 9.8 shape)
        taker_fee_c  = 7 * p * (1-p)                    (fee curve, cents)
        spread_c     = what crossing gives up vs resting

    Returns (justified, receipt_fields).  Every TRUE decision is logged by
    the caller -- the retired blind timer died for lack of receipts.
    """
    qty = unpaired_ct(mt, side)
    if qty <= 0 or ps is None:
        return False, {}
    sc = sigma_contract_c(ps, tau_s=min(max(tte, 1.0), 300.0))
    if sc is None:
        return False, {}
    gamma = gamma_c_dyn(ps)
    hold_cost_c = qty * gamma * sc
    fair = S.last_fair_c.get(mt)
    p_hat = (fair / 100.0) if fair is not None else 0.5
    taker_fee_c = TAKER_FEE_COEF_C * p_hat * (1.0 - p_hat)
    book = S.books.get(mt) or {}
    yb, ya = book.get("yb"), book.get("ya")
    spread_c = ((ya - yb) / 100.0) if (yb is not None and ya is not None
                                       and ya > yb) else 1.0
    justified = hold_cost_c > (taker_fee_c + spread_c)
    return justified, {"hold_cost_c": round(hold_cost_c, 3),
                       "taker_fee_c": round(taker_fee_c, 3),
                       "spread_c": round(spread_c, 3), "qty": qty}


async def pair_flatten_task():
    while True:
        await asyncio.sleep(1)
        # Pausing stops entries, not exits.  A reduce-only orphan cut must
        # remain available while draining a paused or halted canary.
        if not PAIR:
            continue
        now_s = time.time()
        for mt, side in lock_take_due(now_s):
            flatten_lot_taker(mt, side, now_s, cross=0.0, reason="lock_take")
        for mt, side in flatten_due(now_s):
            flatten_lot_taker(mt, side, now_s)
        # Approval #5 risk-layer exits: (4) graveyard unconditional close,
        # (3) explicit hold-cost-vs-taker-cost crossings.  Both leave a
        # receipt per instance.
        for mt, meta_row in list(S.meta.items()):
            tte = meta_row[1] - now_s
            for side in ("bid", "ask_no"):
                qty = unpaired_ct(mt, side)
                if qty <= 0:
                    continue
                if (mt, side) in S.flatten_pending:
                    continue
                if now_s - S.flatten_sent.get((mt, side), 0.0) < 5.0:
                    continue
                if 0 < tte < FLATTEN_TTE_S:
                    L.w({"ev": "GRAVE_FLATTEN", "mt": mt, "side": side,
                         "qty": qty, "tte": round(tte, 1)})
                    flatten_lot_taker(mt, side, now_s)
                    continue
                intent = S.exit_intent.get(mt)
                if intent and intent.get("side") == side:
                    ser = meta_row[0]
                    ps = None
                    try:
                        ps = pricing_state(ser, meta_row[1], now_s=now_s)
                    except Exception:
                        ps = None
                    ok_t, fields = taker_exit_justified(mt, side, ps, tte)
                    if ok_t:
                        L.w({"ev": "TAKER_EXIT", "mt": mt, "side": side,
                             **fields})
                        flatten_lot_taker(mt, side, now_s)


def _pending_to_resting(pending_key, oid):
    """Atomically reclassify one acknowledged POST reservation as resting."""
    pending = S.pending_new.get(pending_key)
    if pending is None:
        raise RuntimeError("acknowledged order lost its pending reservation")
    S.orders[pending_key] = {
        "id": oid,
        "client_order_id": pending["client_order_id"],
        "px": float(pending["px"]),
        "qty": float(pending["qty"]),
        "wire_side": pending["wire_side"],
        "exchange_px": float(pending["exchange_px"]),
        "t": time.time(),
        "expire_ts": pending.get("expire_ts"),
    }
    del S.pending_new[pending_key]
    risk_generation_bump()
    return S.orders[pending_key]


def order_place(mt, side, exchange_px, *, risk_px=None, edge_c=None,
                quantity=None, risk_reducing=False, slot_side=None):
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
    quantity = float(CLIP if quantity is None else quantity)
    risk_px = float(exchange_px if risk_px is None else risk_px)
    engine_side = slot_side or ("ask_no" if side == "ask" else "bid")
    pending_key = (mt, engine_side)
    if pending_key in S.pending_new:
        L.w({"ev": "PENDING_NEW_HOLD", "mt": mt, "side": engine_side})
        return None
    now_s = time.time()
    if now_s < S.place_not_before.get(pending_key, 0.0):
        L.w({"ev": "PLACE_BACKOFF_HOLD", "mt": mt, "side": engine_side,
             "until_in_s": round(
                 S.place_not_before[pending_key] - now_s, 3)})
        return None
    if risk_reducing:
        if not risk_reducing_fill(mt, engine_side, quantity, risk_px):
            L.w({"ev": "RISK_REDUCE_REJ", "mt": mt, "side": engine_side,
                 "risk_px": risk_px, "qty": quantity,
                 "before": round(inventory_worst_loss(mt), 6),
                 "after": round(inventory_worst_loss(
                     mt, add_side=engine_side, add_qty=quantity,
                     add_cost=risk_px * quantity), 6)})
            return None
    else:
        pos = S.net_pos.get(mt, {"y": 0.0, "n": 0.0})
        net = float(pos["y"]) - float(pos["n"])
        projected_net = (
            net + quantity if engine_side == "bid" else net - quantity
        )
        if abs(projected_net) > MAX_NET + 1e-9:
            L.w({"ev": "NET_CAP_SKIP", "mt": mt, "side": engine_side,
                 "net": net, "projected_net": projected_net,
                 "max_net": MAX_NET, "qty": quantity})
            return None
        if (PAIR and unpaired_ct(mt, engine_side) + quantity
                > UNPAIRED_MAX_CT + 1e-9):
            L.w({"ev": "UNPAIRED_CAP_SKIP", "mt": mt,
                 "side": engine_side,
                 "unpaired": unpaired_ct(mt, engine_side),
                 "qty": quantity, "max_unpaired": UNPAIRED_MAX_CT})
            return None
        if not within_cap(risk_px, quantity):
            L.w({"ev":"CAP_SKIP","mt":mt,"side":side,"px":exchange_px,
                 "risk_px":risk_px,"qty":quantity,
                 "exposure":round(exposure(),4),
                 "max_open_cost":MAX_OPEN_COST})
            return None
    if not take_token():                      # A4
        S.place_not_before[pending_key] = now_s + 0.25
        L.w({"ev":"RATE_SKIP","mt":mt,"side":side}); return None
    client_order_id = f"{LINE_TAG}-{uuid.uuid4()}"
    body = {"ticker": mt, "client_order_id": client_order_id, "side": side,
            "count": f"{quantity:.2f}", "price": f"{exchange_px:.4f}",
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "maker", "post_only": True}
    order_expire_ts = None
    if ORDER_TTL_S > 0:
        order_expire_ts = int(time.time() + ORDER_TTL_S)
        body["expiration_ts"] = order_expire_ts
    # Live V2 rejects reduce_only on GTC orders ("can only be used with
    # IoC").  Passive hedge safety therefore comes from exact residual size,
    # one-order-per-side, and zero-tolerance position reconciliation.  The
    # timed IOC cut below retains exchange-enforced reduce_only.
    if MODE == "live":
        candidate = mba.CandidateIntent(
            reference=client_order_id,
            ticker=mt,
            engine_side=engine_side,
            risk_px=risk_px,
            quantity=quantity,
            fee_reserve=0.0,
        )
        if not budget_preflight_candidate(candidate):
            L.w({"ev": "BUDGET_POST_REJECT", "kind": "maker", "mt": mt,
                 "side": engine_side, "risk_px": risk_px,
                 "qty": quantity})
            return None
        risk_map_set(S.pending_new, pending_key, {
            "client_order_id": client_order_id, "px": risk_px,
            "qty": quantity, "wire_side": side,
            "exchange_px": exchange_px, "sent_at": time.time(),
            "risk_reducing": bool(risk_reducing),
            "expire_ts": order_expire_ts,
        })
        t0 = time.monotonic()
        code, resp = rest("POST", "/portfolio/events/orders", body, host=V2O)
        ms = round((time.monotonic() - t0) * 1000.0, 1)
        oid = (resp.get("order_id") or (resp.get("order") or {}).get("order_id")
               or resp.get("id"))
        L.w({"ev":"ORDER_ACK" if code in (200,201) else "ORDER_REJ",
             "mt":mt,"side":side,"px":exchange_px,"risk_px":risk_px,
             "qty":quantity,"code":code,"ms":ms,"edge_c":edge_c,
             "risk_reducing": risk_reducing,
             "oid":oid,"resp":str(resp)[:200]})
        if code in (200, 201) and oid:
            _pending_to_resting(pending_key, oid)
            S.place_not_before.pop(pending_key, None)
            return oid, quantity
        definite_reject = 400 <= code < 500 and code != 409
        if definite_reject:
            risk_map_pop(S.pending_new, pending_key)
            S.place_not_before[pending_key] = time.time() + 1.0
        if code not in (200, 201) and "insufficient_balance" in str(resp):
            # The exchange refusing for money we think we have means the
            # LOCAL ledger is wrong.  07-25: the engine retried this 317
            # times through 18,703 rate-limit blocks.  First strike stops
            # the session; no auto-resume.
            if not S.halted:
                S.halted = True
                cancel_all("INSUFFICIENT_BALANCE")
                L.w({"ev": "HALT",
                     "reason": "exchange insufficient_balance on first "
                               "reject — local ledger untrusted"})
                write_control_status()
        if not definite_reject:
            # Timeout/5xx/2xx-without-id/duplicate is ambiguous: the order
            # may exist.  Keep the reservation and halt, never retry a new
            # UUID into an unknown live order.
            S.halted = True
            L.w({"ev": "ORDER_SUBMIT_UNKNOWN", "mt": mt,
                 "side": engine_side, "client_order_id": client_order_id,
                 "code": code})
            cancel_all("ORDER_SUBMIT_UNKNOWN")
            write_control_status()
        return None
    L.w({"ev":"INTENT_PLACE","mt":mt,"side":side,"px":exchange_px,
         "risk_px":risk_px,"qty":quantity,"edge_c":edge_c,
         "risk_reducing": risk_reducing, "mode":"shadow"})
    return "shadow-"+str(uuid.uuid4())[:8], quantity

def order_cancel(mt, side, oid, reason=""):
    S.order_tombstones[oid] = time.monotonic()   # release imminent
    if oid in S.unknown_orders:
        return False
    # Capture the reservation's economics NOW: if the local order row is
    # gone by the time the budget guard shapes this unknown record, an
    # economics-free record durably latches the budget
    # (2026-07-28T02:48 BUDGET_MONITOR_SNAPSHOT_BLIND, qty/px/xpx all None).
    _od0 = S.orders.get((mt, side))
    _econ0 = (
        {k: _od0[k] for k in ("qty", "px", "wire_side", "exchange_px")
         if k in _od0}
        if isinstance(_od0, dict) and _od0.get("id") == oid else {}
    )
    if MODE == "live" and oid and not oid.startswith("shadow-"):
        t0 = time.monotonic()
        code, resp = rest("DELETE", f"/portfolio/events/orders/{oid}", host=V2O)
        ms = round((time.monotonic() - t0) * 1000.0, 1)
        ok = False
        L.w({"ev":"CANCEL_ACK" if code in (200, 201) else "CANCEL_FAIL",
             "mt":mt,"side":side,"oid":oid,"code":code,"ms":ms,
             "reason":reason})
        if code in (200, 201):
            # The V2 ACK proves only how much was canceled.  Apply any fill
            # racing the cancel before deciding whether the reservation is
            # safe to release.
            reduced_by = qty_of(resp, "reduced_by")
            sweep_ok = poll_fills_once(order_id=oid)
            current = next((
                float(v.get("qty", CLIP)) for v in S.orders.values()
                if v.get("id") == oid
            ), 0.0)
            if (reduced_by is not None and sweep_ok
                    and abs(current - reduced_by) <= 1e-9):
                risk_map_pop(S.unknown_orders, oid)
                return True
            risk_map_set(S.unknown_orders, oid, {
                **_econ0,
                "ticker": mt, "side": side, "reason": reason,
                "since": time.time(), "delete_code": code,
                "reduced_by": reduced_by, "local_remaining": current,
            })
            S.halted = True
            L.w({"ev": "ORDER_UNKNOWN", "mt": mt, "side": side,
                 "oid": oid, "reason": "cancel_ack_mismatch",
                 "reduced_by": reduced_by, "local_remaining": current})
            write_control_status()
            return False
        if code == 404:
            # NotFound does not prove canceled or filled.  Reconcile only
            # this order; retain its slot/reservation unless a terminal state
            # or a full fill is proven.
            had_local_order = any(
                v.get("id") == oid for v in S.orders.values())
            sweep_ok = poll_fills_once(order_id=oid)
            if (sweep_ok and had_local_order
                    and not any(v.get("id") == oid
                                for v in S.orders.values())):
                risk_map_pop(S.unknown_orders, oid)
                return True
            c2, d2 = rest("GET", f"/portfolio/events/orders/{oid}",
                          host=V2O)
            if c2 != 200:
                c2, d2 = rest("GET", f"/portfolio/orders/{oid}")
            order = (d2.get("order") or d2) if isinstance(d2, dict) else {}
            status = str(order.get("status", "")).lower()
            # Deadman expiry is a fully-explained disappearance: the fill
            # sweep above already applied any partial fill, and either the
            # exchange reports the order expired or our own recorded
            # expiration has passed.  Release the remainder cleanly.
            od = S.orders.get((mt, side))
            local_exp = (od or {}).get("expire_ts")
            expiry_explains = (sweep_ok and (
                status == "expired"
                or (local_exp is not None
                    and time.time() >= float(local_exp))))
            if expiry_explains and od is not None and od.get("id") == oid:
                risk_map_pop(S.orders, (mt, side))
                risk_map_pop(S.unknown_orders, oid)
                L.w({"ev": "ORDER_EXPIRED", "mt": mt, "side": side,
                     "oid": oid, "lookup_status": status or None,
                     "local_expire_ts": local_exp})
                return True
            # A terminal-looking status alone is not ledger proof: an
            # executed order can appear before its fill reaches the fills
            # endpoint.  Only the order-scoped fill sweep removing the full
            # local remainder above may release a 404 reservation.
            risk_map_set(S.unknown_orders, oid, {
                **_econ0,
                "ticker": mt, "side": side, "reason": reason,
                "since": time.time(), "delete_code": code,
                "lookup_code": c2, "lookup_status": status or None,
            })
            S.halted = True
            L.w({"ev": "ORDER_UNKNOWN", "mt": mt, "side": side,
                 "oid": oid, "reason": reason, "lookup_code": c2,
                 "lookup_status": status or None})
            write_control_status()
            return False
        if code == 429:
            return False
        if code < 0 or code >= 500 or code == 204:
            risk_map_set(S.unknown_orders, oid, {
                **_econ0,
                "ticker": mt, "side": side, "reason": reason,
                "since": time.time(), "delete_code": code,
            })
            S.halted = True
            write_control_status()
        return ok
    else:
        L.w({"ev":"INTENT_CANCEL","mt":mt,"side":side,"oid":oid,
             "reason":reason})
    return True

def cancel_all(reason):
    all_canceled = True
    for (mt, side), od in list(S.orders.items()):
        if order_cancel(mt, side, od["id"], reason=reason):
            risk_map_pop(S.orders, (mt, side))
        else:
            all_canceled = False
    L.w({"ev":"CANCEL_ALL","reason":reason,
         "complete":all_canceled,"remaining":len(S.orders)})
    return all_canceled


# ------------------------------------------------ absolute budget integration
def live_writer_lock_path():
    """Fixed same-host writer lock, bound to account/subaccount/exchange.

    This advisory lock deliberately does not depend on MM_BUDGET_PATH, so two
    processes cannot evade it by pointing at different budget files.  It does
    not coordinate processes on different hosts.
    """
    fingerprint = mba.account_fingerprint(
        KEY_ID,
        subaccount=BUDGET_SUBACCOUNT,
        exchange_index=BUDGET_EXCHANGE_INDEX,
    )
    return Path("/tmp") / f"crypto-mm-live-{fingerprint}.engine.lock"


def acquire_live_writer_lock(path=None):
    """Hold the account-fixed, same-host lock for the live-engine lifetime."""
    # ``path`` remains accepted for compatibility, but can never select the
    # lock target; the account binding above is the sole namespace authority.
    del path
    lock_path = live_writer_lock_path()
    if S.live_writer_lock_fd is not None:
        if S.live_writer_lock_path == lock_path:
            return S.live_writer_lock_fd
        raise mba.AdapterError("live writer already holds a different lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        os.close(fd)
        raise mba.AdapterError(
            f"another live engine holds {lock_path}"
        ) from exc
    S.live_writer_lock_fd = fd
    S.live_writer_lock_path = lock_path
    return fd


def initialize_budget_guard(*, path=None, acquire_writer=True):
    """Load the one immutable $20.157/$10 budget; never create or rebase it."""
    budget_path = Path(BUDGET_PATH if path is None else path)
    if acquire_writer:
        acquire_live_writer_lock(budget_path)
    guard = mba.BudgetGuard(
        budget_path=budget_path,
        account_identity=KEY_ID,
        subaccount=BUDGET_SUBACCOUNT,
        exchange_index=BUDGET_EXCHANGE_INDEX,
        cancel_all=budget_cancel_all,
        halt=budget_halt,
    )
    record = guard.record
    if record.anchor_equity_micro_usd != BUDGET_ANCHOR_MICRO_USD:
        raise mba.AdapterError(
            "budget anchor mismatch: expected "
            f"{BUDGET_ANCHOR_MICRO_USD}, got "
            f"{record.anchor_equity_micro_usd}"
        )
    if record.max_loss_micro_usd != BUDGET_MAX_LOSS_MICRO_USD:
        raise mba.AdapterError(
            "budget max-loss mismatch: expected "
            f"{BUDGET_MAX_LOSS_MICRO_USD}, got "
            f"{record.max_loss_micro_usd}"
        )
    if record.floor_equity_micro_usd != BUDGET_FLOOR_MICRO_USD:
        raise mba.AdapterError(
            "budget floor mismatch: expected "
            f"{BUDGET_FLOOR_MICRO_USD}, got "
            f"{record.floor_equity_micro_usd}"
        )
    S.budget_guard = guard
    S.budget_startup_ok = False
    return guard


def budget_accounting_contract_errors():
    """Return live-ignition blockers for unproven account-value semantics."""
    if BUDGET_ACCOUNTING_CONTRACT_VERIFIED:
        return []
    return [
        "MM_BUDGET_ACCOUNTING_CONTRACT_VERIFIED=1 is required only after "
        "a read-only three-state probe proves balance/portfolio_value and "
        "resting-collateral semantics for the bound account"
    ]


def _budget_fetch_all(base_path, list_key):
    """Fetch one complete cursor collection or raise without partial output."""
    rows = []
    cursor = None
    seen = set()
    pages = 0
    while True:
        path = base_path + (
            "&cursor=" + urllib.parse.quote(str(cursor), safe="")
            if cursor else ""
        )
        code, data = rest("GET", path)
        if code != 200:
            raise mba.AdapterError(
                f"{list_key} snapshot HTTP {code} on page {pages + 1}"
            )
        if not isinstance(data, dict):
            raise mba.AdapterError(f"{list_key} snapshot is not an object")
        page_rows = data.get(list_key)
        if not isinstance(page_rows, list):
            raise mba.AdapterError(
                f"{list_key} snapshot is missing its array"
            )
        rows.extend(page_rows)
        pages += 1
        cursor = data.get("cursor") or data.get("next_cursor")
        if not cursor:
            return rows
        if cursor in seen or pages >= 100:
            raise mba.AdapterError(f"{list_key} pagination cursor loop")
        seen.add(cursor)


def _budget_validate_open_orders(
        rows, *, orders, pending_new, unknown_orders):
    """Prove identity *and economics* for every active exchange order."""
    if not isinstance(rows, list):
        raise mba.AdapterError("orders snapshot must be a list")
    alias_to_local = {}
    owner_economics = {}
    resting_owner_by_id = {}
    resting_placed_ts = {}

    def alias(identity, owner):
        if identity is None:
            return
        if not isinstance(identity, str) or not identity:
            raise mba.AdapterError("local order identity is invalid")
        previous = alias_to_local.get(identity)
        if previous is not None and previous != owner:
            raise mba.AdapterError("local order identity collision")
        alias_to_local[identity] = owner

    def local_economics(owner, key, order):
        if (
            not isinstance(key, tuple)
            or len(key) != 2
            or key[1] not in ("bid", "ask_no")
        ):
            raise mba.AdapterError("local order key is invalid")
        ticker, engine_side = key
        if not isinstance(ticker, str) or not ticker:
            raise mba.AdapterError("local order ticker is invalid")
        required = ("qty", "px", "wire_side", "exchange_px")
        missing = [field for field in required if field not in order]
        if missing:
            raise mba.AdapterError(
                "local order economics missing " + ",".join(missing)
            )
        try:
            qty = Decimal(str(order["qty"]))
            risk_px = Decimal(str(order["px"]))
            wire_px = Decimal(str(order["exchange_px"]))
        except (InvalidOperation, ValueError):
            raise mba.AdapterError(
                "local order economics unparseable: "
                f"{key} qty={order.get('qty')!r} px={order.get('px')!r} "
                f"xpx={order.get('exchange_px')!r}") from None
        if (
            not qty.is_finite() or qty <= 0
            or not risk_px.is_finite() or not 0 < risk_px < 1
            or not wire_px.is_finite() or not 0 < wire_px < 1
        ):
            raise mba.AdapterError("local order economics outside range")
        expected_wire_side = "bid" if engine_side == "bid" else "ask"
        if order["wire_side"] != expected_wire_side:
            raise mba.AdapterError("local wire side conflicts with outcome")
        expected_wire_px = (
            risk_px if engine_side == "bid" else Decimal(1) - risk_px
        )
        if wire_px != expected_wire_px:
            raise mba.AdapterError("local wire/risk price conflict")
        economics = {
            "ticker": ticker,
            "engine_side": engine_side,
            "quantity": qty,
            "risk_px": risk_px,
            "wire_side": expected_wire_side,
            "wire_px": wire_px,
        }
        previous = owner_economics.get(owner)
        if previous is not None and previous != economics:
            raise mba.AdapterError("local owner economics conflict")
        owner_economics[owner] = economics

    for key, order in orders.items():
        if not isinstance(order, dict):
            raise mba.AdapterError("local resting order is not an object")
        oid = order.get("id")
        owner = ("orders", key)
        alias(oid, owner)
        alias(order.get("client_order_id"), owner)
        local_economics(owner, key, order)
        resting_owner_by_id[oid] = owner
        resting_placed_ts[oid] = float(order.get("t") or 0.0)
    for key, pending in pending_new.items():
        if not isinstance(pending, dict):
            raise mba.AdapterError("local pending order is not an object")
        owner = ("pending_new", key)
        alias(pending.get("client_order_id"), owner)
        alias(pending.get("order_id"), owner)
        local_economics(owner, key, pending)
    for oid, unknown in unknown_orders.items():
        # Cancel-unknown normally reclassifies the still-retained resting
        # reservation.  Preserve that one economic owner instead of treating
        # the same server ID in two namespaces as a second order.
        owner = alias_to_local.get(oid, ("unknown_orders", oid))
        alias(oid, owner)
        if isinstance(unknown, dict):
            alias(unknown.get("client_order_id"), owner)
            alias(unknown.get("order_id"), owner)
            if owner not in owner_economics:
                key = (unknown.get("ticker"), unknown.get("side"))
                shaped = {
                    # local_remaining is the reservation actually retained
                    # when a cancel ACK could not prove release.
                    "qty": (unknown.get("qty")
                            if unknown.get("qty") is not None
                            else unknown.get("local_remaining")),
                    # Unknown-order records vary by origin (cancel-unknown,
                    # IOC limbo); risk_px can be absent -- fall through the
                    # price aliases before letting the strict parser trip
                    # (2026-07-28T02:06 "economics unparseable" halt).
                    "px": (unknown.get("risk_px")
                           if unknown.get("risk_px") is not None
                           else unknown.get("px")
                           if unknown.get("px") is not None
                           else unknown.get("exchange_px")),
                    "wire_side": unknown.get("wire_side"),
                    "exchange_px": unknown.get("exchange_px"),
                }
                local_economics(owner, key, shaped)
        else:
            raise mba.AdapterError("local unknown order is not an object")

    active = []
    seen_remote_aliases = set()
    matched_resting_ids = set()
    for row in rows:
        if not isinstance(row, dict):
            raise mba.AdapterError("exchange order row is not an object")
        if type(row.get("remaining_count_fp")) is not str:
            raise mba.AdapterError(
                "exchange order requires remaining_count_fp string"
            )
        try:
            remaining = Decimal(row["remaining_count_fp"])
        except InvalidOperation:
            raise mba.AdapterError(
                "exchange order remaining_count_fp is unparseable"
            ) from None
        if not remaining.is_finite() or remaining <= 0:
            raise mba.AdapterError(
                "exchange order remaining_count must be positive"
            )
        if row.get("subaccount_number") != BUDGET_SUBACCOUNT:
            raise mba.AdapterError("exchange order subaccount mismatch")
        # Current production Get Orders rows omit exchange_index.  When a
        # future schema supplies it, cross-check it; absence is not ambiguous
        # because this endpoint is already scoped to subaccount 0.
        if (
            "exchange_index" in row
            and row["exchange_index"] != BUDGET_EXCHANGE_INDEX
        ):
            raise mba.AdapterError("exchange order exchange_index mismatch")
        identities = [
            value for value in (
                row.get("order_id") or row.get("id"),
                row.get("client_order_id"),
            )
            if isinstance(value, str) and value
        ]
        if not identities:
            raise mba.AdapterError("active exchange order has no identity")
        if any(identity in seen_remote_aliases for identity in identities):
            raise mba.AdapterError("duplicate active exchange order identity")
        seen_remote_aliases.update(identities)
        owners = {
            alias_to_local[identity]
            for identity in identities
            if identity in alias_to_local
        }
        if len(owners) != 1:
            now_m = time.monotonic()
            if not owners and any(
                    now_m - S.order_tombstones.get(i, -1e9) <= 10.0
                    for i in identities):
                # Our own order, released locally within the last 10s; the
                # exchange snapshot is momentarily stale.  Skipping is
                # fail-closed-safe: only identities WE released recently
                # qualify, anything foreign still trips below.
                L.w({"ev": "STALE_ORDER_SKIPPED",
                     "ids": identities[:2]})
                continue
            raise mba.AdapterError(
                "active exchange order has no unique local reservation"
            )
        owner = next(iter(owners))
        expected = owner_economics.get(owner)
        if expected is None:
            raise mba.AdapterError("local order economics are unavailable")
        if row.get("ticker") != expected["ticker"]:
            raise mba.AdapterError("exchange/local order ticker mismatch")
        if remaining > expected["quantity"]:
            # Read-replica race #14 (2026-07-28T01:51): a partial fill
            # shrinks the local reservation before the exchange's order
            # row catches up, so exchange "remaining" briefly EXCEEDS the
            # local quantity.  Our own fill inside the grace window
            # explains it; anything else still trips.
            mt_row = row.get("ticker")
            if (mt_row and time.monotonic()
                    - S.recent_fill_mono.get(mt_row, -1e9) <= 10.0):
                L.w({"ev": "FRESH_FILL_ORDER_ROW_SKIPPED",
                     "mt": mt_row})
                continue
            raise mba.AdapterError(
                "exchange remaining quantity exceeds local reservation"
            )
        status = row.get("status")
        if status != "resting":
            raise mba.AdapterError(
                "exchange order status is not resting"
            )
        outcome = row.get("outcome_side")
        book_side = row.get("book_side")
        if outcome not in ("yes", "no") or book_side not in ("bid", "ask"):
            raise mba.AdapterError(
                "exchange order direction fields are missing/unparseable"
            )
        remote_engine_side = "bid" if outcome == "yes" else "ask_no"
        book_engine_side = "bid" if book_side == "bid" else "ask_no"
        if (
            remote_engine_side != book_engine_side
            or remote_engine_side != expected["engine_side"]
        ):
            raise mba.AdapterError("exchange/local order direction mismatch")
        expected_action = "buy" if book_side == "bid" else "sell"
        if "action" in row and row["action"] != expected_action:
            raise mba.AdapterError("exchange/local order action mismatch")
        try:
            yes_px = Decimal(row["yes_price_dollars"])
            no_px = Decimal(row["no_price_dollars"])
        except (KeyError, InvalidOperation):
            raise mba.AdapterError(
                "exchange order price dollars missing/unparseable"
            ) from None
        if (
            not yes_px.is_finite() or not no_px.is_finite()
            or yes_px <= 0 or no_px <= 0
            or yes_px + no_px != Decimal(1)
        ):
            raise mba.AdapterError("exchange order price dollars invalid")
        if (
            yes_px != expected["wire_px"]
            or no_px != Decimal(1) - expected["wire_px"]
            or expected["risk_px"] != (
                yes_px if expected["engine_side"] == "bid" else no_px
            )
        ):
            raise mba.AdapterError("exchange/local order price mismatch")

        if owner[0] == "orders":
            oid = row.get("order_id") or row.get("id")
            if resting_owner_by_id.get(oid) != owner:
                # It may have matched by client ID, but resting engine orders
                # are server-ID keyed and must prove that exact identity.
                raise mba.AdapterError(
                    "exchange resting order server ID mismatch"
                )
            matched_resting_ids.add(oid)
        active.append(copy.deepcopy(row))

    missing = set(resting_owner_by_id) - matched_resting_ids
    if missing:
        # Symmetric propagation race to the fill tombstone (observed live
        # 2026-07-27T20:50, one order-ACK later): a JUST-placed order can be
        # missing from the exchange's read snapshot for a moment.  A local
        # order younger than the grace window is treated as propagation lag
        # (receipt, no trip); anything older that is absent is a genuine
        # divergence and still trips.
        now_w = time.time()
        fresh = {oid for oid in missing
                 if now_w - resting_placed_ts.get(oid, 0.0) <= 10.0}
        stale_missing = missing - fresh
        if fresh:
            L.w({"ev": "FRESH_ORDER_PENDING_SNAPSHOT",
                 "ids": sorted(fresh)[:3]})
        # Race #19 (2026-07-28T05:00): the mirror image on the way OUT.  At
        # market close the exchange voids resting orders instantly while
        # order_cancel needs seconds to PROVE expiry; the reservation is
        # rightly retained locally, so the guard must expect its absence.
        # Every cancel attempt stamps an order tombstone -- treat absence
        # with a fresh tombstone (<15s) as cancel-in-flight; a cancel that
        # cannot prove itself within the window still trips.
        now_m = time.monotonic()
        leaving = {oid for oid in stale_missing
                   if now_m - S.order_tombstones.get(oid, -1e9) <= 15.0}
        if leaving:
            L.w({"ev": "CANCELING_ORDER_PENDING_SNAPSHOT",
                 "ids": sorted(leaving)[:3]})
            stale_missing -= leaving
        if stale_missing:
            raise mba.AdapterError(
                "local resting order absent from exchange snapshot"
            )
    return active


def capture_budget_snapshot():
    """Acquire one complete generation-stable risk view."""
    generation_before = S.risk_generation
    local_net_pos = copy.deepcopy(S.net_pos)
    orders = copy.deepcopy(S.orders)
    pending_new = copy.deepcopy(S.pending_new)
    unknown_orders = copy.deepcopy(S.unknown_orders)
    flatten_pending = copy.deepcopy(S.flatten_pending)

    positions = _budget_fetch_all(
        "/portfolio/positions?count_filter=position"
        "&limit=1000&subaccount=0",
        "market_positions",
    )
    exchange_orders = _budget_fetch_all(
        "/portfolio/orders?status=resting&subaccount=0&limit=1000",
        "orders",
    )
    # Preserve the scoped RESTING rows for the trip callback even when strict
    # identity/economics reconciliation below fails.
    S.budget_remote_orders = copy.deepcopy([
        row for row in exchange_orders
        if (
            not isinstance(row, dict)
            or qty_of(row, "remaining_count") is None
            or qty_of(row, "remaining_count") > 1e-9
        )
    ])
    active_orders = _budget_validate_open_orders(
        exchange_orders,
        orders=orders,
        pending_new=pending_new,
        unknown_orders=unknown_orders,
    )
    S.budget_remote_orders = active_orders

    balance_started = time.time()
    # Read-replica race #4 (observed live 2026-07-27T22:30, one fill in):
    # right after an execution the balance endpoint can return a payload
    # whose balance_breakdown has not yet caught up with balance_dollars.
    # Bounded retry -- consistency is still REQUIRED, the exchange just
    # gets three chances 300ms apart to converge before we trip.
    balance_response = None
    for _attempt in range(3):
        code, balance_response = rest(
            "GET", "/portfolio/balance?subaccount=0")
        if code != 200:
            raise mba.AdapterError(f"balance snapshot HTTP {code}")
        try:
            mba.budget.parse_balance_response(balance_response)
            break
        except Exception:
            if _attempt == 2:
                break                      # let the strict parser trip
            time.sleep(0.3)
    balance_finished = time.time()
    timestamp_started = time.time()
    # Read-replica race #6 (2026-07-27T23:17): successive reads of
    # /exchange/user_data_timestamp can go BACKWARDS when different
    # replicas answer.  Monotonicity stays required -- the exchange just
    # gets three chances 300ms apart to serve a fresh replica before the
    # strict validator trips.
    user_data_timestamp_response = None
    for _attempt in range(3):
        code, user_data_timestamp_response = rest(
            "GET", "/exchange/user_data_timestamp"
        )
        if code != 200:
            raise mba.AdapterError(f"user data timestamp HTTP {code}")
        try:
            _as_of = mba.parse_user_data_timestamp(
                user_data_timestamp_response)
        except Exception:
            break                          # let the strict parser trip
        if (S.user_data_as_of_max is None
                or _as_of >= S.user_data_as_of_max):
            S.user_data_as_of_max = _as_of
            break
        if _attempt < 2:
            time.sleep(0.3)
    timestamp_finished = time.time()
    generation_after = S.risk_generation
    return {
        "snapshot_inputs": mba.SnapshotInputs(
            balance_response=balance_response,
            positions_response={"market_positions": positions},
            user_data_timestamp_response=user_data_timestamp_response,
            balance_request_started_s=balance_started,
            balance_response_finished_s=balance_finished,
            user_data_timestamp_request_started_s=timestamp_started,
            user_data_timestamp_response_finished_s=timestamp_finished,
            ledger_generation_before=generation_before,
            ledger_generation_after=generation_after,
            previous_balance_updated_ts=S.budget_previous_updated_ts,
            previous_user_data_as_of_s=(
                S.budget_previous_user_data_as_of_s
            ),
        ),
        "local_net_pos": local_net_pos,
        "recent_fill_markets": dict(S.recent_fill_mono),
        # Markets past close leave the OPEN set immediately: S.meta only
        # refreshes on the ~120s md rotation, and the 22:15 settlement trip
        # proved a just-settled market lingers there while the exchange
        # zeroes its position.
        "open_markets": {m for m, row in S.meta.items()
                         if row[1] > time.time()},
        "orders": orders,
        "pending_new": pending_new,
        "unknown_orders": unknown_orders,
        "flatten_pending": flatten_pending,
    }


def _budget_log_assessment(stage, result):
    S.budget_previous_updated_ts = result.snapshot.updated_ts
    S.budget_previous_user_data_as_of_s = result.user_data_as_of_s
    L.w({
        "ev": "BUDGET_ASSESS",
        "stage": stage,
        "allowed": result.allowed,
        "trip_required": result.trip_required,
        "reason": result.reason,
        "record_generation": result.record.generation,
        "risk_generation": S.risk_generation,
        "user_data_as_of_s": result.user_data_as_of_s,
        "baseline_safe_micro_usd":
            result.baseline.safe_equity_micro_usd,
        "projected_safe_micro_usd":
            result.projected.safe_equity_micro_usd,
        "floor_micro_usd": result.record.floor_equity_micro_usd,
        "resting_micro_usd":
            result.baseline.resting_cost_fee_micro_usd,
        "pending_micro_usd":
            result.baseline.pending_cost_fee_micro_usd,
        "unknown_micro_usd":
            result.baseline.unknown_cost_fee_micro_usd,
        "candidate_micro_usd":
            result.projected.candidate_cost_fee_micro_usd,
    })


def _budget_log_trip(stage, exc=None):
    receipt = (
        S.budget_guard.trip_receipt if S.budget_guard is not None else None
    )
    L.w({
        "ev": "BUDGET_TRIP",
        "stage": stage,
        "error": (
            f"{type(exc).__name__}: {exc}"[:200] if exc is not None else None
        ),
        "durable_latched": (
            receipt.durable_latched if receipt is not None else False
        ),
        "cancel_complete": (
            receipt.cancel_complete if receipt is not None else False
        ),
        "halt_confirmed": (
            receipt.halt_confirmed if receipt is not None else S.halted
        ),
        "complete": receipt.complete if receipt is not None else False,
    })


def budget_halt(reason):
    """BudgetGuard halt callback; a trip has no in-process resume path."""
    already = S.halted
    S.halted = True
    CTRL["paused"] = True
    S.control_error = f"absolute budget halt: {reason}"[:200]
    if not already:
        L.w({"ev": "BUDGET_HALT", "reason": str(reason)[:200]})
    write_control_status()
    return True


def budget_cancel_all(reason):
    """Cancel local reservations plus unmatched exchange orders seen by guard."""
    local_ids = {
        order.get("id") for order in S.orders.values()
        if isinstance(order, dict) and order.get("id")
    }
    complete = cancel_all(reason)
    unresolved_remote = []
    for row in list(S.budget_remote_orders):
        oid = row.get("order_id") or row.get("id")
        if oid in local_ids:
            if any(
                    order.get("id") == oid
                    for order in S.orders.values()):
                unresolved_remote.append(row)
            continue
        if not isinstance(oid, str) or not oid:
            unresolved_remote.append(row)
            complete = False
            continue
        code, _response = rest(
            "DELETE", f"/portfolio/events/orders/{oid}", host=V2O
        )
        if code not in (200, 201):
            unresolved_remote.append(row)
            complete = False
    S.budget_remote_orders = unresolved_remote
    return complete and not unresolved_remote


BUDGET_SNAPSHOT_MAX_FAILS = int(
    os.environ.get("MM_BUDGET_SNAPSHOT_MAX_FAILS", "3"))


def _budget_snapshot_or_trip(stage):
    if S.budget_guard is None:
        raise mba.AdapterError("budget guard is not initialized")
    try:
        snap = capture_budget_snapshot()
        S.budget_snapshot_fails = 0
        return snap
    except Exception as exc:
        # Race #18 (2026-07-28T04:46): ONE transient HTTP -1 on the 1 Hz
        # monitor durably latched the whole engine.  Mirror the recon
        # doctrine: the MONITOR line tolerates a bounded streak of
        # snapshot failures (3s of blindness) before declaring a durable
        # trip.  Startup/preflight stay fail-fast, and persistent
        # blindness still latches.
        if stage == "MONITOR_SNAPSHOT":
            S.budget_snapshot_fails += 1
            if S.budget_snapshot_fails < BUDGET_SNAPSHOT_MAX_FAILS:
                L.w({"ev": "BUDGET_SNAPSHOT_RETRY",
                     "fails": S.budget_snapshot_fails,
                     "max_fails": BUDGET_SNAPSHOT_MAX_FAILS,
                     "error": f"{type(exc).__name__}: {exc}"[:160]})
                return None
        # One latch = one trip.  The 1 Hz monitor re-tripping an already
        # latched, already halted guard on the identical error re-ran
        # cancel_all ~4x/second for 90s straight (2026-07-28T02:48 tape).
        err = f"{type(exc).__name__}: {exc}"[:200]
        already = (
            S.halted
            and S.budget_guard.record.latched
            and err == S.last_budget_trip_err
        )
        S.last_budget_trip_err = err
        if already:
            now_m = time.monotonic()
            if now_m - S.last_budget_trip_log >= 30.0:
                S.last_budget_trip_log = now_m
                _budget_log_trip(stage, exc)
            return None
        S.budget_guard.trip_blind(
            stage=stage, error=exc, reservations_clear=False
        )
        _budget_log_trip(stage, exc)
        return None


def budget_startup_once():
    """Run the budget ignition gate once before live quoting is possible."""
    kwargs = _budget_snapshot_or_trip("STARTUP_SNAPSHOT")
    if kwargs is None:
        S.budget_startup_ok = False
        return None
    try:
        result = S.budget_guard.startup_check(**kwargs)
    except Exception as exc:
        S.budget_startup_ok = False
        _budget_log_trip("STARTUP_ASSESS", exc)
        return None
    _budget_log_assessment("startup", result)
    S.budget_startup_ok = bool(result.allowed and not result.trip_required)
    return result


def budget_preflight_candidate(candidate):
    """Final generation-stable floor decision immediately before one POST."""
    if not BUDGET_ENFORCE:
        return True
    if S.budget_guard is None or not S.budget_startup_ok:
        budget_halt("budget ignition incomplete")
        return False
    kwargs = _budget_snapshot_or_trip("PREFLIGHT_SNAPSHOT")
    if kwargs is None:
        return False
    try:
        result = S.budget_guard.preflight(candidate=candidate, **kwargs)
    except Exception as exc:
        _budget_log_trip("PREFLIGHT_ASSESS", exc)
        return False
    _budget_log_assessment("preflight", result)
    return bool(result.allowed)


def budget_monitor_once():
    """One independent live budget check; blind state is a durable trip."""
    if not BUDGET_ENFORCE or MODE != "live":
        return None
    if S.budget_guard is None:
        budget_halt("budget guard missing")
        return None
    kwargs = _budget_snapshot_or_trip("MONITOR_SNAPSHOT")
    if kwargs is None:
        return None
    try:
        result = S.budget_guard.monitor_once(**kwargs)
    except Exception as exc:
        _budget_log_trip("MONITOR_ASSESS", exc)
        return None
    _budget_log_assessment("monitor", result)
    return result


async def budget_task():
    """Independent 1 Hz safety clock, unrelated to quotes/fills/market data."""
    while True:
        await asyncio.sleep(BUDGET_MONITOR_S)
        if MODE == "live" and BUDGET_ENFORCE:
            budget_monitor_once()


def zone_ok(tte, mid_c):
    """Time/price admission.

    History (E2, 610M contracts, 2026-07-25) said: >=600s all strikes;
    300-600s only |mid-50|>10; 120-300s tails only; <120s graveyard
    (measured -3.5c/contract).  Those bands were measured on the OLD engine
    -- no shield, no position management, no toxicity pricing -- and the
    2026-07-27 live session showed the cost of taking them literally:
    64 of 66 evaluations were rejected by the 300-600s mid-band rule while
    the book sat quietly at 49-50c (the best two-way market of the session),
    leaving only the extreme-deviation region open, which is precisely where
    a "bargain" means the index is mid-crash.

    The graveyard floor stays hard (it is a settlement-truth fact, not a
    strategy artefact).  The mid-band exclusion becomes configurable so the
    band can be re-measured under the current engine instead of inherited
    from a dead one.  MM_ZONE_MID_EXCLUDE=0 opens it.
    """
    if tte < ZONE_GRAVEYARD_S:
        return False
    if tte >= ZONE_ALL_S:
        return True
    if tte >= ZONE_MID_S:
        if ZONE_MID_EXCLUDE_C <= 0:
            return True
        return abs(mid_c - 50) > ZONE_MID_EXCLUDE_C
    if ZONE_TAIL_ONLY:
        return mid_c < ZONE_TAIL_LOW_C or mid_c > ZONE_TAIL_HIGH_C
    return True


def tail_cheap_entry_allowed(mid_c, quote_c):
    """True when this entry is admissible under the tail doctrine.

    Both arguments are CENTS.  Outside the tail bands every entry is
    allowed.  Inside them (mid below TAIL_LOW_C or above TAIL_HIGH_C)
    only the cheap side passes: the quote must cost less than half a
    contract, so the worst case is the premium paid rather than the
    short-tail payout that the measured jump process punishes.
    """
    if not TAIL_CHEAP_ONLY or mid_c is None or quote_c is None:
        return True
    if TAIL_LOW_C <= mid_c <= TAIL_HIGH_C:
        return True
    return float(quote_c) < TAIL_CHEAP_MAX_C


def reset_cf_session():
    """Reset connection-scoped sequence/avg state, preserving RTI history."""
    S.cf_sid = None
    S.cf_seq = None
    S.cf_stream_ok = False
    for series in SERIES:
        S.cf_session_ticks[series].clear()


def _strict_decimal_string(value, field):
    if type(value) is not str:
        raise ValueError(f"{field} is not a decimal string")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} is not decimal") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} is not finite positive")
    return parsed


def _strict_window(window, *, source_ms, field):
    if type(window) is not dict:
        raise ValueError(f"{field} is not an object")
    avg = _strict_decimal_string(window.get("value"), f"{field}.value")
    n = window.get("window_size")
    start_ms = window.get("window_start_ts_ms")
    end_ms = window.get("window_end_ts_exclusive")
    if type(n) is not int or type(start_ms) is not int or type(end_ms) is not int:
        raise ValueError(f"{field} integer field type")
    return avg, n, start_ms, end_ms


def _tail_json_lines(path, max_bytes=262_144):
    """Last lines of an NDJSON file as parsed dicts (partial first line
    discarded).  Cheap, allocation-bounded, no file handle kept open."""
    out = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            for raw in fh.read().decode("utf-8", "ignore").splitlines():
                try:
                    out.append(json.loads(raw))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return []
    return out


def anchor_refresh(now_s=None):
    """Refresh the anchor snapshot from the recorders' file tails.

    Keeps, per feed, the newest price and the price as of the last BRTI tick
    we priced from; their difference is the *unpriced* move the fair value
    has not seen yet.  Basis level never enters -- only the delta.
    """
    now_s = time.time() if now_s is None else now_s
    if now_s - S.anchor_last_refresh < ANCHOR_REFRESH_S:
        return
    S.anchor_last_refresh = now_s
    hour = time.strftime("%Y%m%dT%H", time.gmtime(now_s))
    prev_hour = time.strftime("%Y%m%dT%H", time.gmtime(now_s - 3600))
    for feed, prefix, extract in (
        ("bn", "perp_btcusdt_", _anchor_px_binance),
        ("cb", "coinbase_btcusd_", _anchor_px_coinbase),
    ):
        best_ts = None
        best_px = None
        for hh in (hour, prev_hour):
            path = os.path.join(ANCHOR_GLOB, f"{prefix}{hh}.ndjson")
            if not os.path.exists(path):
                continue
            for rec in reversed(_tail_json_lines(path)):
                got = extract(rec)
                if got is None:
                    continue
                ts_s, px = got
                if best_ts is None or ts_s > best_ts:
                    best_ts, best_px = ts_s, px
                break
            if best_ts is not None:
                break
        if best_ts is None:
            S.anchor[feed] = None
            continue
        S.anchor[feed] = {"ts": best_ts, "px": px_or(best_px)}
        hist = S.anchor_hist[feed]
        if px_or(best_px) is not None and (
                not hist or best_ts > hist[-1][0]):
            hist.append((best_ts, px_or(best_px)))


def px_or(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _anchor_px_binance(rec):
    try:
        bid = float(rec["b"])
        ask = float(rec["a"])
        return float(rec["T"]) / 1000.0, (bid + ask) / 2.0
    except (KeyError, TypeError, ValueError):
        return None


def _anchor_px_coinbase(rec):
    try:
        if rec.get("ty") != "ticker":
            return None
        return float(rec["recv_wall_ns"]) / 1e9, float(rec["p"])
    except (KeyError, TypeError, ValueError):
        return None


def anchor_mark(ser, now_s=None):
    """Latch the anchor prices that correspond to the fair value in force."""
    now_s = time.time() if now_s is None else now_s
    anchor_refresh(now_s)
    S.anchor_mark[ser] = {
        feed: (None if snap is None else snap["px"])
        for feed, snap in S.anchor.items()
    }


def anchor_unpriced_move(ser, now_s=None):
    """(delta_usd, detail) of index movement the current fair has not seen.

    dx = anchor(now) - anchor(now - ANCHOR_LAG_S), from a per-feed history
    buffer.  HISTORY, not a re-latched mark: the original implementation
    re-latched the mark to the CURRENT anchor price on every new BRTI tick,
    which zeroed out exactly the ~1s lead the anchor exists to exploit --
    observed live 2026-07-27T20:38 as anchor_dx pinned at 0.0 while Binance
    sat $35 off the index (the shield was blind for its whole first run).

    Returns (None, reason) when the anchor cannot be trusted: stale feed,
    short history, or no feed at all -> caller must not act on it.
    """
    now_s = time.time() if now_s is None else now_s
    anchor_refresh(now_s)
    votes = []
    detail = {}
    for feed, beta in (("bn", ANCHOR_BETA_BN), ("cb", ANCHOR_BETA_CB)):
        snap = S.anchor.get(feed)
        if snap is None or snap.get("px") is None:
            continue
        age = now_s - snap["ts"]
        if age > ANCHOR_MAX_AGE_S:
            detail[feed] = {"stale_age_s": round(age, 2)}
            continue
        hist = S.anchor_hist[feed]
        cutoff = now_s - ANCHOR_LAG_S
        ref = None
        for ts_h, px_h in reversed(hist):
            if ts_h <= cutoff:
                ref = px_h
                break
        if ref is None:
            # history shorter than the lag: usable only if it spans most
            # of the window, else no signal (fail closed)
            if hist and (snap["ts"] - hist[0][0]) >= 0.6 * ANCHOR_LAG_S:
                ref = hist[0][1]
            else:
                detail[feed] = {"short_history": len(hist)}
                continue
        move = snap["px"] - ref
        detail[feed] = {"move_usd": round(move, 2), "beta": beta,
                        "age_s": round(age, 2)}
        votes.append(beta * move)
    if not votes:
        return None, detail
    # Both feeds carry the same information (corr 0.78 same-second); average
    # their beta-scaled votes rather than double-counting the move.
    return sum(votes) / len(votes), detail


def _capture_source_ticks(paths):
    """Parse validated {series: {source_ms: Decimal}} out of recorder files.

    Shared by startup hydration and the live seam bridge; a partially
    written final line is expected and skipped, invalid complete lines are
    omitted, and the continuity gates downstream still fail closed.
    """
    by_series = {s: {} for s in SERIES}
    for path in paths:
        try:
            src = open(path, "r", encoding="utf-8")
        except OSError:
            continue
        with src:
            for line in src:
                try:
                    envelope = json.loads(line)
                    if envelope.get("kind") != "FRAME":
                        continue
                    frame = json.loads(envelope["payload"])
                    if frame.get("type") != "cfbenchmarks_value":
                        continue
                    msg = frame["msg"]
                    idx = msg["index_id"]
                    ser = next((s for s, ix in IDX.items() if ix == idx), None)
                    if ser is None:
                        continue
                    received_at = msg["received_at"]
                    raw_wire = msg["data"]
                    if type(received_at) is not int or type(raw_wire) is not str:
                        continue
                    raw = json.loads(raw_wire)
                    source_ms = raw["time"]
                    if (raw.get("type") != "value" or raw.get("id") != idx
                            or type(source_ms) is not int
                            or not 0 <= received_at - source_ms <= 5000):
                        continue
                    value_d = _strict_decimal_string(
                        raw.get("value"), "capture.data.value")
                    previous = by_series[ser].get(source_ms)
                    if previous is not None and previous != value_d:
                        raise ValueError("conflicting capture source tick")
                    by_series[ser][source_ms] = value_d
                except (KeyError, TypeError, ValueError,
                        json.JSONDecodeError):
                    continue
    return by_series


def hydrate_rti_from_capture(pattern=None, now_s=None):
    """Load recent BRTI source ticks from the independent 24/7 recorder.

    This removes the five-minute *process restart* warmup without weakening
    the continuity requirement.  The recorder is a separate data plane, so
    deploying or restarting the trading engine does not erase market history.
    """
    pattern = CF_CAPTURE_GLOB if pattern is None else pattern
    paths = glob.glob(pattern)
    if not paths:
        return 0
    try:
        paths = sorted(paths, key=os.path.getmtime)[-3:]
    except OSError:
        paths = sorted(paths)[-3:]
    by_series = _capture_source_ticks(paths)
    loaded = 0
    for ser, by_time in by_series.items():
        ordered = sorted(by_time.items())[-RTI_HISTORY_TICKS:]
        if not ordered:
            continue
        S.rti[ser].clear()
        S.rti_src_ms[ser].clear()
        S.rti[ser].extend(float(v) for _, v in ordered)
        S.rti_src_ms[ser].extend(t for t, _ in ordered)
        S.rti_t[ser] = (
            time.time() if now_s is None else float(now_s)
        )
        S.rti_lock[ser] = None
        S.rti_seam_from_ms[ser] = ordered[-1][0]
        S.rti_seam_attempts[ser] = 0
        loaded += len(ordered)
    S.cf_stream_ok = False
    if loaded:
        L.w({"ev": "RTI_HYDRATE", "ticks": loaded,
             "files": paths,
             "latest_source_ms": {
                 s: (S.rti_src_ms[s][-1] if S.rti_src_ms[s] else None)
                 for s in SERIES
             }})
    return loaded


def bridge_rti_seam(ser):
    """Splice recorder ticks into the hydrate-to-live seam (trip fix
    2026-07-28).

    Called after each accepted live tick while a seam is open.  The 300s
    sigma window tolerates only 0.3s of missing coverage, so the 1-3s hole
    between the last hydrated tick and the first live tick blinded pricing
    (and therefore ALL quoting) for a measured 300.8s per restart.  The
    recorder never stopped; the missing ticks exist on disk.  Splicing real
    recorded ticks keeps the continuity doctrine fully fail-closed: a tick
    genuinely absent from the recorder still leaves the gap, and after
    RTI_SEAM_MAX_ATTEMPTS we stop trying and let the sigma gate rule.
    """
    lo = S.rti_seam_from_ms.get(ser)
    if lo is None:
        return
    times = S.rti_src_ms[ser]
    values = S.rti[ser]
    hi = next((t for t in times if t > lo), None)
    if hi is None:
        return                      # no live tick landed yet
    if hi - lo <= 1100:             # 1Hz cadence: seam already continuous
        S.rti_seam_from_ms[ser] = None
        return
    attempts = S.rti_seam_attempts.get(ser, 0)
    if attempts >= RTI_SEAM_MAX_ATTEMPTS:
        L.w({"ev": "RTI_SEAM_GIVEUP", "series": ser, "lo_ms": lo,
             "hi_ms": hi, "attempts": attempts})
        S.rti_seam_from_ms[ser] = None
        return
    S.rti_seam_attempts[ser] = attempts + 1
    paths = glob.glob(CF_CAPTURE_GLOB)
    try:
        paths = sorted(paths, key=os.path.getmtime)[-2:]
    except OSError:
        paths = sorted(paths)[-2:]
    fresh = _capture_source_ticks(paths).get(ser, {})
    extra = [(t, float(v)) for t, v in fresh.items() if lo < t < hi]
    if not extra:
        return                      # recorder hasn't flushed them yet; retry
    merged = dict(zip(times, values))
    for t, v in extra:
        prev = merged.get(t)
        if prev is not None and abs(prev - v) > 1e-9:
            L.w({"ev": "RTI_SEAM_GIVEUP", "series": ser, "lo_ms": lo,
                 "hi_ms": hi, "reason": "conflicting tick", "at_ms": t})
            S.rti_seam_from_ms[ser] = None
            return
        merged[t] = v
    ordered = sorted(merged.items())[-RTI_HISTORY_TICKS:]
    times.clear()
    values.clear()
    times.extend(t for t, _ in ordered)
    values.extend(v for _, v in ordered)
    gap_ms = max(
        (b - a for a, b in zip(times, list(times)[1:]) if lo <= a <= hi),
        default=0,
    )
    closed = gap_ms <= 1100
    L.w({"ev": "RTI_SEAM_BRIDGED", "series": ser, "added": len(extra),
         "lo_ms": lo, "hi_ms": hi, "residual_gap_ms": gap_ms,
         "closed": closed})
    if closed:
        S.rti_seam_from_ms[ser] = None


def ingest_cf_value(frame, received_wall_s=None):
    """Strictly validate and atomically append one official CF frame.

    Returns ``True`` for an accepted tick, ``None`` for an exact idempotent
    duplicate, and ``False`` for a contract violation.  Production reconnects
    on ``False`` and leaves entry pricing invalid until a new stream is sound.
    """
    idx = None
    try:
        if type(frame) is not dict or frame.get("type") != "cfbenchmarks_value":
            raise ValueError("wrong frame type")
        sid = frame.get("sid")
        seq = frame.get("seq")
        msg = frame.get("msg")
        if type(sid) is not int or sid <= 0 or type(seq) is not int or seq <= 0:
            raise ValueError("invalid sid/seq")
        if type(msg) is not dict:
            raise ValueError("msg is not an object")
        idx = msg.get("index_id")
        ser = next((s for s, ix in IDX.items() if ix == idx), None)
        if ser is None:
            raise ValueError("unsubscribed index")
        received_at = msg.get("received_at")
        if type(received_at) is not int:
            raise ValueError("received_at is not integer ms")
        raw_wire = msg.get("data")
        if type(raw_wire) is not str:
            raise ValueError("data is not encoded JSON")
        raw = json.loads(raw_wire)
        if type(raw) is not dict:
            raise ValueError("decoded data is not object")
        if raw.get("type") != "value" or raw.get("id") != idx:
            raise ValueError("raw identity mismatch")
        source_ms = raw.get("time")
        if type(source_ms) is not int or source_ms <= 0:
            raise ValueError("source time is not positive integer ms")
        value_d = _strict_decimal_string(raw.get("value"), "data.value")
        if not 0 <= received_at - source_ms <= 5000:
            raise ValueError("source-to-received latency out of bounds")

        times = S.rti_src_ms[ser]
        values = S.rti[ser]
        history_duplicate = False
        if times and source_ms == times[-1]:
            same = abs(float(value_d) - values[-1]) <= 1e-9
            if same and seq == S.cf_seq:
                L.w({"ev": "CF_SOURCE_SKIP", "series": ser,
                     "source_ms": source_ms, "duplicate": True})
                return None
            if same and S.cf_seq is None:
                # First frame after hydration may be the recorder's newest
                # source tick.  Validate the full new-session contract, then
                # establish sequence state without counting it twice.
                history_duplicate = True
            else:
                raise ValueError("same source time with conflicting frame")
        if times and source_ms < times[-1]:
            raise ValueError("source timestamp regressed")
        if S.cf_sid is not None and sid != S.cf_sid:
            raise ValueError("sid changed without reconnect")
        if S.cf_seq is not None and seq != S.cf_seq + 1:
            raise ValueError("stream sequence gap/regression")

        avg60_d, avg60_n, avg60_start, avg60_end = _strict_window(
            msg.get("avg_60s_data"), source_ms=source_ms,
            field="avg_60s_data")
        if (not 0 <= avg60_n <= 60
                or avg60_start != source_ms - 60_000
                or avg60_end != source_ms):
            raise ValueError("invalid avg_60s_data window")
        session_prior = [
            (t, v) for t, v in S.cf_session_ticks[ser]
            if source_ms - 60_000 <= t < source_ms
        ]
        expected_n = min(60, len(session_prior))
        if avg60_n != expected_n:
            raise ValueError("avg_60s_data count mismatch")
        expected_avg = (
            sum((v for _, v in session_prior), Decimal(0)) / expected_n
            if expected_n else value_d
        )
        if abs(avg60_d - expected_avg) > Decimal("0.00000001"):
            raise ValueError("avg_60s_data mean mismatch")

        phase = source_ms % (15 * 60 * 1000)
        close_ms = (
            source_ms if phase == 0
            else source_ms + (900_000 - phase)
        )
        official_start_ms = close_ms - 60_000

        # This exchange field is intentionally NOT our settlement
        # accumulator.  Observed wire semantics are
        # (close-60s, current], including the close tick at phase 0.  The
        # contract settles on avg_60s_data's [close-60s, close) interval.
        # Keep validating the shifted field so a schema/semantic change still
        # fails closed, but build the official accumulator from raw RTI ticks.
        shifted_expected = phase == 0 or 841_000 <= phase <= 899_000
        shifted = msg.get("last_60s_windowed_average_15min")
        known = {
            int(t): Decimal(str(v))
            for t, v in zip(times, values)
        }
        known[source_ms] = value_d
        if shifted_expected:
            shifted_d, shifted_n, shifted_start, shifted_end = _strict_window(
                shifted, source_ms=source_ms,
                field="last_60s_windowed_average_15min")
            # Like avg_60s_data, this field's count is scoped to the current
            # subscription.  A reconnect at phase :14:29 legitimately starts
            # at n=1 even though the metadata window starts at phase :14:00.
            shifted_session = [
                v for t, v in S.cf_session_ticks[ser]
                if official_start_ms < t < source_ms
            ] + [value_d]
            expected_shifted_n = min(60, len(shifted_session))
            if (shifted_n != expected_shifted_n
                    or shifted_start != official_start_ms
                    or shifted_end != source_ms):
                raise ValueError(
                    "invalid shifted final-minute window "
                    f"n={shifted_n}/{expected_shifted_n} "
                    f"start={shifted_start}/{official_start_ms} "
                    f"end={shifted_end}/{source_ms}")
            observed_shifted = (
                sum(shifted_session[-shifted_n:], Decimal(0))
                / shifted_n
            )
            if (abs(shifted_d - observed_shifted)
                    > Decimal("0.00000001")):
                raise ValueError("shifted final-minute mean mismatch")
        elif shifted is not None:
            raise ValueError("shifted final-minute field outside valid phase")

        # Authoritative contract window:
        #   phase 840..899 -> progressively include phase 840 through current
        #   phase 0        -> retain the prior 60 ticks; exclude close tick
        new_lock = None
        if 840_000 <= phase <= 899_000:
            official_n = (phase - 840_000) // 1000 + 1
            official_times = [
                official_start_ms + i * 1000
                for i in range(official_n)
            ]
        elif phase == 0:
            official_n = 60
            official_times = [
                official_start_ms + i * 1000
                for i in range(official_n)
            ]
        else:
            official_n = 0
            official_times = []
        if official_times and all(t in known for t in official_times):
            official_sum = sum(
                (known[t] for t in official_times), Decimal(0))
            official_avg = official_sum / official_n
            # At the close, a fully warmed avg_60s_data is an independent
            # contract-window checksum.  After a reconnect its session count
            # can be smaller, while hydrated raw history remains usable.
            if (phase == 0 and avg60_n == 60
                    and abs(avg60_d - official_avg)
                    > Decimal("0.00000001")):
                raise ValueError("official final-minute mean mismatch")
            new_lock = {
                "sum": float(official_sum),
                "n": official_n,
                "close_ms": close_ms,
                "source_ms": source_ms,
                "avg": float(official_avg),
            }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        S.cf_stream_ok = False
        L.w({"ev": "CF_VALUE_REJ", "index_id": idx,
             "reason": str(e)[:160]})
        return False

    # All consumed fields have passed.  Commit the new state together.
    S.cf_sid = sid
    S.cf_seq = seq
    if not history_duplicate:
        values.append(float(value_d))
        times.append(source_ms)
    S.cf_session_ticks[ser].append((source_ms, value_d))
    S.rti_t[ser] = (
        time.time() if received_wall_s is None else float(received_wall_s)
    )
    S.rti_lock[ser] = new_lock
    S.cf_stream_ok = True
    if S.rti_seam_from_ms.get(ser) is not None:
        bridge_rti_seam(ser)
    return True


def pricing_state(ser, close_s, now_s=None):
    """Return a conservative, source-time pricing snapshot or ``None``.

    Both the 60s and 300s windows must be continuous.  We use their maximum
    sigma because the first captured live session showed that the short
    window becomes severely overconfident early in a 15-minute contract.
    """
    now_s = time.time() if now_s is None else float(now_s)
    if not S.cf_stream_ok:
        S.pricing_unready[ser] = "cf_stream_down"
        return None
    ticks = list(S.rti[ser])
    source_ms = list(S.rti_src_ms[ser])
    if not ticks or len(ticks) != len(source_ms):
        S.pricing_unready[ser] = "no_ticks"
        return None
    receipt_age_s = now_s - S.rti_t[ser]
    source_age_s = now_s - source_ms[-1] / 1000.0
    if (receipt_age_s > RTI_MAX_SOURCE_AGE_S or receipt_age_s < -2.0
            or source_age_s > RTI_MAX_SOURCE_AGE_S
            or source_age_s < -2.0):
        S.pricing_unready[ser] = (
            f"tick_stale r={receipt_age_s:.1f}s s={source_age_s:.1f}s")
        return None
    common = {
        "min_n": 30,
        "min_coverage": RTI_MIN_COVERAGE,
        "max_gap_s": RTI_MAX_GAP_S,
        "demean": False,
    }
    sigma_short = rp.sigma_from_timed_ticks(
        ticks, source_ms, window_s=RTI_SHORT_WINDOW_S, **common)
    sigma_long = rp.sigma_from_timed_ticks(
        ticks, source_ms, window_s=RTI_LONG_WINDOW_S, **common)
    if (sigma_short is None or sigma_short <= 0
            or sigma_long is None or sigma_long <= 0):
        S.pricing_unready[ser] = (
            f"sigma_window short={'ok' if sigma_short else 'FAIL'} "
            f"long={'ok' if sigma_long else 'FAIL'} "
            f"seam_open={S.rti_seam_from_ms.get(ser) is not None}")
        return None
    lock = S.rti_lock.get(ser)
    locked_sum = 0.0
    locked_n = 0
    if lock and abs(float(lock["close_ms"]) - close_s * 1000.0) <= 1.0:
        locked_sum = float(lock["sum"])
        locked_n = int(lock["n"])
    S.pricing_unready.pop(ser, None)
    return {
        "rti": ticks[-1],
        "source_ms": source_ms[-1],
        "sigma": max(sigma_short, sigma_long),
        # dP/dS is filled in by think() once the strike is known; keep the
        # key present so downstream code never sees a missing field.
        "dp_ds_c_per_usd": None,
        "sigma_short": sigma_short,
        "sigma_long": sigma_long,
        "model_tte": max(0.0, close_s - source_ms[-1] / 1000.0),
        "locked_sum": locked_sum,
        "locked_n": locked_n,
    }


def think():
    load_control()
    now = time.time()
    tox_settle_pending(now)
    if S.canary_done:
        if not S.halted:
            S.halted = True
            cancel_all("CANARY_MAX_CYCLES")
            L.w({"ev": "CANARY_DONE", "completed_cycles":
                 S.completed_cycles})
            write_control_status()
        return
    if CTRL.get("kill"):
        S.halted = True
    if CTRL.get("paused") or S.halted:
        if S.orders and now - S.last_control_cancel >= 1.0:
            cancel_all("CONTROL_ENFORCE")
            S.last_control_cancel = now
            write_control_status()
        return
    for mt, (ser, close_s, strike) in list(S.meta.items()):
        if ONLY_TICKER and mt != ONLY_TICKER:
            for side in ("bid", "ask_no"):
                od = S.orders.get((mt, side))
                if od and order_cancel(mt, side, od["id"],
                                       reason="ticker_allowlist"):
                    risk_map_pop(S.orders, (mt, side))
            continue
        tte = close_s - now
        if tte <= 0:
            for side in ("bid","ask_no"):
                od = S.orders.get((mt,side))
                if od and order_cancel(mt, side, od["id"],
                                       reason="expired"):
                    risk_map_pop(S.orders, (mt, side))
            S.meta.pop(mt, None); continue
        # An unresolved IOC is already the sole hedge for this market.
        # Re-posting a maker counterpart before its terminal fill state is
        # known can double-fill and create a fresh orphan in the other
        # direction.  Keep the market quote-dark and remove any stale maker
        # reservation until IOC reconciliation releases flatten_pending.
        if any(key[0] == mt for key in S.flatten_pending):
            for side in ("bid", "ask_no"):
                od = S.orders.get((mt, side))
                if od and order_cancel(
                        mt, side, od["id"],
                        reason="ioc_pending_no_double_hedge"):
                    risk_map_pop(S.orders, (mt, side))
            continue
        bk = S.books.get(mt)
        if not bk:
            bk = {"y": {}, "n": {}}
        yb, nb = best(bk["y"]), best(bk["n"])
        book_boot = False
        if yb is None or nb is None:
            # Fresh-listing bootstrap (2026-07-28): an empty book is not
            # "no market" -- it is a market NOBODY is making yet, and the
            # measured rollover blindness (90-110s every quarter hour) was
            # mostly us waiting for someone ELSE to quote first.  When our
            # pricing is ready, synthesize the missing side(s) one cent
            # outside fair and be the first maker; every downstream gate
            # (margin, tox, caps, budget) still applies.
            ps_b = pricing_state(ser, close_s, now_s=now)
            fair_b = None
            if ps_b is not None:
                p_b = rp.p_settle_above(
                    ps_b["rti"], strike, ps_b["sigma"], ps_b["model_tte"],
                    locked_sum=ps_b["locked_sum"],
                    locked_n=ps_b["locked_n"])
                if p_b is not None:
                    fair_b = 100.0 * p_b
            if fair_b is None or not (3.0 < fair_b < 97.0):
                if now - S.last_boot_skip.get(mt, 0.0) >= 30.0:
                    S.last_boot_skip[mt] = now
                    L.w({"ev": "BOOT_SKIP", "mt": mt,
                         "fair_b": (round(fair_b, 2)
                                    if fair_b is not None else None),
                         "ps_none": ps_b is None})
                continue
            book_boot = True
            if yb is None:
                yb = max(100, int(fair_b - 1.0) * 100)
            if nb is None:
                nb = max(100, int(100.0 - fair_b - 1.0) * 100)
        ya = 10000 - nb
        mid_c = (yb + ya) / 200.0
        y_touch = float(bk["y"].get(yb, 0.0) or 0.0)
        n_touch = float(bk["n"].get(nb, 0.0) or 0.0)
        touch_total = y_touch + n_touch
        touch_imbalance = (
            (y_touch - n_touch) / touch_total if touch_total > 0 else 0.0
        )
        ok = zone_ok(tte, mid_c)
        # Knife 3: entries also need enough runway to complete the pair;
        # exits/completions are governed separately and never gated here.
        ok_entry = ok and tte >= ENTRY_CUTOFF_TTE_S
        # Compute risk-reducing pair legs BEFORE the pricing gate.  A feed
        # reconnect, warmup, or sentinel disagreement may block entries but
        # must never strand inventory that can be closed at a bounded cost.
        want = {}
        np_ = S.net_pos.get(mt, {"y":0,"n":0,"cost":0.0})
        net = np_["y"] - np_["n"]
        ps_early = pricing_state(ser, close_s, now_s=now)
        if ps_early is not None:
            # dP/dS must exist BEFORE the sigma-scaled knobs are read, or
            # they silently fall back to the legacy fixed cents (observed
            # in shadow 2026-07-27T04:30: sigma_c=7.3c yet margin=2.5c).
            _p0 = rp.p_settle_above(
                ps_early["rti"], strike, ps_early["sigma"],
                ps_early["model_tte"], locked_sum=ps_early["locked_sum"],
                locked_n=ps_early["locked_n"])
            _p1 = rp.p_settle_above(
                ps_early["rti"] + 1.0, strike, ps_early["sigma"],
                ps_early["model_tte"], locked_sum=ps_early["locked_sum"],
                locked_n=ps_early["locked_n"])
            if _p0 is not None and _p1 is not None:
                ps_early["dp_ds_c_per_usd"] = 100.0 * (_p1 - _p0)
        if ps_early is not None and ps_early.get("sigma"):
            S.sigma_hist.append(float(ps_early["sigma"]))
        # Attribution: the re-opened 300-600s mid band overturns a
        # 610M-contract conclusion, so every admission it grants must be
        # countable or the change cannot be falsified.
        if (ZONE_MID_S <= tte < ZONE_ALL_S
                and abs(mid_c - 50) <= max(ZONE_MID_EXCLUDE_C, 10.0)):
            S.gate_counts["midband_admitted"] = (
                S.gate_counts.get("midband_admitted", 0) + 1)
        gamma_now = gamma_c_dyn(ps_early)
        margin_base_c = margin_c(ps_early)
        margin_now = margin_base_c
        y_px = skew_px(q_px(yb, ya), net, gamma_now)
        n_px = skew_px(q_px(10000-ya, 10000-yb), -net, gamma_now)
        # Per-side threshold: sigma-scaled base plus what history says
        # this exact (side, zone) has been costing us.
        _sc = sigma_contract_c(ps_early)
        # The sigma-scaled base already charges for generic adverse
        # selection; hand that to the table as redundancy so the same risk
        # is not billed twice.
        _redundant_c = max(0.0, margin_base_c - MARGIN_FLOOR_C)
        margin_bid_c = margin_base_c + tox_addon_c(
            'bid', y_px * 100.0, sigma_c=_sc, redundancy=_redundant_c)
        margin_no_c = margin_base_c + tox_addon_c(
            'ask_no', n_px * 100.0, sigma_c=_sc, redundancy=_redundant_c)
        exit_bid = exit_no = False
        if PAIR:
            y_px, n_px, exit_bid, exit_no = pair_push_prices(
                mt, y_px, n_px, yb, ya)
        pair_exit_active = exit_bid or exit_no
        prequote_sides = {
            side for side in ("bid", "ask_no")
            if (mt, side) in S.orders
        }
        prequote_flat = (
            not pair_exit_active
            and unpaired_ct(mt, "bid") <= 1e-9
            and unpaired_ct(mt, "ask_no") <= 1e-9
        )
        staged_pair = (
            PAIR and PAIR_PREQUOTE and prequote_flat
            and prequote_sides == {"bid", "ask_no"}
        )
        incomplete_stage = (
            PAIR and PAIR_PREQUOTE and prequote_flat
            and len(prequote_sides) == 1
        )
        if staged_pair:
            # A staged pair is one economic quote bundle.  Keep both original
            # prices (and thus both queue positions) until a hard gate closes
            # or one leg fills; do not chase every best-bid update.
            y_px = float(S.orders[(mt, "bid")]["px"])
            n_px = float(S.orders[(mt, "ask_no")]["px"])

        # F0 (2026-07-25): the kernel IS the side selector.  V6 on 101
        # real fills: kernel edge sign correct (YES +4.27c -> +30.09c,
        # NO -3.87c -> -25.81c) while this engine picked sides from
        # market mid / wind fair and bought the kernel-negative side.
        # The unvalidated wind layer (D6) is stripped; no kernel price,
        # no ENTRY quotes (fail closed).  Exact offsetting legs remain live.
        ps = ps_early
        if not pair_exit_active:
            if ps is not None:
                if S.last_entry_source_ms.get(mt) == ps["source_ms"]:
                    # Book deltas often arrive in bursts and temporarily move
                    # one side before its complement.  Re-deciding entries on
                    # every delta caused cancel/place storms and destroyed
                    # queue age.  One authoritative BRTI tick = one entry
                    # decision; pair exits remain book-reactive.
                    continue
                S.last_entry_source_ms[mt] = ps["source_ms"]
            elif not any(key[0] == mt for key in S.orders):
                # Silent starvation here hid a measured 300.8s post-restart
                # quoting blackout; the reason must be on the tape.
                if now - S.last_pricing_unready.get(mt, 0.0) >= 30.0:
                    S.last_pricing_unready[mt] = now
                    L.w({"ev": "PRICING_UNREADY", "mt": mt,
                         "reason": S.pricing_unready.get(ser, "unknown")})
                continue
        fair_c = None
        sigma = None
        p = None
        pricing_reason = "rti_unready"
        if ps is not None:
            sigma = ps["sigma"]
            p = rp.p_settle_above(
                ps["rti"], strike, sigma, ps["model_tte"],
                locked_sum=ps["locked_sum"], locked_n=ps["locked_n"])
            if p is not None:
                fair_c = 100.0 * p
                pricing_reason = "ready"
                # dP/dS in cents per index dollar: the shield's translation
                # from an index move into a contract-price move.  Numeric
                # derivative keeps it exact for both pricing regimes.
                p_up = rp.p_settle_above(
                    ps["rti"] + 1.0, strike, sigma, ps["model_tte"],
                    locked_sum=ps["locked_sum"], locked_n=ps["locked_n"])
                if p_up is not None:
                    ps["dp_ds_c_per_usd"] = 100.0 * (p_up - p)
                # (2026-07-27) The old per-BRTI-tick mark re-latch lived
                # here; it zeroed the anchor lead and blinded the shield.
                # The unpriced move now comes from the history buffer in
                # anchor_unpriced_move -- nothing to latch.
        sentinel = (
            fair_c is not None and abs(fair_c - mid_c) > SENTINEL_C
        )
        if sentinel:
            pricing_reason = "sentinel"
            if now - S.last_sentinel_log.get(mt, 0.0) >= 1.0:
                S.last_sentinel_log[mt] = now
                L.w({"ev":"SENTINEL_PAUSE","mt":mt,
                     "fv":round(fair_c,1),"mid":mid_c,
                     "rti": ps["rti"], "sigma": round(sigma, 6),
                     "sigma_short": round(ps["sigma_short"], 6),
                     "sigma_long": round(ps["sigma_long"], 6),
                     "tte": round(tte, 3),
                     "locked_n": ps["locked_n"]})

        # F0 side gate: only rest on a side that is CHEAP vs KERNEL fair.
        # RTI drops -> kernel fair drops -> bid side turns rich -> cancel
        # NOW (without waiting for the book); mirror for the NO side.
        edge_bid = None if fair_c is None else fair_c - y_px*100.0
        edge_no = (
            None if fair_c is None
            else (100.0 - fair_c) - n_px*100.0
        )
        entry_pricing_ok = fair_c is not None and not sentinel
        exp_now = exposure()                                   # A1 sync
        room = exp_now < MAX_OPEN_COST
        if exp_now >= MAX_OPEN_COST:                           # A6
            # Drain entry orders, but never veto the exact offsetting leg.
            for (omt, oside), od in list(S.orders.items()):
                opposite_inventory = (
                    unpaired_ct(omt, "ask_no") if oside == "bid"
                    else unpaired_ct(omt, "bid")
                )
                if opposite_inventory <= 1e-9:
                    if order_cancel(omt, oside, od["id"],
                                    reason="EXPOSURE_CAP"):
                        risk_map_pop(S.orders, (omt, oside))
            still_breached = exposure() > MAX_OPEN_COST
            if not S.limit_breached or still_breached != S.limit_breached:
                L.w({"ev":"LIMIT_BREACH","exposure":round(exposure(),2),
                     "max_open_cost":MAX_OPEN_COST,
                     "filled_exposure":still_breached})
            S.limit_breached = still_breached
            write_control_status()
            # Continue: pair exit candidates below are assessed by terminal
            # worst loss rather than gross spend.
        else:
            S.limit_breached = False
        # A5 projected inventory latch: the NEW clip itself may not cross the
        # limit.  Checking only current net allowed net=5, clip=2 through a
        # max_net=6 configuration.
        entry_qty = float(CLIP)
        skew_block_bid = net + entry_qty > MAX_NET + 1e-9
        skew_block_no = net - entry_qty < -MAX_NET - 1e-9
        cycle_gate_ok = False
        cycle_gate_reason = "legacy"
        if PAIR and PAIR_PREQUOTE and not pair_exit_active:
            if incomplete_stage:
                cycle_gate_reason = "incomplete_stage"
            elif staged_pair:
                staged_orders = (
                    S.orders[(mt, "bid")], S.orders[(mt, "ask_no")])
                staged_qty_ok = all(
                    abs(float(od.get("qty", 0.0)) - entry_qty) <= 1e-9
                    for od in staged_orders
                )
                staged_known = all(
                    od.get("id") not in S.unknown_orders
                    for od in staged_orders
                )
                staged_sum_ok = (
                    (y_px + n_px) * 100.0
                    <= PAIR_LOCK_C - PAIR_MIN_EDGE_C + 1e-9)
                cycle_gate_ok = (
                    entry_pricing_ok and ok_entry and staged_qty_ok
                    and staged_known and staged_sum_ok
                    and tail_cheap_entry_allowed(mid_c, y_px)
                    and tail_cheap_entry_allowed(mid_c, n_px)
                    and exposure() <= MAX_OPEN_COST + 1e-9
                    and not side_blocked(mt, "bid")
                    and not side_blocked(mt, "ask_no")
                )
                cycle_gate_reason = (
                    "staged_hold" if cycle_gate_ok else "staged_risk")
            elif prequote_sides:
                cycle_gate_reason = "unexpected_stage"
            else:
                cycle_gate_ok, cycle_gate_reason = cycle_admission_gate(
                    mt, y_px, n_px, y_touch, n_touch,
                    entry_pricing_ok=entry_pricing_ok,
                    zone=ok_entry, net=net)
            # This mode trades one complementary bundle, never a favorable
            # standalone leg.  If admission fails, BOTH entry wants are off.
            want["bid"] = cycle_gate_ok
            want["ask_no"] = cycle_gate_ok
            # Approval #1 accounting (staged-pair branch)
            S.side_evals += 2
            if cycle_gate_ok:
                S.side_wants += 2
            else:
                r = f"cycle_{cycle_gate_reason}"
                S.block_reasons[r] = S.block_reasons.get(r, 0) + 2
        else:
            # Exit legs (pair mode) bypass the kernel edge gate and the zone
            # gate: they REDUCE risk, and vetoing them is exactly how the
            # one-sided piles built up.  Legacy entries keep every gate.
            has_unpaired_yes = unpaired_ct(mt, "bid") > 1e-9
            has_unpaired_no = unpaired_ct(mt, "ask_no") > 1e-9
            cheap_bid_entry = tail_cheap_entry_allowed(mid_c, y_px * 100.0)
            cheap_no_entry = tail_cheap_entry_allowed(mid_c, n_px * 100.0)
            # Speed-directive item 3, SECOND HALF (2026-07-28): the
            # concurrent-cycle relaxation lived only in side_blocked; the
            # `not has_unpaired_opp` terms below still forced one-cycle-
            # at-a-time (measured: two-sided quoting 6% of evaluations).
            # Entries now stay open while inventory works out, bounded by
            # the net cap in side_blocked and order_place.  When a side is
            # the exit leg, the exit price takes priority over its entry.
            want["bid"] = (
                not side_blocked(mt, "bid") and y_px >= 0.001
                and (
                    (exit_bid if has_unpaired_no else False)
                    or (entry_pricing_ok and ok_entry and room
                        and cheap_bid_entry
                        and edge_bid >= margin_bid_c
                        and edge_within_cap(edge_bid, ps_early)
                        and not skew_block_bid)
                )
            )
            want["ask_no"] = (
                not side_blocked(mt, "ask_no") and n_px >= 0.001
                and (
                    (exit_no if has_unpaired_yes else False)
                    or (entry_pricing_ok and ok_entry and room
                        and cheap_no_entry
                        and edge_no >= margin_no_c
                        and edge_within_cap(edge_no, ps_early)
                        and not skew_block_no)
                )
            )
            # Approval #1 accounting: every evaluated side either quotes
            # or increments exactly one reason; beat() asserts
            #   sum(reasons) == side_evals - side_wants  with zero error.
            for sd, w_, px_, ex_, unp_, cheap_, edge_, marg_, skewb_ in (
                ("bid", want["bid"], y_px, exit_bid, has_unpaired_no,
                 cheap_bid_entry, edge_bid, margin_bid_c, skew_block_bid),
                ("ask_no", want["ask_no"], n_px, exit_no, has_unpaired_yes,
                 cheap_no_entry, edge_no, margin_no_c, skew_block_no),
            ):
                S.side_evals += 1
                if w_:
                    S.side_wants += 1
                    continue
                r = block_reason(
                    blocked=side_blocked(mt, sd), px=px_,
                    is_exit_path=ex_, has_unpaired_opp=unp_,
                    entry_pricing_ok=entry_pricing_ok, zone_ok_=ok_entry,
                    room=room, cheap_ok=cheap_,
                    edge=edge_, margin=marg_,
                    cap_ok=edge_within_cap(edge_, ps_early),
                    skew_block=skewb_)
                S.block_reasons[r] = S.block_reasons.get(r, 0) + 1
        # Entry hysteresis: zone and MARGIN_C decide whether to JOIN, not
        # whether an already-old queue position must be thrown away.  Keep a
        # resting entry while its own price still has non-negative model edge.
        # Sentinel/stale pricing, inventory latches, and an actual capital
        # breach still cancel immediately.
        if (not (PAIR and PAIR_PREQUOTE and not pair_exit_active)
                and entry_pricing_ok and ok_entry
                and exposure() <= MAX_OPEN_COST + 1e-9):
            for held_side in ("bid", "ask_no"):
                od = S.orders.get((mt, held_side))
                held_is_exit = (
                    exit_bid if held_side == "bid" else exit_no
                )
                if od is None or held_is_exit or side_blocked(mt, held_side):
                    continue
                held_edge = (
                    fair_c - float(od["px"]) * 100.0
                    if held_side == "bid"
                    else (100.0 - fair_c) - float(od["px"]) * 100.0
                )
                if held_edge >= 0.0:
                    want[held_side] = True
        # Position re-evaluation (operator ruling 2026-07-27: p is a
        # function of state, not a number fixed at fill time).  Every event
        # re-marks residual inventory against the CURRENT fair and emits a
        # receipt; the exit ORDER itself is the pair/exit-leg machinery
        # below, so this stays decision-only and cannot double-send.
        pos_view = None
        if fair_c is not None:
            S.last_fair_c[mt] = fair_c
        # Stale-intent leak (2026-07-28T00:00, convicted by the placement
        # lamp): position_reval returns None once the lot is gone, and the
        # old code only touched exit_intent inside the non-None branch --
        # a dead intent then capped the NEXT entry to 0.1c forever.  A flat
        # market clears its intent unconditionally.
        d_np = S.net_pos.get(mt)
        if (d_np is None
                or abs(float(d_np["y"]) - float(d_np["n"])) < 1e-9):
            S.exit_intent.pop(mt, None)
            # Compact per-market snapshot so a FILL receipt can carry the
            # full shape of the moment it happened, instead of relying on a
            # fragile 1s join against QUOTE_EVAL at analysis time.  Sample
            # shape is the raw material of every toxicity estimate; a fill
            # without its context is a wasted observation.
            S.last_eval_state[mt] = {
                "fair_c": round(fair_c, 3),
                "mid_c": (round(mid_c, 2) if mid_c is not None else None),
                # Point-in-time volatility block (operator directive
                # 2026-07-28: every trade carries its PIT indicators).
                "sigma": (round(ps["sigma"], 4)
                          if ps and ps.get("sigma") is not None else None),
                "sigma_short": (round(ps["sigma_short"], 4)
                                if ps and ps.get("sigma_short") is not None
                                else None),
                "sigma_long": (round(ps["sigma_long"], 4)
                               if ps and ps.get("sigma_long") is not None
                               else None),
                "sigma_c": (round(sigma_contract_c(ps), 3)
                            if ps and sigma_contract_c(ps) is not None
                            else None),
                "dp_ds": (round(ps["dp_ds_c_per_usd"], 4)
                          if ps and ps.get("dp_ds_c_per_usd") is not None
                          else None),
                "rti": (ps.get("rti") if ps else None),
                "spread_c": (round((ya - yb) / 100.0, 2)
                             if (ya is not None and yb is not None)
                             else None),
                "tte": round(tte, 1),
                "yb": yb, "ya": ya,
                "y_touch_ct": round(y_touch, 2),
                "n_touch_ct": round(n_touch, 2),
                "touch_imbalance": round(touch_imbalance, 4),
                "net_pos": net,
                "margin_bid_c": round(margin_bid_c, 3),
                "margin_no_c": round(margin_no_c, 3),
                "ev_density_bid": (
                    round(edge_bid / max(y_px * (tte / 60.0), 1e-6), 3)
                    if (edge_bid is not None and y_px > 0) else None),
                "ev_density_no": (
                    round(edge_no / max(n_px * (tte / 60.0), 1e-6), 3)
                    if (edge_no is not None and n_px > 0) else None),
                "anchor_mark": getattr(S, "anchor_mark", None),
            }
        if POS_MANAGE and fair_c is not None:
            # The bid that would lift OUR leg: a long-YES lot is sold into
            # the YES bid; a long-NO lot is sold into the NO bid.
            resid_now = (S.net_pos.get(mt, {"y": 0.0, "n": 0.0})["y"]
                         - S.net_pos.get(mt, {"y": 0.0, "n": 0.0})["n"])
            leg_bid_c = (yb / 100.0 if resid_now > 0
                         else (10000 - ya) / 100.0)
            pos_view = position_reval(mt, fair_c, ps, best_bid_c=leg_bid_c)
            if pos_view is not None:
                last = S.last_pos_log.get(mt, 0.0)
                prev_action = S.last_pos_action.get(mt)
                # Log on action CHANGE or every 5s -- a persistent non-hold
                # state must not write hundreds of identical lines (observed
                # 2026-07-27: ~700 identical sell_rich receipts in minutes).
                if (pos_view["action"] != prev_action or now - last >= 5.0):
                    S.last_pos_log[mt] = now
                    S.last_pos_action[mt] = pos_view["action"]
                    L.w({"ev": "POSITION_REVAL", "mt": mt,
                         "tte": round(tte, 1), **pos_view})
                # Approval #5: the reval verdict DRIVES the exit leg.  The
                # intent is state the pair-push reads every tick, so the
                # exit order re-pins to max(fair+thr, book) on every
                # re-evaluation, throttle-exempt like every exit leg.
                if pos_view["action"] in ("sell_rich", "sell_cost"):
                    prev_int = S.exit_intent.get(mt)
                    S.exit_intent[mt] = {
                        "side": pos_view["side"],
                        "target_c": pos_view["exit_target_c"],
                        "action": pos_view["action"], "ts": now}
                    if (prev_int is None
                            or prev_int.get("action")
                            != pos_view["action"]):
                        L.w({"ev": "EXIT_INTENT", "mt": mt,
                             "side": pos_view["side"],
                             "target_c": pos_view["exit_target_c"],
                             "trigger": pos_view["action"]})
                else:
                    S.exit_intent.pop(mt, None)

        # Fast-anchor shield (A-stage, defence only): a resting entry whose
        # own remaining edge is already exceeded by the unpriced anchor move
        # against it is stale by the time the tape reaches it -- pull it.
        # Exit legs are never shielded (cancelling them traps inventory).
        # Parameterless: the threshold IS that quote's remaining edge.
        shield_dx = None
        shield_detail = None
        if ANCHOR_SHIELD and fair_c is not None and ps is not None:
            shield_dx, shield_detail = anchor_unpriced_move(ser, now)
            dpds = ps.get("dp_ds_c_per_usd")
            if shield_dx is not None and dpds:
                pred_c = shield_dx * dpds       # predicted fair move, cents
                for sh_side in ("bid", "ask_no"):
                    od = S.orders.get((mt, sh_side))
                    if od is None:
                        continue
                    # Exit-leg immunity by SEMANTICS, not by this tick's
                    # flag: exit_bid/exit_no can flip False transiently
                    # (budget hiccup) while the RESTING order is still the
                    # complement of live inventory -- the shield then killed
                    # a pair leg (2026-07-27T23:46, reason=anchor_shield on
                    # a risk_reducing bid).  An order facing opposite
                    # unpaired lots IS an exit leg, whatever the flag says.
                    if PAIR and unpaired_ct(
                            mt, "ask_no" if sh_side == "bid"
                            else "bid") > 0:
                        continue
                    if PAIR and (exit_bid if sh_side == "bid" else exit_no):
                        continue
                    adverse_c = -pred_c if sh_side == "bid" else pred_c
                    if adverse_c <= 0.0:
                        continue
                    rest_edge_c = (
                        fair_c - float(od["px"]) * 100.0
                        if sh_side == "bid"
                        else (100.0 - fair_c) - float(od["px"]) * 100.0
                    )
                    if adverse_c > max(rest_edge_c, 0.0):
                        if order_cancel(mt, sh_side, od["id"],
                                        reason="anchor_shield"):
                            risk_map_pop(S.orders, (mt, sh_side))
                            want[sh_side] = False
                            S.gate_counts["anchor_pull"] = (
                                S.gate_counts.get("anchor_pull", 0) + 1)
                            L.w({"ev": "ANCHOR_PULL", "mt": mt,
                                 "side": sh_side,
                                 "pred_move_c": round(pred_c, 3),
                                 "adverse_c": round(adverse_c, 3),
                                 "rest_edge_c": round(rest_edge_c, 3),
                                 "anchor": shield_detail})
        # Calibration telemetry: EVERY evaluation leaves a receipt (1/s
        # per market), including the times we choose NOT to quote — the
        # live probe's primary product is evidence, not dollars.
        if now - S.last_eval.get(mt, 0.0) >= 1.0:
            S.last_eval[mt] = now
            flow = pair_flow_stats(
                mt, y_px, n_px,
                ps["source_ms"] if ps else int(now * 1000))
            S.evals_since_unpause += 1
            S.last_eval_mono = time.monotonic()
            L.w({"ev": "QUOTE_EVAL", "mt": mt, "tte": round(tte, 1),
                 "fair_c": (round(fair_c, 2)
                            if fair_c is not None else None),
                 "mid_c": round(mid_c, 2),
                 "sigma": (round(sigma, 4)
                           if sigma is not None else None),
                 "sigma_short": (
                     round(ps["sigma_short"], 4) if ps else None),
                 "sigma_long": (
                     round(ps["sigma_long"], 4) if ps else None),
                 "rti": ps["rti"] if ps else None,
                 "source_ms": ps["source_ms"] if ps else None,
                 "locked_n": ps["locked_n"] if ps else 0,
                 "sigma_c": (round(sigma_contract_c(ps), 4)
                             if sigma_contract_c(ps) is not None else None),
                 "margin_c_dyn": round(margin_base_c, 3),
                 "margin_bid_c": round(margin_bid_c, 3),
                 "margin_no_c": round(margin_no_c, 3),
                 "tox_buckets": len(S.tox),
                 "gamma_c_dyn": round(gamma_now, 3),
                 "dp_ds": (round(ps["dp_ds_c_per_usd"], 4)
                           if ps and ps.get("dp_ds_c_per_usd") is not None
                           else None),
                 "anchor_dx_usd": (round(shield_dx, 2)
                                   if shield_dx is not None else None),
                 "anchor": shield_detail,
                 "pricing_reason": pricing_reason,
                 "yb": yb, "ya": ya, "y_px": y_px, "n_px": n_px,
                 "spread_c": round((ya - yb) / 100.0, 3),
                 "y_touch_ct": round(y_touch, 4),
                 "n_touch_ct": round(n_touch, 4),
                 "touch_imbalance": round(touch_imbalance, 6),
                 "pair_quote_sum_c": round((y_px + n_px) * 100.0, 3),
                 "pair_prequote": bool(PAIR and PAIR_PREQUOTE),
                 "pair_min_depth_ct": PAIR_MIN_DEPTH_CT,
                 "cycle_gate_ok": cycle_gate_ok,
                 "cycle_gate_reason": cycle_gate_reason,
                 "staged_pair": staged_pair,
                 "bundle_cost": round(entry_qty * (y_px + n_px), 6),
                 "bid_order_age_s": (
                     round(now - S.orders[(mt, "bid")]["t"], 3)
                     if (mt, "bid") in S.orders else None),
                 "no_order_age_s": (
                     round(now - S.orders[(mt, "ask_no")]["t"], 3)
                     if (mt, "ask_no") in S.orders else None),
                 "y_flow_10s": round(flow["y_flow_10s"], 4),
                 "n_flow_10s": round(flow["n_flow_10s"], 4),
                 "y_flow_30s": round(flow["y_flow_30s"], 4),
                 "n_flow_30s": round(flow["n_flow_30s"], 4),
                 "y_flow_60s": round(flow["y_flow_60s"], 4),
                 "n_flow_60s": round(flow["n_flow_60s"], 4),
                 "y_queue_ahead": round(flow["y_queue_ahead"], 4),
                 "n_queue_ahead": round(flow["n_queue_ahead"], 4),
                 "y_clear_eta_s": (
                     round(flow["y_clear_eta_s"], 3)
                     if flow["y_clear_eta_s"] is not None else None),
                 "n_clear_eta_s": (
                     round(flow["n_clear_eta_s"], 3)
                     if flow["n_clear_eta_s"] is not None else None),
                 "edge_bid": (round(edge_bid, 2)
                              if edge_bid is not None else None),
                 "edge_no": (round(edge_no, 2)
                              if edge_no is not None else None),
                 "tail_cheap_only": TAIL_CHEAP_ONLY,
            "gates": dict(S.gate_counts),
                 "tail_bid_entry_allowed": tail_cheap_entry_allowed(
                     mid_c, y_px),
                 "tail_no_entry_allowed": tail_cheap_entry_allowed(
                     mid_c, n_px),
                 "net": net,
                 # Stage-0 #7a (2026-07-28): EV density telemetry.  Pure
                 # recording -- the capital allocator's admission threshold
                 # will be READ from this column's distribution, not set by
                 # a human.  edge cents per dollar-minute of capital lock;
                 # lock proxy = tte (settlement worst case).
                 "ev_density_bid": (
                     round(edge_bid / max(y_px * (tte / 60.0), 1e-6), 3)
                     if (edge_bid is not None and y_px > 0) else None),
                 "ev_density_no": (
                     round(edge_no / max(n_px * (tte / 60.0), 1e-6), 3)
                     if (edge_no is not None and n_px > 0) else None),
                 "want_bid": want["bid"], "want_no": want["ask_no"],
                 "zone_ok": ok, "book_boot": book_boot,
                 "exit_bid": exit_bid, "exit_no": exit_no})
        for side, w, px in (("bid", want["bid"], y_px), ("ask_no", want["ask_no"], n_px)):
            od = S.orders.get((mt, side))
            # Approval #1 assertion B bookkeeping: a wanted side with no
            # resting order and no pending placement starts (or continues)
            # its unserved clock; anything else clears it.  survival_check()
            # alarms when the clock passes 2s.
            key = (mt, side)
            if w and od is None and key not in S.pending_new:
                S.want_unmet.setdefault(key, time.monotonic())
            else:
                S.want_unmet.pop(key, None)
            exit_leg = exit_bid if side == "bid" else exit_no
            target_qty = (
                unpaired_ct(mt, "ask_no") if side == "bid"
                else unpaired_ct(mt, "bid")
            ) if exit_leg else float(CLIP)
            qty_changed = (
                od is not None and
                abs(float(od.get("qty", CLIP)) - target_qty) > 1e-9
            )
            old_model_edge = None
            if od is not None and fair_c is not None:
                old_model_edge = (
                    fair_c - float(od["px"]) * 100.0
                    if side == "bid"
                    else (100.0 - fair_c) - float(od["px"]) * 100.0
                )
            # Deadman renewal: replace a healthy order well before its
            # exchange-side expiry so the TTL only ever fires when the
            # engine is dead.  Renewal outranks every churn suppressor.
            ttl_renew = (od is not None and ORDER_TTL_S > 0
                         and od.get("expire_ts") is not None
                         and time.time() >= float(od["expire_ts"]) - 15.0)
            if (od and w and not exit_leg and not qty_changed
                    and not ttl_renew
                    and old_model_edge is not None
                    and old_model_edge >= 0.0
                    and now - float(od.get("t", now)) < MIN_QUOTE_AGE_S
                    and abs(float(od["px"]) - px) >= 0.001):
                S.requote_suppressed += 1
                continue
            if (od and w and not exit_leg and not qty_changed
                    and not ttl_renew
                    and 0.001 <= abs(od["px"]-px)
                    < MIN_REQUOTE_C/100.0):
                S.requote_suppressed += 1          # F5: hold, don't churn
                continue
            if od and (not w or qty_changed or ttl_renew
                       or abs(od["px"]-px) >= 0.001):
                if (now - S.last_replace.get((mt,side),0) < 1.0
                        and w and not exit_leg
                        and not qty_changed and not ttl_renew):
                    # Operator speed directive 2026-07-27: the pair chase
                    # leg (locks profit, cannot add exposure) is exempt
                    # from every requote throttle; entries keep them all.
                    continue
                if order_cancel(mt, side, od["id"],
                                reason=("ttl_renew" if ttl_renew else
                                        "resize" if qty_changed else
                                        "reprice" if w else "risk_off")):
                    risk_map_pop(S.orders, (mt,side))
                    S.last_replace[(mt,side)] = now
                    od = None
                else:
                    L.w({"ev":"CANCEL_RETRY_HOLD","mt":mt,"side":side})
                    continue
            if od is None and w:
                assert (mt, side) not in S.orders, "A2 violated"
                if not exit_leg and not within_cap(px, target_qty):
                    L.w({"ev":"CAP_SKIP","mt":mt,"side":side,"px":px,
                         "qty":target_qty,"exposure":round(exposure(),4),
                         "max_open_cost":MAX_OPEN_COST})
                    continue
                if side == "bid":
                    placed = order_place(
                        mt, "bid", px, risk_px=px,
                        edge_c=(round(edge_bid, 2)
                                if edge_bid is not None else None),
                        quantity=target_qty,
                        risk_reducing=exit_leg, slot_side=side)
                else:   # buy NO == rest an ask on the YES book at 1-no_px
                    placed = order_place(
                        mt, "ask", round(1.0 - px, 4), risk_px=px,
                        edge_c=(round(edge_no, 2)
                                if edge_no is not None else None),
                        quantity=target_qty,
                        risk_reducing=exit_leg, slot_side=side
                    )
                if not placed:
                    # Assertion-B lamp: every declined placement must say
                    # so.  order_place logs its own reject reasons; this
                    # covers any path that returns None silently.
                    L.w({"ev": "PLACE_RETURNED_NONE", "mt": mt,
                         "side": side, "px": px, "qty": target_qty,
                         "exit_leg": exit_leg})
                if placed:
                    oid, quantity = placed
                    # Live order_place already performs the atomic
                    # pending->resting transfer before returning.  Shadow has
                    # no HTTP ambiguity and creates its simulated slot here.
                    order_state = S.orders.get((mt, side))
                    if order_state is None:
                        order_state = {
                            "id":oid, "px":px, "qty":quantity, "t":now
                        }
                        risk_map_set(S.orders, (mt, side), order_state)
                    if MODE == "shadow" and SHADOW_QUEUE_SIM:
                        book_key = "y" if side == "bid" else "n"
                        level = int(round(float(px) * 10000.0))
                        order_state["ahead"] = float(
                            bk[book_key].get(level, 0.0) or 0.0)
        if (PAIR and PAIR_PREQUOTE and not pair_exit_active
                and unpaired_ct(mt, "bid") <= 1e-9
                and unpaired_ct(mt, "ask_no") <= 1e-9):
            staged_keys = [
                (mt, side) for side in ("bid", "ask_no")
                if (mt, side) in S.orders
            ]
            if len(staged_keys) == 1:
                key = staged_keys[0]
                od = S.orders[key]
                if order_cancel(
                        mt, key[1], od["id"],
                        reason="PAIR_BUNDLE_INCOMPLETE"):
                    risk_map_pop(S.orders, key)
                L.w({"ev": "PAIR_BUNDLE_ABORT", "mt": mt,
                     "remaining_side": key[1],
                     "cycle_gate_reason": cycle_gate_reason})

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
                reset_cf_session()
                await ws.send(json.dumps({"id":1,"cmd":"subscribe","params":{
                    "channels":["cfbenchmarks_value"],
                    "index_ids":sorted(set(IDX.values()))}}))
                async for raw in ws:
                    m = json.loads(raw)
                    if m.get("type") != "cfbenchmarks_value": continue
                    accepted = ingest_cf_value(m)
                    if accepted is True:
                        think()
                    elif accepted is False:
                        # One malformed consumed field invalidates entry
                        # pricing immediately.  ``think`` drains entry orders
                        # while preserving exact risk-reducing pair legs, then
                        # reconnect to obtain a fresh sequence baseline.
                        think()
                        raise RuntimeError("CF stream contract violation")
        except Exception as e:
            S.cf_stream_ok = False
            think()
            L.w({"ev":"CF_ERR","err":repr(e)[:150]})
            await asyncio.sleep(2)

def fetch_open():
    out = {}
    import datetime as dtm
    for ser in SERIES:
        code, d = rest("GET", f"/markets?series_ticker={ser}&status=open&limit=100")
        for mk in (d.get("markets") or []):
            try:
                if ONLY_TICKER and mk.get("ticker") != ONLY_TICKER:
                    continue
                cs = dtm.datetime.fromisoformat(mk["close_time"].replace("Z","+00:00")).timestamp()
                out[mk["ticker"]] = (ser, cs, float(mk["floor_strike"]))
            except Exception: pass
    return out

MD_SESSION_S = 120.0        # rotate subscription (re-fetch open markets)
MD_RECV_TIMEOUT_S = 15.0    # silent-channel watchdog


def _shadow_apply_queue_trade(
        mt, maker_side_hit, yes_trade_px, trade_qty, ts_ms, trade_id):
    """Pessimistic price-time queue simulation for a shadow resting order.

    Displayed size at placement is treated as entirely ahead of us and only
    executable trades reduce it; cancellations never improve our position.
    A trade strictly through our price implies a full fill by price priority.
    This is evidence generation only and is impossible to enable in live.
    """
    if not SHADOW_QUEUE_SIM or MODE != "shadow":
        return
    key = (mt, maker_side_hit)
    od = S.orders.get(key)
    if od is None or "ahead" not in od:
        return
    order_px = float(od["px"])
    trade_side_px = (
        float(yes_trade_px) if maker_side_hit == "bid"
        else 1.0 - float(yes_trade_px)
    )
    if trade_side_px > order_px + 1e-12:
        return
    remaining = float(od.get("qty", 0.0))
    if remaining <= 1e-9:
        return
    old_ahead = max(0.0, float(od.get("ahead", 0.0)))
    if trade_side_px < order_px - 1e-12:
        fill_qty = remaining
        od["ahead"] = 0.0
    else:
        available = max(0.0, float(trade_qty) - old_ahead)
        od["ahead"] = max(0.0, old_ahead - float(trade_qty))
        fill_qty = min(remaining, available)
    if fill_qty <= 1e-9:
        return
    outcome = "yes" if maker_side_hit == "bid" else "no"
    yes_px = order_px if outcome == "yes" else 1.0 - order_px
    sim_id = f"shadow:{trade_id}:{od.get('id')}:{remaining:.8f}"
    L.w({"ev": "SHADOW_FILL_SIM", "mt": mt, "side": maker_side_hit,
         "qty": round(fill_qty, 8), "px": order_px,
         "queue_ahead_before": round(old_ahead, 8),
         "trade_qty": round(float(trade_qty), 8),
         "through": trade_side_px < order_px - 1e-12,
         "order_id": od.get("id"), "trade_id": trade_id})
    apply_fill({
        "fill_id": sim_id,
        "trade_id": sim_id,
        "order_id": od.get("id"),
        "ticker": mt,
        "outcome_side": outcome,
        "book_side": "bid" if outcome == "yes" else "ask",
        "side": outcome,
        "count_fp": f"{fill_qty:.8f}",
        "yes_price_dollars": f"{yes_px:.4f}",
        "no_price_dollars": f"{1.0 - yes_px:.4f}",
        "created_ts": int(ts_ms),
        "fee_cost": 0.0,
    })


def apply_public_trade(msg):
    """Strictly add one public trade to the side-specific flow tape."""
    try:
        if type(msg) is not dict:
            raise ValueError("trade msg is not object")
        mt = msg.get("market_ticker")
        trade_id = msg.get("trade_id")
        ts_ms = msg.get("ts_ms")
        if (type(mt) is not str or not mt or type(trade_id) is not str
                or not trade_id or type(ts_ms) is not int or ts_ms <= 0):
            raise ValueError("trade identity/time invalid")
        if trade_id in S.public_trade_ids:
            return None
        count_d = _strict_decimal_string(msg.get("count_fp"), "trade.count_fp")
        yes_px_d = _strict_decimal_string(
            msg.get("yes_price_dollars"), "trade.yes_price_dollars")
        if yes_px_d >= 1:
            raise ValueError("trade yes price outside (0,1)")
        outcome = msg.get("taker_outcome_side")
        book_side = msg.get("taker_book_side")
        legacy = msg.get("taker_side")
        if outcome is None:
            outcome = legacy
        if outcome not in ("yes", "no"):
            raise ValueError("unknown taker outcome side")
        if book_side is not None:
            expected_book = "bid" if outcome == "yes" else "ask"
            if book_side != expected_book:
                raise ValueError("conflicting taker direction")
        maker_side_hit = "ask_no" if outcome == "yes" else "bid"
    except (ValueError, InvalidOperation) as e:
        L.w({"ev": "PUBLIC_TRADE_REJ", "reason": str(e)[:140]})
        return False

    if len(S.public_trade_id_fifo) == S.public_trade_id_fifo.maxlen:
        evicted = S.public_trade_id_fifo[0]
        S.public_trade_ids.discard(evicted)
    S.public_trade_id_fifo.append(trade_id)
    S.public_trade_ids.add(trade_id)
    tape = S.public_trades[mt]
    tape.append((ts_ms, maker_side_hit, float(yes_px_d), float(count_d)))
    cutoff = ts_ms - 300_000
    while tape and tape[0][0] < cutoff:
        tape.popleft()
    _shadow_apply_queue_trade(
        mt, maker_side_hit, float(yes_px_d), float(count_d),
        ts_ms, trade_id)
    return True


def pair_flow_stats(mt, y_px, n_px, now_ms):
    """Recent executable taker flow and queue-clear ETA for both legs."""
    tape = S.public_trades.get(mt, ())
    out = {}
    for horizon_s in (10, 30, 60):
        cutoff = int(now_ms) - horizon_s * 1000
        y_qty = 0.0
        n_qty = 0.0
        for ts_ms, maker_side_hit, yes_trade_px, qty in tape:
            if ts_ms < cutoff:
                continue
            if maker_side_hit == "bid" and yes_trade_px <= y_px + 1e-12:
                y_qty += qty
            elif (maker_side_hit == "ask_no"
                  and 1.0 - yes_trade_px <= n_px + 1e-12):
                n_qty += qty
        out[f"y_flow_{horizon_s}s"] = y_qty
        out[f"n_flow_{horizon_s}s"] = n_qty
    bk = S.books.get(mt) or {"y": {}, "n": {}}
    y_level = int(round(y_px * 10000))
    n_level = int(round(n_px * 10000))
    y_ahead = float(bk["y"].get(y_level, 0.0) or 0.0)
    n_ahead = float(bk["n"].get(n_level, 0.0) or 0.0)
    y_rate = out["y_flow_60s"] / 60.0
    n_rate = out["n_flow_60s"] / 60.0
    out.update({
        "y_queue_ahead": y_ahead,
        "n_queue_ahead": n_ahead,
        "y_clear_eta_s": (
            (y_ahead + float(CLIP)) / y_rate if y_rate > 0 else None),
        "n_clear_eta_s": (
            (n_ahead + float(CLIP)) / n_rate if n_rate > 0 else None),
    })
    return out


async def _md_session(ws):
    """One subscription session; ALWAYS returns within MD_SESSION_S.

    The old `async for raw in ws` checked its 120s budget only when a
    message ARRIVED — after the subscribed market settled, the silent
    channel hung the loop forever, fetch_open() never re-ran and the
    engine sat at markets=0 while the exchange had an open market
    (found live in shadow, 2026-07-26; the earlier 'markets=0 blip').
    """
    t0 = time.time()
    # Session deadline snaps to the earliest market close (+3s): when the
    # only subscribed market settles, the session must END so md_task
    # re-fetches the newly listed window immediately.  Fixed cadence left
    # the engine blind for up to ~2.5 min every quarter hour (stop-source
    # #8, observed 2026-07-27T22:45 as markets=0 with a 214s eval stall).
    deadline = MD_SESSION_S
    closes = [row[1] for row in S.meta.values() if row[1] > t0]
    if closes:
        deadline = min(MD_SESSION_S, max(5.0, min(closes) + 3.0 - t0))
    last_data = time.time()
    while time.time() - t0 <= deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=MD_RECV_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Stop-source #11 (2026-07-27T23:38): a session can go silent
            # WITHOUT erroring -- socket up, subscription dead, pings fine.
            # 30s with zero data while a market is open = force resubscribe.
            if time.time() - last_data > 30.0:
                L.w({"ev": "MD_STALE_RECONNECT",
                     "silent_s": round(time.time() - last_data, 1)})
                return
            continue
        m = json.loads(raw); t = m.get("type"); msg = m.get("msg",{})
        mt = msg.get("market_ticker","")
        # Staleness clock feeds on MARKET DATA ONLY: heartbeats and
        # subscription acks kept the old clock "fresh" while the book was
        # dead, so the 30s force-reconnect never fired (223s stalls at
        # 00:38 and 00:45, the recurring window thief).
        if t in ("orderbook_snapshot", "orderbook_delta", "trade",
                 "ticker"):
            last_data = time.time()
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
        elif t == "trade":
            apply_public_trade(msg)


def md_subscription_params(tickers):
    """Pin the order-book price convention consumed by ``_md_session``.

    The current parser stores YES bids on a YES-price scale and NO bids on a
    NO-price scale.  Kalshi currently defaults to that dual-scale convention,
    but has announced a future default flip to unified YES pricing.  Relying
    on the default would silently complement every NO level after the flip.
    """
    return {
        "channels": ["orderbook_delta", "trade"],
        "market_tickers": list(tickers),
        "use_yes_price": False,
    }


async def md_task():
    while True:
        try:
            S.meta.update(fetch_open())
            tickers = [mt for mt,(s,cs,_k) in S.meta.items() if cs > time.time()]
            if not tickers: await asyncio.sleep(3); continue
            async with websockets.connect(WS, additional_headers=websockets_headers(),
                                          ping_interval=10) as ws:
                L.w({"ev": "MD_CONNECT", "tickers": len(tickers)})
                await ws.send(json.dumps({
                    "id": 2,
                    "cmd": "subscribe",
                    "params": md_subscription_params(tickers),
                }))
                await _md_session(ws)
        except Exception as e:
            L.w({"ev":"MD_ERR","err":repr(e)[:150]}); await asyncio.sleep(2)

# ------------------------------------------------------------ F3 fills
def fill_created_s(f):
    """Fill timestamp in SECONDS regardless of the wire encoding.

    Preserve sub-second precision for queue/fill-hazard measurements.  The
    REST cursor is floored separately when it is sent back as ``min_ts``;
    fill-id idempotency makes that inclusive overlap safe.
    """
    ts = f.get("created_ts")
    if ts is not None:
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(ts):
            return None
        return ts / 1000.0 if ts > 10 ** 12 else ts
    ct = f.get("created_time")
    if ct:
        try:
            import datetime as dtm
            return dtm.datetime.fromisoformat(
                str(ct).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return None
    return None


# Live capture 2026-07-26 (incident #3): every portfolio surface uses
# *_fp / *_dollars STRING fields (count_fp, position_fp, yes_price_dollars,
# yes_total_cost_dollars...).  The legacy names this engine originally
# guessed never arrive on the wire.  Readers accept both shapes; a record
# that parses under NEITHER is a schema break and must halt live trading,
# never be silently skipped (11x FILL_APPLY_ERR while quoting = incident #3).

def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def qty_of(rec, base):
    """Contract count: '<base>_fp' string first, legacy number second.
    None (not 0) when the field is absent or unparseable."""
    if f"{base}_fp" in rec:
        return fnum(rec[f"{base}_fp"])
    return fnum(rec.get(base))


def cents_of(rec, base):
    """Money in cents: '<base>_dollars' string x100 first, legacy cents
    number second.  None when absent or unparseable."""
    if f"{base}_dollars" in rec:
        v = fnum(rec[f"{base}_dollars"])
        return None if v is None else v * 100.0
    return fnum(rec.get(base))


def fill_price_dollars(f, side):
    """Side-correct fill cost in dollars.  None = unparseable."""
    base = "yes_price" if side == "bid" else "no_price"
    if f"{base}_dollars" in f:
        return fnum(f[f"{base}_dollars"])
    # The live WS fill currently sends only yes_price_dollars even when
    # outcome_side=no/book_side=ask.  In a binary book NO cost is the exact
    # complement, so derive the missing canonical side instead of halting;
    # REST later supplies both and fill-id dedupe keeps this single-entry.
    other = "no_price" if base == "yes_price" else "yes_price"
    if f"{other}_dollars" in f:
        other_px = fnum(f[f"{other}_dollars"])
        return None if other_px is None else 1.0 - other_px
    px = fnum(f.get(base))
    if px is None and f.get(other) is not None:
        other_px = fnum(f.get(other))
        if other_px is not None:
            other_px = other_px / 100.0 if other_px > 1.0 else other_px
            return 1.0 - other_px
    if px is None:
        px = fnum(f.get("price"))
    if px is None:
        return None
    return px / 100.0 if px > 1.0 else px


def ws_fill_to_rest(msg):
    """Normalize a WS 'fill' channel message to the REST fills shape so
    both paths feed the ONE apply_fill ledger.  Real wire fields pass
    through untouched — parsing happens in exactly one place."""
    f = {k: msg[k] for k in (
        "side", "outcome_side", "book_side", "action",
        "count", "count_fp", "yes_price", "no_price",
        "yes_price_dollars", "no_price_dollars", "price",
        "created_ts", "created_time", "fill_id", "trade_id", "order_id",
        "subaccount", "subaccount_number", "fee_cost", "is_taker")
         if k in msg}
    f["ticker"] = msg.get("market_ticker") or msg.get("ticker")
    if "created_ts" not in f and "created_time" not in f:
        f["created_ts"] = msg.get("ts")
    return f


def fill_engine_side(f):
    """Engine side of a fill, by AUTHORITY order (Kalshi direction
    rules, docs/getting_started/order_direction): outcome_side, then
    book_side, then the bare 'side' as a legacy fallback.  A 'sell yes'
    print is LONG NO (ask) — reading the bare side field books it as
    long YES, which is how the 07-25 session miscounted its inventory
    direction.  None = cannot determine (caller halts live)."""
    o = f.get("outcome_side")
    b = f.get("book_side")
    o_side = ("bid" if o == "yes" else "ask_no" if o == "no" else None)
    b_side = ("bid" if b == "bid" else "ask_no" if b == "ask" else None)
    if o_side and b_side and o_side != b_side:
        return None
    if o_side:
        return o_side
    if b_side:
        return b_side
    # Live fills are required to carry a canonical direction field.  The
    # legacy fallback remains solely for offline historical replay.
    if MODE == "live":
        return None
    raw = f.get("side")
    if raw in ("yes", "bid"):
        return "bid"
    if raw in ("no", "ask", "ask_no"):
        return "ask_no"
    return None


def parse_fill(f):
    """Strictly parse one fill record into (ticker, side, count,
    px_dollars, fee_dollars, ts_s).  Returns (None, why) on ANY consumed-field
    failure — the caller decides whether that halts the engine."""
    # The WS push carries "market_ticker"; so does /portfolio/fills (verified
    # against the live REST payload 2026-07-27).  Only the WS path used to
    # normalise it, so a REST-delivered fill parsed as "no ticker" -- which in
    # live mode is a HALT.  Accept both spellings here, at the single point
    # every fill flows through.
    mt = f.get("ticker") or f.get("market_ticker")
    if not mt:
        return None, "no ticker"
    side = fill_engine_side(f)
    if side is None:
        return None, ("cannot determine direction "
                      f"(outcome_side={f.get('outcome_side')!r} "
                      f"book_side={f.get('book_side')!r} "
                      f"side={f.get('side')!r})")
    n = qty_of(f, "count")
    if n is None or n <= 0:
        return None, "count missing/unparseable"
    px = fill_price_dollars(f, side)
    if px is None or not (0.0 < px < 1.0):
        return None, f"price missing/unparseable ({px!r})"
    ts = fill_created_s(f)
    if ts is None:
        return None, "timestamp missing/unparseable"
    if "fee_cost" in f:
        fee = fnum(f.get("fee_cost"))
        if fee is None or fee < 0:
            return None, "fee_cost missing/unparseable"
    elif MODE == "live":
        return None, "fee_cost absent on live fill"
    else:
        fee = 0.0
    return (mt, side, n, px, fee, ts), None


def apply_fill(f, *, advance_cursor=True):
    """One exchange fill: parse STRICTLY, then advance the cursor, latch
    the side, move cost from the resting-order reservation to the filled
    ledger (A1 stays conserved — never double-counted, never dropped).
    Deduped by trade_id so the WS push and the REST poll can both
    deliver it.  An unparseable record means the ledger is blind — in
    live mode that is a HALT, not a log line (incident #3: 11x
    FILL_APPLY_ERR while quoting continued).  The cursor advances ONLY
    after a successful parse, so a schema break can never make the REST
    backstop skip what the WS path failed to apply."""
    fill_id = f.get("fill_id")
    trade_id = f.get("trade_id")
    subaccount = f.get("subaccount_number", f.get("subaccount", 0))
    aliases = set()
    if trade_id:
        aliases.add((subaccount, "trade", trade_id))
    if fill_id:
        aliases.add((subaccount, "fill", fill_id))
    if aliases:
        key = next(iter(aliases))
    elif MODE == "live":
        parsed, why = None, "fill_id/trade_id absent on live fill"
        L.w({"ev": "FILL_APPLY_ERR", "why": why})
        if not S.halted:
            S.halted = True
            cancel_all("FILL_ID_BLIND")
            L.w({"ev": "HALT", "reason": why})
            write_control_status()
        return
    else:
        key = (f.get("ticker"), f.get("side"), qty_of(f, "count"),
               f.get("created_ts"), f.get("created_time"))
    if aliases and any(alias in S.fills_seen for alias in aliases):
        L.w({"ev": "FILL_DUP_SKIP", "key": str(sorted(aliases))[:120]})
        return
    if not aliases and key in S.fills_seen:
        L.w({"ev": "FILL_DUP_SKIP", "key": str(key)[:120]})
        return
    parsed, why = parse_fill(f)
    if parsed is None:
        L.w({"ev": "FILL_APPLY_ERR", "why": why, "raw": {
            k: f.get(k) for k in (
                "ticker", "side", "outcome_side", "book_side", "action",
                "count", "count_fp", "price",
                "yes_price", "no_price", "yes_price_dollars",
                "no_price_dollars", "created_ts", "created_time")}})
        if MODE == "live" and not S.halted:
            S.halted = True
            cancel_all("FILL_SCHEMA_BLIND")
            L.w({"ev": "HALT",
                 "reason": f"unparseable fill record ({why}) — "
                           "ledger blind, refusing to keep quoting"})
            write_control_status()
        return
    mt, side, n, px, fee, ts = parsed
    S.fills_seen.update(aliases or {key})
    # The fill changes positions even when it is not tied to a currently
    # resting local order.  Advance before any in-place ledger mutations.
    risk_generation_bump()
    # Keep an inclusive, overlapping timestamp watermark.  ID idempotency,
    # not ts+1, prevents reapplication and therefore cannot skip a second
    # fill that shares this integer second.
    if advance_cursor:
        S.fills_cursor = max(S.fills_cursor, math.floor(ts))
    if TOX_ENABLE:
        # Park this fill for scoring once its markout horizon elapses: the
        # table learns only from realised outcomes, never from the fill it
        # is about to price.
        snap = S.last_eval_state.get(mt) or {}
        S.tox_pending.append({
            "t": time.time(), "mt": mt, "side": side,
            "entry_c": px * 100.0,
            "bucket": tox_bucket(side, px * 100.0, snap.get("sigma_c"))})
    S.recent_fill_mono[mt] = time.monotonic()
    _bk = (mt, side)
    _now_m = time.monotonic()
    _times = [t2 for t2 in S.side_fill_times.get(_bk, [])
              if _now_m - t2 <= BRAKE_N_S]
    _times.append(_now_m)
    S.side_fill_times[_bk] = _times
    if len(_times) >= 2:
        S.side_brake[_bk] = _now_m + BRAKE_HOLD_S
        L.w({"ev": "SIDE_BRAKE", "mt": mt, "side": side,
             "fills_in_window": len(_times), "hold_s": BRAKE_HOLD_S})
    if f.get("order_id"):
        # Tombstone: the exchange's order snapshot can still show this
        # order as "resting" for a moment after the fill releases our local
        # reservation.  The budget guard consults these to distinguish
        # "our just-filled order, stale snapshot" (skip, receipt) from a
        # truly foreign order (hard trip).  Observed live 2026-07-27T20:38:
        # fill -> complement preflight -> "no unique local reservation".
        S.order_tombstones[f["order_id"]] = time.monotonic()
    L.w({"ev": "FILL", "ticker": mt, "side": side, "count": n,
         "px_dollars": px, "fee_dollars": fee, "ts_s": ts,
         "fill_id": fill_id, "trade_id": trade_id,
         "subaccount": subaccount, "order_id": f.get("order_id"),
         "ctx": S.last_eval_state.get(mt)})
    S.latch.add((mt, side))
    d = S.net_pos.setdefault(mt, {"y": 0, "n": 0, "cost": 0.0})
    d["y" if side == "bid" else "n"] += n
    d["cost"] += px * n + fee
    S.open_cost = sum(v["cost"] for v in S.net_pos.values())
    order_id = f.get("order_id")
    if order_id:
        S.filled_by_order[order_id] += n
        for pending_key, pending in list(S.flatten_pending.items()):
            if pending.get("order_id") != order_id:
                continue
            remaining_risk = max(
                0.0,
                float(pending.get("remaining_risk_qty", 0.0)) - n,
            )
            pending["remaining_risk_qty"] = remaining_risk
            pending["fee_reserve"] = (
                taker_fee_usd(pending["risk_px"], remaining_risk)
                if remaining_risk > 1e-9 else 0.0
            )
            risk_map_touch(S.flatten_pending, pending_key)
            if (pending.get("terminal")
                    and abs(S.filled_by_order[order_id]
                            - float(pending.get("expected_fill", -1.0)))
                    <= 1e-9):
                risk_map_pop(S.flatten_pending, pending_key)
    order_key = None
    if order_id:
        order_key = next((
            key_ for key_, value in S.orders.items()
            if value.get("id") == order_id
        ), None)
    elif MODE != "live":
        order_key = (mt, side)
    od = S.orders.get(order_key) if order_key else None
    if od is not None:
        od["qty"] = float(od.get("qty", CLIP)) - n
        if od["qty"] <= 1e-9:
            risk_map_pop(S.orders, order_key)
            if order_id:
                risk_map_pop(S.unknown_orders, order_id)
        else:
            risk_map_touch(S.orders, order_key)
    if PAIR:
        pair_off(mt, side, n, px, ts, fee)
        orphan_side = "bid" if side == "ask_no" else "ask_no"
        # IOC terminal reconciliation, not inventory shape, owns removal of
        # flatten_pending.  This prevents a delayed/partial IOC from being
        # duplicated while its final state is unknown.
    if S.canary_done and not S.halted:
        S.halted = True
        cancel_all("CANARY_MAX_CYCLES")
        L.w({"ev": "CANARY_DONE", "completed_cycles":
             S.completed_cycles, "pair_locked_total":
             round(S.pair_locked_total, 6)})
        write_control_status()
    elif PAIR and not S.halted:
        # The fill event, rather than a later book tick, drives the hedge
        # decision.  Avoid synchronous recursion when called inside asyncio.
        try:
            asyncio.get_running_loop().call_soon(think)
        except RuntimeError:
            pass


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
    # min_ts must be an integer on the wire: the exchange 400s a float
    # query value (found at first live ignition 2026-07-27).  Floor keeps
    # the probe inclusive of the newest fill.
    code2, d2 = rest(
        "GET", f"/portfolio/fills?min_ts={int(ts) - 1}&limit=100")
    if code2 != 200:
        return False, f"min_ts probe HTTP {code2}"
    if not (d2.get("fills") or []):
        return False, ("min_ts probe returned empty while a fill exists — "
                       "unit bug (D1 class), refusing to trade blind")
    return True, f"pipeline sees fill at ts={ts}"


# ------------------------------------------------- contract selfcheck
def parse_position(p):
    """(ok, why) for one /portfolio/positions record — every field the
    reconciler consumes must exist and parse."""
    if not p.get("ticker"):
        return False, "no ticker"
    if qty_of(p, "position") is None:
        return False, "position missing/unparseable (want position_fp)"
    return True, "ok"


def parse_settlement(s):
    """(ok, why) for one /portfolio/settlements record — every field the
    econ breaker (F4) consumes must exist and parse."""
    if not s.get("ticker"):
        return False, "no ticker"
    for base in ("revenue", "yes_total_cost", "no_total_cost"):
        if cents_of(s, base) is None:
            return False, f"{base} missing/unparseable"
    try:
        import datetime as dtm
        dtm.datetime.fromisoformat(
            str(s.get("settled_time")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False, "settled_time unparseable"
    return True, "ok"


CONTRACT_SURFACES = (
    ("fills", "/portfolio/fills?limit=1", "fills",
     lambda r: (parse_fill(r)[0] is not None, parse_fill(r)[1] or "ok")),
    ("positions", "/portfolio/positions?count_filter=position"
     "&limit=1&subaccount=0", "market_positions",
     parse_position),
    ("settlements", "/portfolio/settlements?limit=1", "settlements",
     parse_settlement),
)


def contract_selfcheck():
    """Startup ignition gate (incident #3 fix doctrine #1): fetch ONE
    real record from each REST portfolio surface and prove every field
    this engine consumes exists and parses.  Three same-day schema-name
    guesses (count_fp, position_fp, yes_total_cost_dollars) each blinded
    a different safety layer — unit tests against assumptions cannot
    catch that; only the exchange's own records can.  An empty surface
    is reported but cannot be verified.  The WS fill surface cannot be
    fetched on demand — it is covered by parse_fill going through the
    same strict path with HALT-on-failure at the first live message."""
    notes = []
    for name, path, listkey, parser in CONTRACT_SURFACES:
        code, d = rest("GET", path)
        if code != 200:
            return False, f"{name}: HTTP {code}"
        recs = d.get(listkey) or []
        if not recs:
            notes.append(f"{name}=EMPTY(unverified)")
            continue
        ok, why = parser(recs[0])
        if not ok:
            return False, f"{name}: {why}"
        notes.append(f"{name}=ok")
    return True, " ".join(notes)


def flat_start_selfcheck():
    """Prove the account has no inherited position or nonterminal order."""
    surfaces = (
        ("/portfolio/positions?count_filter=position"
         "&limit=1000&subaccount=0",
         "market_positions"),
        # Flat-start must see create-pending and every other nonterminal row;
        # unlike the budget snapshot, it deliberately does not RESTING-filter.
        ("/portfolio/orders?subaccount=0&limit=1000",
         "orders"),
    )
    findings = []
    for base, list_key in surfaces:
        cursor = None
        seen = set()
        pages = 0
        while True:
            path = base + (
                "&cursor=" + urllib.parse.quote(str(cursor), safe="")
                if cursor else "")
            code, data = rest("GET", path)
            if code != 200:
                return False, f"{list_key}: HTTP {code}"
            pages += 1
            rows = data.get(list_key) or []
            if list_key == "market_positions":
                for row in rows:
                    qty = qty_of(row, "position")
                    if qty is None:
                        return False, "positions: unparseable position"
                    if abs(qty) > 1e-9:
                        findings.append(
                            f"position:{row.get('ticker')}={qty}")
            else:
                for row in rows:
                    remaining = qty_of(row, "remaining_count")
                    if remaining is None:
                        return False, "orders: unparseable remaining_count"
                    if remaining > 1e-9:
                        findings.append(
                            f"order:{row.get('order_id') or row.get('id')}"
                            f" remaining={remaining}")
            cursor = data.get("cursor") or data.get("next_cursor")
            if not cursor:
                break
            if cursor in seen or pages >= 100:
                return False, f"{list_key}: cursor loop"
            seen.add(cursor)
    if findings:
        return False, "nonzero start: " + ",".join(findings[:10])
    if S.pending_new or S.unknown_orders or S.flatten_pending:
        return False, "local unresolved order state is nonempty"
    return True, "zero positions and zero nonterminal orders"


# ----------------------------------------------------------- F4 breaker
ECON_HALT_LOSS = float(os.environ.get("MM_ECON_HALT", "-2.0"))  # dollars
ECON_ZERO_STREAK = 2   # consecutive zero-revenue settlements -> halt


def settlement_in_session(s):
    """Only settlements that settled AFTER this process started count
    toward the econ breaker.  The first live boot applied all-time
    history and produced a garbage realized number — thresholds must
    compare against THIS session's economics only."""
    ts = s.get("settled_time")
    try:
        import datetime as dtm
        settled = dtm.datetime.fromisoformat(
            str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return False        # unparseable time: exclude, never guess
    return settled >= S.session_start_ts - 60.0


def apply_settlement(s):
    """One EXCHANGE settlement record (the only admissible P&L source —
    2026-07-25 lesson: never judge P&L from balance or memory).  Trips
    the economic breaker on (a) two consecutive zero-revenue settlements
    with nonzero cost — the exact incident signature — or (b) cumulative
    realized below ECON_HALT_LOSS.  The halt does not auto-resume."""
    key = (s.get("ticker"), s.get("settled_time"))
    if key in S.settled_seen:
        return False
    revenue = cents_of(s, "revenue")
    yes_cost = cents_of(s, "yes_total_cost")
    no_cost = cents_of(s, "no_total_cost")
    fee = fnum(s.get("fee_cost")) if "fee_cost" in s else 0.0
    if revenue is None or yes_cost is None or no_cost is None:
        # Incident #3: F4 read the guessed 'yes_total_cost' name, got 0,
        # and computed garbage realized P&L.  A settlement this engine
        # cannot price means the econ breaker is blind — halt, don't skip.
        L.w({"ev": "SETTLE_PARSE_ERR", "raw": {k: s.get(k) for k in (
            "ticker", "settled_time", "revenue", "revenue_dollars",
            "yes_total_cost", "yes_total_cost_dollars",
            "no_total_cost", "no_total_cost_dollars")}})
        if MODE == "live" and not S.halted:
            S.halted = True
            cancel_all("SETTLE_SCHEMA_BLIND")
            L.w({"ev": "HALT",
                 "reason": "unparseable settlement — econ breaker blind"})
            write_control_status()
        return False
    if fee is None or fee < 0:
        L.w({"ev": "SETTLE_PARSE_ERR", "why": "fee_cost",
             "ticker": s.get("ticker"), "fee_cost": s.get("fee_cost")})
        if MODE == "live" and not S.halted:
            S.halted = True
            cancel_all("SETTLE_FEE_BLIND")
            write_control_status()
        return False
    S.settled_seen.add(key)
    cost = yes_cost + no_cost
    reported_pnl = (revenue - cost) / 100.0 - fee
    mt = s.get("ticker")
    pair_ledger = mt in S.pair_locked_by_market or mt in S.unpaired
    residual_cost = 0.0
    if pair_ledger:
        result = str(s.get("market_result", "")).lower()
        if result not in ("yes", "no"):
            L.w({"ev": "SETTLE_PARSE_ERR", "why": "market_result",
                 "ticker": mt, "market_result": s.get("market_result")})
            if MODE == "live" and not S.halted:
                S.halted = True
                cancel_all("SETTLE_RESULT_BLIND")
                write_control_status()
            return False
        # Pair locks have already been realized exactly once in pair_off.
        # Settlement therefore books only residual lots still present in the
        # local event ledger, never the exchange's gross historical basis.
        pnl = 0.0
        lots = S.unpaired.get(mt, {"bid": [], "ask_no": []})
        for side in ("bid", "ask_no"):
            wins = ((side == "bid" and result == "yes")
                    or (side == "ask_no" and result == "no"))
            for lot in lots.get(side, []):
                px, _ts, ct = lot[:3]
                lot_fee = lot[3] if len(lot) > 3 else 0.0
                basis = px * ct + lot_fee
                residual_cost += basis
                pnl += (ct if wins else 0.0) - basis
        S.unpaired.pop(mt, None)
        risk_map_pop(S.net_pos, mt)
        S.cycle_active.discard(mt)
    else:
        pnl = reported_pnl
    S.realized += pnl
    breaker_cost = residual_cost if pair_ledger else cost / 100.0 + fee
    if pnl <= 0 and breaker_cost > 0:
        S.settle_zero_streak += 1
    else:
        S.settle_zero_streak = 0
    L.w({"ev": "SETTLE", "ticker": s.get("ticker"),
         "result": s.get("market_result"), "revenue_c": revenue,
         "cost_c": cost, "fee_usd": fee,
         "reported_pnl_usd": round(reported_pnl, 4),
         "pair_ledger": pair_ledger, "pnl_usd": round(pnl, 4),
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
            if settlement_in_session(s):
                apply_settlement(s)


# --------------------------------------------------------------- F2 recon
RECON_EVERY_S = 5.0
RECON_MAX_DIVERGENCE = float(
    os.environ.get("MM_RECON_MAX_DIVERGENCE", "0"))
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
        # Real schema is position_fp STRING; there is no 'position' key
        # (incident #3: reading it gave exch=0 vs local 0 = false green).
        # Unparseable = divergence, never a silent 0.
        raw = qty_of(mp, "position")
        if raw is None:
            lp = local.get(mt, {"y": 0, "n": 0})
            diffs.append((mt, float(lp["y"] - lp["n"]), None))
            continue
        exch = float(raw)
        lp = local.get(mt, {"y": 0, "n": 0})
        lnet = float(lp["y"] - lp["n"])
        if abs(lnet - exch) > max(RECON_MAX_DIVERGENCE, 0.001):
            # 0.001-contract noise band: fractional fills accumulate
            # binary-float dust (local -1.9100000000000001 vs exchange
            # -1.91 halted recon 2026-07-28T01:47).  Real divergences are
            # whole contracts.
            diffs.append((mt, lnet, exch))
    for mt, lp in local.items():
        if mt in seen:
            continue
        lnet = float(lp["y"] - lp["n"])
        if abs(lnet) > RECON_MAX_DIVERGENCE:
            diffs.append((mt, lnet, 0))
    return diffs


def adopt_open_positions():
    """Rebuild the local ledger from exchange truth at startup.

    The engine used to assume it always began flat: a restart while holding
    inventory left local=0 vs exchange=1, which is (correctly) a
    reconciliation divergence and a budget trip -- observed live
    2026-07-27T08:00 when switching modes with one YES lot open.  Being
    unable to restart without first flattening is not acceptable for a
    market maker, so startup now ADOPTS what the account actually holds.

    Fail-closed rules, in order:
      * both endpoints must answer 200, or adoption fails;
      * every open position must have enough recent fills to reconstruct a
        cost basis; an unexplained lot is a refusal, never a guess;
      * resting exchange orders cannot be adopted (their queue position and
        client id are unknowable after a restart) -- they are cancelled, so
        the book is exactly what this process believes it is;
      * the rebuilt ledger is then checked against the same
        recon_divergences() the running engine uses.

    Returns (ok, why).  On failure the caller must not trade.
    """
    code, pos = rest("GET", "/portfolio/positions?count_filter=position"
                            "&limit=1000&subaccount=0")
    if code != 200:
        return False, f"positions HTTP {code}"
    rows = [r for r in (pos.get("market_positions") or [])
            if qty_of(r, "position") not in (None, 0.0)]
    if not rows:
        return True, "flat account, nothing to adopt"

    # Cancel every resting order first: an order we did not place in THIS
    # process has no reservation, no queue history and no client id we can
    # trust.  Leaving it live would make the budget guard see an exchange
    # order with no local twin (its exact trip on 2026-07-27T08:00).
    ocode, orders = rest("GET", "/portfolio/orders?status=resting&limit=1000")
    if ocode != 200:
        return False, f"resting orders HTTP {ocode}"
    stale = orders.get("orders") or []
    for od in stale:
        oid = od.get("order_id")
        if not oid:
            return False, "resting order without order_id"
        ccode, _ = rest("DELETE", f"/portfolio/events/orders/{oid}",
                        host=V2O)
        if ccode not in (200, 201, 404):
            return False, f"cannot cancel pre-existing order {oid}: {ccode}"
    if stale:
        L.w({"ev": "ADOPT_CANCELLED_ORDERS", "n": len(stale)})

    # Cost basis from the fills that built each lot.  Walk fills newest to
    # oldest and consume them until the open quantity is explained.
    fcode, fills = rest("GET", "/portfolio/fills?limit=1000")
    if fcode != 200:
        return False, f"fills HTTP {fcode}"
    per_market = {}
    for f in (fills.get("fills") or []):
        parsed, why = parse_fill(f)
        if parsed is None:
            return False, f"unparseable fill during adoption: {why}"
        mt, side, n, px, fee, ts = parsed
        per_market.setdefault(mt, []).append(
            {"side": side, "n": float(n), "px": float(px),
             "fee": float(fee), "ts": float(ts)})
    for lots in per_market.values():
        lots.sort(key=lambda r: r["ts"], reverse=True)

    adopted = {}
    for row in rows:
        mt = row.get("ticker")
        net = float(qty_of(row, "position"))
        want_side = "bid" if net > 0 else "ask_no"
        need = abs(net)
        cost = 0.0
        got = 0.0
        for lot in per_market.get(mt, []):
            if lot["side"] != want_side or got >= need - 1e-9:
                continue
            take = min(lot["n"], need - got)
            cost += take * lot["px"] + lot["fee"] * (take / lot["n"])
            got += take
        if got < need - 1e-9:
            return False, (f"cannot reconstruct basis for {mt}: "
                           f"need {need} lots, fills explain {got}")
        adopted[mt] = {"y": need if net > 0 else 0.0,
                       "n": need if net < 0 else 0.0,
                       "cost": cost}
        L.w({"ev": "ADOPT_POSITION", "mt": mt, "side": want_side,
             "qty": need, "cost_usd": round(cost, 6),
             "avg_px_c": round(100.0 * cost / need, 3)})

    S.net_pos.update(adopted)
    S.open_cost = sum(v["cost"] for v in S.net_pos.values())
    risk_generation_bump()
    # Adopted lots are unpaired inventory in pair mode: the pairing loop must
    # see them so it can quote the complement instead of ignoring them.
    if PAIR:
        for mt, v in adopted.items():
            slot = S.unpaired.setdefault(mt, {"bid": [], "ask_no": []})
            side = "bid" if v["y"] > 0 else "ask_no"
            qty = v["y"] or v["n"]
            px = v["cost"] / qty if qty else 0.0
            # Lot tuples are (px, ts, ct, fee) EVERYWHERE -- the 3-tuple
            # written here crashed pair_flatten_task on first contact
            # (2026-07-27T22:59, engine_process_dead).  Adopted lots carry
            # fee 0.0: their fees are already inside the reconstructed cost.
            slot[side].append((px, time.time(), qty, 0.0))
    diffs = recon_divergences(S.net_pos, pos.get("market_positions"))
    if diffs:
        return False, f"adopted ledger still diverges: {diffs[:3]}"
    return True, f"adopted {len(adopted)} position(s)"


def recon_check():
    """One reconciliation pass.  False = engine halted or blind pass."""
    code, d = rest("GET",
                   "/portfolio/positions?count_filter=position"
                   "&limit=1000&subaccount=0")
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
    # Race #5 sibling (2026-07-27T23:30 RECON_HALT): the recon line needs
    # the same settlement-window exemption as the budget line -- a market
    # past close leaves the live comparison; its terminal accounting flows
    # through the settlements pipeline.
    now_r = time.time()
    open_now = {m for m, row in S.meta.items() if row[1] > now_r}
    local_open = {m: v for m, v in S.net_pos.items()
                  if m in open_now or (v["y"] or v["n"]) == 0}
    rows_open = [r_ for r_ in (d.get("market_positions") or [])
                 if r_.get("ticker") in open_now]
    diffs = recon_divergences(local_open, rows_open)
    if diffs:
        # Race #16 (2026-07-28T02:29 RECON_HALT): our own graveyard-flatten
        # taker fill moved local to 0 while the positions read-replica still
        # showed -2 for a few seconds.  Same grace as the budget line: a
        # divergence on a market WE just filled is explained by our own
        # action for 10s; one that persists past the grace is real.
        now_mono = time.monotonic()
        graced = [t for t in diffs
                  if now_mono - S.recent_fill_mono.get(t[0], -1e9) <= 10.0]
        if graced:
            L.w({"ev": "RECON_FILL_GRACE",
                 "diffs": [{"mt": m, "local": l, "exchange": x}
                           for m, l, x in graced]})
            diffs = [t for t in diffs if t not in graced]
    if diffs:
        # Race #16 mirror (2026-07-28T02:47): the exchange can also record
        # our fill BEFORE the local pipeline applies it, so recent_fill_mono
        # is not yet set and the grace above cannot see it.  Two-strike
        # rule: a divergence must survive two consecutive recon passes
        # (~5s apart) before it halts.  A real break still halts within
        # ~10s -- the same bound the own-fill grace already accepts.
        prev_suspect = S.recon_suspect
        S.recon_suspect = {t[0] for t in diffs}
        confirmed = [t for t in diffs if t[0] in prev_suspect]
        if not confirmed:
            L.w({"ev": "RECON_SUSPECT",
                 "diffs": [{"mt": m, "local": l, "exchange": x}
                           for m, l, x in diffs]})
            return False
        diffs = confirmed
    else:
        S.recon_suspect = set()
    if not diffs:
        S.last_recon_ok_mono = time.monotonic()
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


def poll_fills_once(order_id=None):
    """One complete, paginated REST fills sweep.

    Normal recovery uses an overlapping inclusive timestamp watermark and
    fill IDs for idempotency.  Cancel recovery scopes the sweep to one order.
    A partial/failed pagination never claims success.
    """
    if order_id:
        base = f"/portfolio/fills?order_id={order_id}&limit=100"
    else:
        base = ("/portfolio/fills?"
                f"min_ts={max(0, int(S.fills_cursor) - 1)}&limit=100")
    old_cursor = S.fills_cursor
    candidate_cursor = old_cursor
    cursor = None
    seen_cursors = set()
    pages = 0
    while True:
        path = base + (
            "&cursor=" + urllib.parse.quote(str(cursor), safe="")
            if cursor else "")
        code, d = rest("GET", path)
        if code != 200:
            S.fills_cursor = old_cursor
            L.w({"ev": "FILL_POLL_FAIL", "code": code,
                 "order_id": order_id, "page": pages})
            return False
        pages += 1
        for f in (d.get("fills") or []):
            ts = fill_created_s(f)
            apply_fill(f, advance_cursor=False)
            if ts is not None:
                candidate_cursor = max(candidate_cursor, ts)
            if S.halted and MODE == "live":
                S.fills_cursor = old_cursor
                return False
        cursor = d.get("cursor") or d.get("next_cursor")
        if not cursor:
            S.fills_cursor = (old_cursor if order_id else candidate_cursor)
            return True
        if cursor in seen_cursors or pages >= 100:
            L.w({"ev": "FILL_POLL_FAIL", "code": "CURSOR_LOOP",
                 "order_id": order_id, "page": pages})
            S.fills_cursor = old_cursor
            return False
        seen_cursors.add(cursor)


async def fills_task():
    while True:
        await asyncio.sleep(3)
        if MODE != "live": continue
        poll_fills_once()
        if S.realized <= KILL_LOSS and not S.halted:
            S.halted=True; cancel_all("KILL_LOSS"); L.w({"ev":"KILL","realized":S.realized})

def block_reason(*, blocked, px, is_exit_path, has_unpaired_opp,
                 entry_pricing_ok, zone_ok_, room, cheap_ok, edge, margin,
                 cap_ok, skew_block):
    """First failing gate for a side that was NOT quoted.

    Approval #1 (expanded): sampling-completeness piece.  Mirrors the want
    expression's evaluation order exactly; called ONLY when want is False,
    and always returns a reason -- so by construction
        sum(block reasons) == side-evaluations - side-quotes
    holds with zero error, which beat() asserts every cycle.
    """
    if blocked:
        return "side_latched"
    if px < 0.001:
        return "px_zero"
    if has_unpaired_opp:
        return "exit_leg_unready" if not is_exit_path else "exit_leg_capped"
    if not entry_pricing_ok:
        return "pricing_not_ready"
    if not zone_ok_:
        return "zone_closed"
    if not room:
        return "no_capital_room"
    if not cheap_ok:
        return "tail_blocked"
    if edge is None or edge < margin:
        return "edge_below_margin"
    if not cap_ok:
        return "edge_above_cap"
    if skew_block:
        return "skew_blocked"
    return "other"


def survival_check(now_mono=None):
    """Approval #1 ②: the engine must prove it is alive, not just running.

    Four live ignitions died silently doing nothing; both assertions are
    alarms (receipts + status), never behavior changes.

    A: unpaused for 5 minutes with zero QUOTE_EVAL -> the evaluation loop
       is starved (the 08:00 connection-contention shape).  Extended: a
       mid-run stall of 60s after evaluations HAVE flowed fires too.
    B: a side the engine WANTS has gone >2s with no resting order, no
       pending order and no receipt explaining why -> decision layer and
       execution layer have detached (the 07:51 sell_rich shape).
    """
    if MODE != "live" or S.halted or CTRL.get("paused"):
        return []
    now_mono = time.monotonic() if now_mono is None else now_mono
    alarms = []
    if S.unpaused_mono is not None:
        up_for = now_mono - S.unpaused_mono
        if up_for > 300 and S.evals_since_unpause == 0:
            alarms.append({"kind": "no_quote_eval_since_unpause",
                           "unpaused_s": round(up_for)})
        elif (S.evals_since_unpause > 0 and S.last_eval_mono is not None
              and now_mono - S.last_eval_mono > 60):
            alarms.append({"kind": "quote_eval_stalled",
                           "stalled_s": round(now_mono - S.last_eval_mono)})
    for (mt, side), t0 in list(S.want_unmet.items()):
        if now_mono - t0 > 2.0:
            alarms.append({"kind": "intent_without_order", "mt": mt,
                           "side": side,
                           "unserved_s": round(now_mono - t0, 1)})
            S.want_unmet[(mt, side)] = now_mono + 28.0   # re-alarm ~30s
    for a in alarms:
        S.survival_alarms += 1
        L.w({"ev": "SURVIVAL_ALARM", **a})
    return alarms


async def beat():
    while True:
        await asyncio.sleep(10)
        load_control(force=True)
        active = (MODE == "live" and not S.halted
                  and not CTRL.get("paused"))
        if active and S.unpaused_mono is None:
            S.unpaused_mono = time.monotonic()
            S.evals_since_unpause = 0
        elif not active:
            S.unpaused_mono = None
        survival_check()
        cutoff = time.monotonic() - 60.0
        for oid_ in [k for k, v in S.order_tombstones.items() if v < cutoff]:
            S.order_tombstones.pop(oid_, None)
        blocked_sum = sum(S.block_reasons.values())
        identity_err = S.side_evals - S.side_wants - blocked_sum
        if S.side_evals and identity_err != 0:
            L.w({"ev": "SURVIVAL_ALARM", "kind": "block_accounting_broken",
                 "err": identity_err})
            S.survival_alarms += 1
        L.w({"ev": "BLOCK_ACCOUNTING", "side_evals": S.side_evals,
             "side_wants": S.side_wants, "reasons": dict(S.block_reasons),
             "identity_err": identity_err})
        L.w({"ev":"HEALTH","mode":MODE,"halted":S.halted,
             "orders":len(S.orders),"open_cost":round(S.open_cost,2),
             "exposure":round(exposure(),2),
             "control_revision":CTRL["revision"],
             "max_open_cost":MAX_OPEN_COST,"clip":CLIP,"max_net":MAX_NET,
             "pair_prequote":PAIR_PREQUOTE,
             "pair_min_depth_ct":PAIR_MIN_DEPTH_CT,
             "shadow_queue_sim":SHADOW_QUEUE_SIM,
             "paused":CTRL["paused"],"kill":CTRL["kill"],
             "limit_breached":S.limit_breached,
             "requote_suppressed":S.requote_suppressed,
             "realized":round(S.realized,4),
             "recon_fails":S.recon_fails,
             "survival_alarms":S.survival_alarms,
             "markets":len(S.meta),
             "candidate_modules":mca.status(),
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


def strategy_config_errors():
    """Static fail-closed checks for the pair-cycle configuration."""
    errors = []
    # Hardcode-audit 2026-07-27: the code default MARGIN 0.3c is the
    # measured-toxic legacy value (bid markout -1.76c@5s).  A deployment
    # that loses the MM_MARGIN env would silently fall back to it and
    # replay disease #2 with no alarm.  Missing critical config is
    # ambiguity, and ambiguity halts: live requires an explicit margin.
    if MODE == "live" and os.environ.get("MM_MARGIN") is None:
        errors.append(
            "MM_MARGIN must be set explicitly for live (code default "
            "0.3c is the measured-toxic legacy value; refusing to guess)")
    if PAIR_PREQUOTE and not PAIR:
        errors.append("MM_PAIR_PREQUOTE requires MM_PAIR=1")
    if PAIR_PREQUOTE and MODE == "live":
        errors.append(
            "MM_PAIR_PREQUOTE is shadow-only pending replay promotion")
    if SHADOW_QUEUE_SIM and MODE != "shadow":
        errors.append("MM_SHADOW_QUEUE_SIM is shadow-only")
    if not math.isfinite(PAIR_LOCK_C) or not 0.0 < PAIR_LOCK_C <= 99.0:
        errors.append("MM_PAIR_LOCK_C must be in (0,99]")
    if (not math.isfinite(PAIR_MIN_EDGE_C)
            or not 0.0 <= PAIR_MIN_EDGE_C < PAIR_LOCK_C):
        errors.append(
            "MM_PAIR_MIN_EDGE_C must be finite and in [0, pair lock)")
    if (not math.isfinite(PAIR_MAX_LOCKED_COST)
            or PAIR_MAX_LOCKED_COST <= 0.0):
        errors.append("MM_PAIR_MAX_LOCKED_COST must be positive")
    if (not math.isfinite(PAIR_MIN_DEPTH_CT)
            or PAIR_MIN_DEPTH_CT < 0.0):
        errors.append("MM_PAIR_MIN_DEPTH_CT must be finite and non-negative")
    if PAIR and float(CLIP) > UNPAIRED_MAX_CT + 1e-9:
        errors.append("MM_CLIP exceeds MM_UNPAIRED_MAX_CT")
    # Research candidates are metadata-only and disabled by default.  If an
    # operator explicitly enables one, require its immutable fail-closed
    # contract and a complete import before the engine can start.
    errors.extend(mca.config_errors())
    return errors


def source_sha256(path):
    """Content identity for deployed experiment receipts."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def main():
    if MODE == "live" and BUDGET_ENFORCE:
        accounting_errors = budget_accounting_contract_errors()
        if accounting_errors:
            S.halted = True
            S.control_error = "; ".join(accounting_errors)
            L.w({
                "ev": "HALT",
                "reason": "budget accounting contract unverified",
                "errors": accounting_errors,
            })
            write_control_status()
            return
        try:
            initialize_budget_guard()
            L.w({
                "ev": "BUDGET_BOUND",
                "path": str(BUDGET_PATH),
                "budget_id": S.budget_guard.record.budget_id,
                "record_generation": S.budget_guard.record.generation,
                "anchor_micro_usd":
                    S.budget_guard.record.anchor_equity_micro_usd,
                "max_loss_micro_usd":
                    S.budget_guard.record.max_loss_micro_usd,
                "floor_micro_usd":
                    S.budget_guard.record.floor_equity_micro_usd,
                "writer_lock": str(S.live_writer_lock_path),
            })
        except Exception as exc:
            S.halted = True
            S.control_error = (
                f"absolute budget ignition failed: "
                f"{type(exc).__name__}: {exc}"
            )[:200]
            L.w({"ev": "HALT", "reason": S.control_error})
            write_control_status()
            return
    load_control(force=True)
    config_errors = strategy_config_errors()
    L.w({"ev": "CANDIDATE_MODULE_STATUS", "modules": mca.status()})
    if config_errors:
        S.halted = True
        S.control_error = "; ".join(config_errors)
        L.w({"ev": "HALT", "reason": "strategy config",
             "errors": config_errors})
    hydrated = hydrate_rti_from_capture()
    L.w({"ev": "RTI_HYDRATE_STATUS", "ticks": hydrated,
         "capture_glob": CF_CAPTURE_GLOB})
    if MODE == "live":
        violated, peers = credential_exclusivity_violation()
        L.w({"ev": "CREDENTIAL_EXCLUSIVITY", "ok": not violated,
             "peers": peers, "max_allowed": MAX_PEER_ENGINES})
        if violated:
            S.halted = True
            S.control_error = (f"credential exclusivity: {len(peers)} peer "
                               f"engine(s) {peers} share this API key")
            L.w({"ev": "HALT", "reason": S.control_error})
        ok, why = fills_visibility_selfcheck()
        S.fills_selfcheck_ok = ok
        L.w({"ev": "FILLS_SELFCHECK", "ok": ok, "why": why})
        if not ok:
            S.halted = True
            L.w({"ev": "HALT", "reason": f"fills selfcheck: {why}"})
        ok2, why2 = contract_selfcheck()
        S.contract_selfcheck_ok = ok2
        L.w({"ev": "CONTRACT_SELFCHECK", "ok": ok2, "why": why2})
        if not ok2:
            S.halted = True
            L.w({"ev": "HALT", "reason": f"contract selfcheck: {why2}"})
        ok3, why3 = flat_start_selfcheck()
        S.flat_start_ok = ok3
        L.w({"ev": "FLAT_START_SELFCHECK", "ok": ok3, "why": why3})
        if REQUIRE_FLAT_START and not ok3:
            S.halted = True
            L.w({"ev": "HALT", "reason": f"flat start selfcheck: {why3}"})
        elif not ok3 and not S.halted:
            # Not flat and not required to be: adopt what the account holds
            # so the local ledger equals exchange truth before anything is
            # quoted.  Failure to adopt is a HALT -- trading with inventory
            # this process cannot explain is exactly the 2026-07-25 shape.
            ok_adopt, why_adopt = adopt_open_positions()
            L.w({"ev": "ADOPT_POSITIONS", "ok": ok_adopt, "why": why_adopt})
            if not ok_adopt:
                S.halted = True
                L.w({"ev": "HALT",
                     "reason": f"position adoption failed: {why_adopt}"})
        if not S.halted:
            recon_check()
        if TOX_ENABLE and TOX_PRIOR_FILE:
            tox_load_prior(TOX_PRIOR_FILE)
        budget_result = budget_startup_once()
        if budget_result is None or not budget_result.allowed:
            S.halted = True
            S.budget_startup_ok = False
            L.w({"ev": "HALT", "reason":
                 "absolute budget startup gate did not clear"})
    else:
        # Shadow: run the same check for the log when creds allow, but
        # never block — shadow's job is to produce evidence.
        ok2, why2 = contract_selfcheck()
        S.contract_selfcheck_ok = ok2
        L.w({"ev": "CONTRACT_SELFCHECK", "ok": ok2, "why": why2,
             "mode": "shadow-advisory"})
    write_control_status()
    _mm_env = {k: v for k, v in sorted(os.environ.items())
               if k.startswith("MM_")}
    _resolved = {
        "MARGIN_M": MARGIN_M, "SIGMA_TAU_S": SIGMA_TAU_S,
        "SIGMA_SCALE": SIGMA_SCALE, "GAMMA_K": GAMMA_K,
        "MARGIN_FLOOR_C": MARGIN_FLOOR_C, "MAX_EDGE_SIGMA": MAX_EDGE_SIGMA,
        "ANCHOR_SHIELD": ANCHOR_SHIELD, "ANCHOR_LAG_S": ANCHOR_LAG_S,
        "TOX_ENABLE": TOX_ENABLE, "TOX_PRIOR_FILE": TOX_PRIOR_FILE,
        "PAIR": PAIR, "ORPHAN_MAKER_AGE_S": ORPHAN_MAKER_AGE_S,
        "ORPHAN_MAKER_MAX_LOSS_C": ORPHAN_MAKER_MAX_LOSS_C,
        "BRAKE_N_S": BRAKE_N_S, "BRAKE_HOLD_S": BRAKE_HOLD_S,
        "UNPAIRED_AGE_S": UNPAIRED_AGE_S, "ORDER_TTL_S": ORDER_TTL_S,
        "MIN_QUOTE_AGE_S": MIN_QUOTE_AGE_S, "MIN_REQUOTE_C": MIN_REQUOTE_C,
        "ZONE_MID_EXCLUDE_C": ZONE_MID_EXCLUDE_C,
        "ZONE_TAIL_ONLY": ZONE_TAIL_ONLY, "ECON_HALT": ECON_HALT_LOSS,
        "MAX_PEER_ENGINES": MAX_PEER_ENGINES,
    }
    _payload = json.dumps({"env": _mm_env, "resolved": _resolved},
                          sort_keys=True, default=str)
    # Operator speed directive item 1: effective config IS the experiment
    # identity -- ends every "default vs actual" argument.
    L.w({"ev": "CONFIG_SNAPSHOT",
         "params_sha256": hashlib.sha256(_payload.encode()).hexdigest()[:16],
         "env": _mm_env, "resolved": _resolved})
    L.w({"ev":"START","mode":MODE,"series":list(SERIES),"clip":CLIP,
         "improve":IMP,"sentinel_c":SENTINEL_C,"max_open_cost":MAX_OPEN_COST,
         "hard_max_open_cost":HARD_MAX_OPEN_COST,
         "kill_loss":KILL_LOSS, "pair":PAIR,
         "pair_prequote":PAIR_PREQUOTE,
         "pair_min_depth_ct":PAIR_MIN_DEPTH_CT,
         "pair_lock_c":PAIR_LOCK_C,
         "pair_min_edge_c":PAIR_MIN_EDGE_C,
         "pair_max_locked_cost":PAIR_MAX_LOCKED_COST,
         "tail_cheap_only":TAIL_CHEAP_ONLY,
         "tail_low_c":TAIL_LOW_C,
         "tail_high_c":TAIL_HIGH_C,
         "tail_cheap_max_c":TAIL_CHEAP_MAX_C,
         "unpaired_age_s":UNPAIRED_AGE_S,
         "unpaired_max_ct":UNPAIRED_MAX_CT,
         "shadow_queue_sim":SHADOW_QUEUE_SIM,
         "taker_fee_model":"general_0.07_total_centicent_ceil",
         "settlement_window":"[close-60s,close)",
         "engine_sha256":source_sha256(__file__),
         "pricing_sha256":source_sha256(rp.__file__),
         "only_ticker":ONLY_TICKER or None,
         "max_cycles":MAX_CYCLES,
         "require_flat_start":REQUIRE_FLAT_START,
         "budget_enforced":BUDGET_ENFORCE,
         "budget_anchor_micro_usd":BUDGET_ANCHOR_MICRO_USD,
         "budget_max_loss_micro_usd":BUDGET_MAX_LOSS_MICRO_USD,
         "budget_floor_micro_usd":BUDGET_FLOOR_MICRO_USD,
         "budget_path":str(BUDGET_PATH) if BUDGET_ENFORCE else None,
         "risk_generation":S.risk_generation})
    await asyncio.gather(
        cf_task(), md_task(), fills_task(), ws_fills_task(), beat(),
        control_task(), recon_task(), settlements_task(),
        pair_flatten_task(), budget_task()
    )

if __name__ == "__main__":
    asyncio.run(main())
