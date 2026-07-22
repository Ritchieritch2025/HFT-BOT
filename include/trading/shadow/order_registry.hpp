#pragma once
//
// Order registry + lifecycle state machine + exposure gauge for the shadow
// market-making engine (ACCEPTANCE_影子做市引擎 §10.1/10.2/10.4).
//
// Threading contract:
//   - The registry is SINGLE-WRITER: only the accounting thread mutates it.
//   - The hot thread never touches the registry. Its only shared surface is
//     ExposureGauge: plain atomics the accounting thread updates and the hot
//     thread reads O(1) (risk gate = a few atomic loads + compares).
//   - All storage is preallocated at construction; placing/canceling/filling
//     never allocates.
//
// Lifecycle (the only legal transitions; anything else is a bug the tests
// catch):
//   Free -> PendingNew -> Open -> {Filled, PendingCancel, Expired}
//   PendingCancel -> {Canceled, Filled}          (fills can win the race)
//   Open/PendingCancel partial fills stay in-state until terminal.

#include <atomic>
#include <cstdint>
#include <cstring>
#include <functional>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace trading::shadow {

enum class OrdState : std::uint8_t {
  Free, PendingNew, Open, PendingCancel, Filled, Canceled, Expired
};

inline const char* to_string(OrdState s) {
  switch (s) {
    case OrdState::Free: return "Free";
    case OrdState::PendingNew: return "PendingNew";
    case OrdState::Open: return "Open";
    case OrdState::PendingCancel: return "PendingCancel";
    case OrdState::Filled: return "Filled";
    case OrdState::Canceled: return "Canceled";
    case OrdState::Expired: return "Expired";
  }
  return "?";
}

inline bool legal_transition(OrdState from, OrdState to) {
  switch (from) {
    case OrdState::Free: return to == OrdState::PendingNew;
    case OrdState::PendingNew: return to == OrdState::Open;
    case OrdState::Open:
      return to == OrdState::PendingCancel || to == OrdState::Filled ||
             to == OrdState::Expired;
    case OrdState::PendingCancel:
      return to == OrdState::Canceled || to == OrdState::Filled;
    default: return false;  // terminal states never transition
  }
}

struct OrderRec {
  std::uint64_t cid = 0;          // (strategy_id << 56) | seq — deterministic
  std::uint32_t market = 0;       // dense market index (replay-local)
  std::int32_t price_cents = 0;   // yes-price of the quoted level
  std::int32_t count = 0;
  std::int32_t filled = 0;
  bool buy_yes = false;           // true = resting buy YES; false = sell YES
  OrdState state = OrdState::Free;
  std::int64_t queue_ahead = -1;  // contracts ahead at activation; -1 = unknown
  std::uint64_t place_ts_ns = 0;      // decision time
  std::uint64_t active_ts_ns = 0;     // place_ts + place latency (speed param)
  std::uint64_t cancel_eff_ns = 0;    // cancel request ts + cancel latency
  std::int32_t remaining() const { return count - filled; }
};

// Hot-thread-visible risk surface. Fixed slots, O(1) atomic reads. The
// accounting thread is the only writer.
class ExposureGauge {
 public:
  static constexpr std::size_t kMarketSlots = 4096;

  void on_open(std::uint32_t mkt, std::int32_t count, std::int32_t price_cents,
               bool buy_yes) {
    at_risk_cents_[mkt % kMarketSlots].fetch_add(
        risk_cents(count, price_cents, buy_yes), std::memory_order_relaxed);
    open_contracts_[mkt % kMarketSlots].fetch_add(count,
                                                  std::memory_order_relaxed);
    total_at_risk_cents_.fetch_add(risk_cents(count, price_cents, buy_yes),
                                   std::memory_order_relaxed);
  }
  void on_close(std::uint32_t mkt, std::int32_t count, std::int32_t price_cents,
                bool buy_yes) {
    at_risk_cents_[mkt % kMarketSlots].fetch_sub(
        risk_cents(count, price_cents, buy_yes), std::memory_order_relaxed);
    open_contracts_[mkt % kMarketSlots].fetch_sub(count,
                                                  std::memory_order_relaxed);
    total_at_risk_cents_.fetch_sub(risk_cents(count, price_cents, buy_yes),
                                   std::memory_order_relaxed);
  }

  // Hot-path read: a couple of atomic loads + compares.
  bool within(std::uint32_t mkt, std::int64_t max_market_cents,
              std::int64_t max_total_cents) const {
    return at_risk_cents_[mkt % kMarketSlots].load(std::memory_order_relaxed) <=
               max_market_cents &&
           total_at_risk_cents_.load(std::memory_order_relaxed) <=
               max_total_cents;
  }
  std::int64_t market_at_risk_cents(std::uint32_t mkt) const {
    return at_risk_cents_[mkt % kMarketSlots].load(std::memory_order_relaxed);
  }
  std::int64_t total_at_risk_cents() const {
    return total_at_risk_cents_.load(std::memory_order_relaxed);
  }

  // Worst-case loss of a resting maker order, in cents:
  // buy YES at p risks p per contract; sell YES at p risks (100 - p).
  static std::int64_t risk_cents(std::int32_t count, std::int32_t p,
                                 bool buy_yes) {
    return static_cast<std::int64_t>(count) * (buy_yes ? p : (100 - p));
  }

 private:
  std::atomic<std::int64_t> at_risk_cents_[kMarketSlots]{};
  std::atomic<std::int64_t> open_contracts_[kMarketSlots]{};
  std::atomic<std::int64_t> total_at_risk_cents_{0};
};

// Preallocated slot store + cid index + FSM enforcement. Accounting thread only.
class OrderRegistry {
 public:
  using AuditFn = std::function<void(const OrderRec&, OrdState from)>;

  explicit OrderRegistry(std::size_t capacity = 8192, AuditFn audit = nullptr)
      : slots_(capacity), audit_(std::move(audit)) {
    free_.reserve(capacity);
    for (std::size_t i = capacity; i > 0; --i)
      free_.push_back(static_cast<std::uint32_t>(i - 1));
    by_cid_.reserve(capacity * 2);
  }

  // Returns slot index or -1 when the table is full (an explicit drop, never
  // a resize on the accounting path).
  std::int64_t place(std::uint64_t cid, std::uint32_t market,
                     std::int32_t price_cents, std::int32_t count, bool buy_yes,
                     std::uint64_t place_ts_ns, std::uint64_t active_ts_ns) {
    if (free_.empty()) return -1;
    const std::uint32_t idx = free_.back();
    free_.pop_back();
    OrderRec& r = slots_[idx];
    r = OrderRec{};
    r.cid = cid;
    r.market = market;
    r.price_cents = price_cents;
    r.count = count;
    r.buy_yes = buy_yes;
    r.place_ts_ns = place_ts_ns;
    r.active_ts_ns = active_ts_ns;
    transition(idx, OrdState::PendingNew);
    by_cid_.emplace(cid, idx);
    return idx;
  }

  bool transition(std::uint32_t idx, OrdState to) {
    OrderRec& r = slots_[idx];
    const OrdState from = r.state;
    if (!legal_transition(from, to)) {
      ++illegal_transitions_;
      return false;
    }
    r.state = to;
    if (to == OrdState::Open)
      gauge_.on_open(r.market, r.remaining(), r.price_cents, r.buy_yes);
    if (is_terminal(to))
      gauge_.on_close(r.market, r.remaining(), r.price_cents, r.buy_yes);
    if (audit_) audit_(r, from);
    return true;
  }

  // A fill consumes remaining quantity; exposure shrinks by the filled part.
  void apply_fill(std::uint32_t idx, std::int32_t qty) {
    OrderRec& r = slots_[idx];
    gauge_.on_close(r.market, qty, r.price_cents, r.buy_yes);
    r.filled += qty;
    if (r.remaining() == 0) {
      const OrdState from = r.state;
      r.state = OrdState::Filled;  // direct: gauge already reduced per-fill
      if (audit_) audit_(r, from);
    }
  }

  void release(std::uint32_t idx) {
    OrderRec& r = slots_[idx];
    by_cid_.erase(r.cid);
    r.state = OrdState::Free;
    free_.push_back(idx);
  }

  OrderRec& at(std::uint32_t idx) { return slots_[idx]; }
  const OrderRec& at(std::uint32_t idx) const { return slots_[idx]; }
  std::int64_t lookup(std::uint64_t cid) const {
    auto it = by_cid_.find(cid);
    return it == by_cid_.end() ? -1 : static_cast<std::int64_t>(it->second);
  }
  static bool is_terminal(OrdState s) {
    return s == OrdState::Filled || s == OrdState::Canceled ||
           s == OrdState::Expired;
  }
  ExposureGauge& gauge() { return gauge_; }
  std::uint64_t illegal_transitions() const { return illegal_transitions_; }
  std::size_t in_use() const { return slots_.size() - free_.size(); }

 private:
  std::vector<OrderRec> slots_;
  std::vector<std::uint32_t> free_;
  std::unordered_map<std::uint64_t, std::uint32_t> by_cid_;
  ExposureGauge gauge_;
  AuditFn audit_;
  std::uint64_t illegal_transitions_ = 0;
};

}  // namespace trading::shadow
