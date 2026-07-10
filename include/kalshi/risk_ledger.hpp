// risk_ledger.hpp — the five-layer reservation ledger (W-K3, contract #7).
//
// The heart of pre-trade risk. In-process, in-memory (contracts #1/#6): this
// header links with ZERO I/O dependencies — no network, no DuckDB, no strategy
// code. Money is INTEGER micro-dollars (E6, `Micros`), never float (D5). E6
// is used deliberately over the roadmap's E4: W-K1 verified live that account
// money carries six decimals (a real position exposure was "4.726960"), so E4
// would be lossy narrowing of the very quantity this ledger caps. An order's
// exposure in micros is EXACT from the existing fixed-point types:
//     exposure_micros = CountFp(contracts x 10^2) * PriceE4(dollars x 10^4)
//                     = contracts * dollars * 10^6   (micro-dollars)
//
// FIVE LAYERS reserved ATOMICALLY at slot-take; ANY layer short => the whole
// order is REJECTED and NOTHING is deducted (the "look then send" race — two
// same-millisecond orders each seeing headroom and both firing — is
// impossible because reserve() holds one lock across check-and-deduct):
//   (1) per-market   key = market ticker
//   (2) per-event    key = event ticker
//   (3) per-factor   key = same underlying / same outcome direction (all
//                    alternate lines of one game, all BTC brackets one way).
//                    An EMPTY factor key is the fail-closed `unknown_factor`
//                    class: every unclassifiable order shares ONE bucket with
//                    the most conservative cap, so they aggregate and throttle
//                    hardest rather than each slipping through.
//   (4) total exposure  single global budget
//   (5) daily loss      breaker: once realized day loss reaches the cap, all
//                    NEW-risk orders are refused (fail-closed; S2).
// Layers (2) and (3) are the additions the interview post-mortems demanded
// (Eggsy correlated lines, NHL template legs); the roadmap's old Phase-3 list
// was only (1)(4)(5)+per-order, which cannot stop a factor-level blowup — the
// red-first test proves it.
//
// REDUCE-RISK orders bypass reservation and are ALWAYS admitted (Q8: the exit
// quote is never suppressed at max inventory — the rodlaf deadlock).
//
// LIFECYCLE: reserve() -> (fill) settle(filled, loss) keeps the filled part +
// refunds the remainder; (fail/timeout) refund() returns the whole. Invariant,
// always and per handle: reserved == settled + refunded.
//
// client_order_id (contract #9): each reservation gets a process-monotonic
// slot index; client_order_id(res, strategy, ts) derives a deterministic id
// from it (via wire::client_order_id), so a retry that reuses the SAME
// reservation reuses the SAME id — a duplicate can fill at most once.

#ifndef KALSHI_RISK_LEDGER_HPP
#define KALSHI_RISK_LEDGER_HPP

#include <cstdint>
#include <limits>
#include <mutex>
#include <string>
#include <unordered_map>

#include "kalshi/wire.hpp"

namespace kalshi::risk {

using Micros = std::int64_t;  // dollars x 10^6 (E6 micro-dollars)

inline constexpr Micros kNoCap = std::numeric_limits<Micros>::max();
inline constexpr const char* kUnknownFactor = "__unknown_factor__";

// Exact E6 exposure from the repo's fixed-point types. count_fp is CountFp
// (contracts x 10^2, >= 0); price_e4 is PriceE4 (dollars x 10^4, [0,10000]).
inline Micros exposure_micros(std::int64_t count_fp, std::int32_t price_e4) {
  return count_fp * static_cast<Micros>(price_e4);
}

enum class Layer : std::uint8_t {
  None = 0, Market, Event, Factor, Total, DayLoss
};

inline const char* to_string(Layer l) {
  switch (l) {
    case Layer::Market:  return "per_market";
    case Layer::Event:   return "per_event";
    case Layer::Factor:  return "per_factor";
    case Layer::Total:   return "total";
    case Layer::DayLoss: return "day_loss";
    default:             return "none";
  }
}

struct OrderReq {
  std::string market;          // layer (1) key
  std::string event;           // layer (2) key
  std::string factor;          // layer (3) key; EMPTY => unknown_factor bucket
  Micros exposure = 0;         // E6, >= 0
  bool reduces_risk = false;   // Q8: always admitted, reserves nothing
};

struct Reservation {
  bool ok = false;
  Layer rejected = Layer::None;   // the short layer when !ok
  std::uint64_t slot = 0;         // process-monotonic slot index (contract #9);
                                  // 0 = invalid / reduce-risk passthrough
  Micros amount = 0;              // reserved exposure (0 for reduce-risk)
  bool reduce = false;            // this was a reduce-risk passthrough
  // keys captured so settle()/refund() hit the exact same buckets
  std::string market, event, factor;
  Micros settled = 0;             // filled part kept (bookkeeping/conservation)
  bool closed = false;            // settle() or refund() already applied
};

class RiskLedger {
 public:
  struct Caps {
    Micros per_market = kNoCap;   // default per-key caps
    Micros per_event  = kNoCap;
    Micros per_factor = kNoCap;
    Micros total      = kNoCap;
    Micros day_loss   = kNoCap;   // realized-loss breaker
  };

  explicit RiskLedger(Caps caps) : caps_(caps) {}

  // Per-key cap overrides (e.g. a tighter cap on one hot market). The unknown
  // factor bucket can be given its own (tightest) cap via set_factor_cap.
  void set_market_cap(const std::string& k, Micros c) { market_cap_[k] = c; }
  void set_event_cap(const std::string& k, Micros c)  { event_cap_[k] = c; }
  void set_factor_cap(const std::string& k, Micros c) { factor_cap_[k] = c; }

  // Atomic five-layer reserve. Returns ok=false + the short layer, having
  // deducted NOTHING, when any layer lacks headroom. Reduce-risk orders are
  // admitted unconditionally with amount=0 (Q8).
  Reservation reserve(const OrderReq& req) {
    std::lock_guard<std::mutex> lock(mu_);
    Reservation r;
    r.market = req.market;
    r.event = req.event;
    r.factor = req.factor.empty() ? kUnknownFactor : req.factor;
    if (req.reduces_risk) {          // exit/reduce: never blocked, reserves 0
      r.ok = true; r.reduce = true; r.amount = 0; r.slot = 0;
      return r;
    }
    // (5) day-loss breaker first: a tripped breaker refuses ALL new risk.
    if (day_loss_ >= caps_.day_loss) { r.rejected = Layer::DayLoss; return r; }
    const Micros e = req.exposure;
    // Check every exposure layer's headroom BEFORE deducting any (atomic).
    if (used_(market_used_, r.market) + e > cap_(market_cap_, caps_.per_market, r.market)) {
      r.rejected = Layer::Market; return r; }
    if (used_(event_used_, r.event) + e > cap_(event_cap_, caps_.per_event, r.event)) {
      r.rejected = Layer::Event; return r; }
    if (used_(factor_used_, r.factor) + e > cap_(factor_cap_, caps_.per_factor, r.factor)) {
      r.rejected = Layer::Factor; return r; }
    if (total_used_ + e > caps_.total) { r.rejected = Layer::Total; return r; }
    // all five clear -> commit to all layers
    market_used_[r.market] += e;
    event_used_[r.event] += e;
    factor_used_[r.factor] += e;
    total_used_ += e;
    r.ok = true; r.amount = e; r.slot = ++slot_seq_;
    return r;
  }

  // A fill consumed `filled` (0 <= filled <= reserved); keep it, refund the
  // remainder to every layer. `realized_loss` (>= 0) advances the day-loss
  // breaker. Idempotent-guarded: a second settle/refund on the same handle is
  // a no-op (so a retry path cannot double-refund).
  void settle(Reservation& r, Micros filled, Micros realized_loss = 0) {
    std::lock_guard<std::mutex> lock(mu_);
    if (r.reduce || r.closed || !r.ok) { r.closed = true; return; }
    if (filled < 0) filled = 0;
    if (filled > r.amount) filled = r.amount;
    const Micros refund = r.amount - filled;
    release_(r, refund);           // return only the unfilled part
    r.settled = filled;
    if (realized_loss > 0) day_loss_ += realized_loss;
    r.closed = true;
  }

  // Full refund (send failure / timeout / rejected before any fill).
  void refund(Reservation& r) {
    std::lock_guard<std::mutex> lock(mu_);
    if (r.reduce || r.closed || !r.ok) { r.closed = true; return; }
    release_(r, r.amount);
    r.settled = 0;
    r.closed = true;
  }

  // Deterministic client_order_id for a reservation (contract #9): stable
  // across retries that reuse the same reservation slot.
  std::string client_order_id(const Reservation& r, std::uint8_t strategy_id,
                              std::uint64_t ts_ns) const {
    wire::ExecPayload p;
    p.strategy_id = strategy_id;
    p.seq = r.slot;              // process-monotonic, slot-indexed
    p.ts_ns = ts_ns;
    return wire::client_order_id(p);
  }

  // ---- introspection (tests / telemetry; all read under the lock) ----
  Micros used_total() const { std::lock_guard<std::mutex> l(mu_); return total_used_; }
  Micros used_market(const std::string& k) const {
    std::lock_guard<std::mutex> l(mu_); return used_(market_used_, k); }
  Micros used_event(const std::string& k) const {
    std::lock_guard<std::mutex> l(mu_); return used_(event_used_, k); }
  Micros used_factor(const std::string& k) const {
    std::lock_guard<std::mutex> l(mu_);
    return used_(factor_used_, k.empty() ? kUnknownFactor : k); }
  Micros day_loss() const { std::lock_guard<std::mutex> l(mu_); return day_loss_; }

 private:
  static Micros used_(const std::unordered_map<std::string, Micros>& m,
                      const std::string& k) {
    auto it = m.find(k);
    return it == m.end() ? 0 : it->second;
  }
  static Micros cap_(const std::unordered_map<std::string, Micros>& over,
                     Micros dflt, const std::string& k) {
    auto it = over.find(k);
    return it == over.end() ? dflt : it->second;
  }
  void release_(const Reservation& r, Micros amt) {
    market_used_[r.market] -= amt;
    event_used_[r.event] -= amt;
    factor_used_[r.factor] -= amt;
    total_used_ -= amt;
  }

  mutable std::mutex mu_;
  Caps caps_;
  std::unordered_map<std::string, Micros> market_cap_, event_cap_, factor_cap_;
  std::unordered_map<std::string, Micros> market_used_, event_used_, factor_used_;
  Micros total_used_ = 0;
  Micros day_loss_ = 0;
  std::uint64_t slot_seq_ = 0;
};

}  // namespace kalshi::risk

#endif  // KALSHI_RISK_LEDGER_HPP
