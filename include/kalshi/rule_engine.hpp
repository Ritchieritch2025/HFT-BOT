// rule_engine.hpp — the always-on defensive rules (W-K4, PLAN_RISK_KILLSWITCH
// §3). A PURE, in-memory, I/O-free library: it consumes a tape of engine
// events and EMITS decisions; it transmits nothing (shadow-style, S5). Four
// rules, each a documented invariant of a safe order engine:
//
//   (1) DEAD-MAN EXPIRY — every resting order carries an expiry_ns. If the
//       engine loses its heartbeat (now - last_heartbeat > dead_man_window),
//       ALL resting orders are marked Expire — the exchange-side dead-man
//       (order groups / TTL) is what actually cancels them live; here we
//       decide it. A per-order expiry_ns also expires a single stale order.
//   (2) CANCEL-ON-DISCONNECT (S6) — a disconnect emits Cancel for EVERY
//       resting order. Never a silent drop.
//   (3) DAY-LOSS CIRCUIT BREAKER — when the risk ledger's layer-⑤ realized
//       loss trips (fed in as `day_loss_tripped`), the engine emits QuoteStop
//       + a one-shot PanicRecommend. The breaker is evaluated on EVERY tick
//       and EVERY new-quote intent, so it trips BETWEEN book updates, not
//       only at requote time (Q8 multi-fill window).
//   (4) ORDER-RATE LIMITER — new-quote intents spend a write-token budget
//       (TokenBucketI64, roadmap 300/s). On saturation, requotes are SHED.
//       **Cancels are NEVER shed** — cancel starvation is a classic incident;
//       cancel intents bypass the limiter entirely and always Admit.
//
// Clock is caller-injected (every method takes now_ns) so tests drive a fake
// clock — same discipline as token_bucket.hpp. Single mutex; this is a
// control-path guard, not the market-data hot path.

#ifndef KALSHI_RULE_ENGINE_HPP
#define KALSHI_RULE_ENGINE_HPP

#include <cstdint>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

#include "kalshi/token_bucket.hpp"

namespace kalshi::rules {

enum class Action : std::uint8_t {
  Admit,          // intent passes the guards
  Shed,           // rate-limited: a requote dropped (never a cancel)
  Cancel,         // cancel a resting order (disconnect)
  Expire,         // dead-man expiry of a resting order
  QuoteStop,      // stop quoting (day-loss breaker tripped)
  PanicRecommend, // recommend running the panic kill switch (one-shot)
};

inline const char* to_string(Action a) {
  switch (a) {
    case Action::Admit:          return "admit";
    case Action::Shed:           return "shed";
    case Action::Cancel:         return "cancel";
    case Action::Expire:         return "expire";
    case Action::QuoteStop:      return "quote_stop";
    case Action::PanicRecommend: return "panic_recommend";
  }
  return "?";
}

struct Decision {
  Action action;
  std::string order_id;   // set for Cancel/Expire
  std::string reason;
};

struct RestingOrder {
  std::string id;
  std::uint64_t expiry_ns = 0;   // 0 = no per-order TTL (engine dead-man only)
};

class RuleEngine {
 public:
  struct Config {
    std::uint64_t dead_man_window_ns = 0;  // 0 = engine dead-man disabled
    std::int64_t write_rate = 300;         // tokens/sec (roadmap)
    std::int64_t write_capacity = 600;     // burst capacity
    std::int64_t requote_cost = 1;         // tokens per new-quote intent
  };

  explicit RuleEngine(Config cfg)
      : cfg_(cfg), bucket_(cfg.write_rate, cfg.write_capacity) {}

  // ---- state feeds -------------------------------------------------------
  void on_heartbeat(std::uint64_t now_ns) {
    std::lock_guard<std::mutex> l(mu_);
    if (!armed_ || now_ns > ref_ns_) ref_ns_ = now_ns;   // monotonic reference
    armed_ = true;
  }
  // now_ns is REQUIRED (audit B1): adding a resting order ARMS the engine
  // dead-man from that instant, so an engine that never heartbeats still
  // expires its orders after the window (an engine dead from birth is the
  // worst case a dead-man must catch — arming only on a prior heartbeat was
  // fail-open). A later heartbeat advances the reference.
  void add_resting(const RestingOrder& o, std::uint64_t now_ns) {
    std::lock_guard<std::mutex> l(mu_);
    resting_[o.id] = o;
    if (!armed_) { ref_ns_ = now_ns; armed_ = true; }
  }
  void remove_resting(const std::string& id) {
    std::lock_guard<std::mutex> l(mu_);
    resting_.erase(id);
  }
  std::size_t resting_count() const {
    std::lock_guard<std::mutex> l(mu_); return resting_.size();
  }

  // (2) cancel-on-disconnect: Cancel for EVERY resting order (S6).
  std::vector<Decision> on_disconnect(std::uint64_t /*now_ns*/) {
    std::lock_guard<std::mutex> l(mu_);
    std::vector<Decision> out;
    for (auto& [id, o] : resting_)
      out.push_back({Action::Cancel, id, "disconnect"});
    resting_.clear();               // they are being cancelled; forget them
    return out;
  }

  // (1)+(3) per-tick evaluation: dead-man expiry (engine heartbeat loss OR a
  // per-order TTL past due) + the day-loss breaker (evaluated EVERY tick so it
  // trips between book updates, Q8). `day_loss_tripped` is the ledger ⑤ state.
  std::vector<Decision> on_tick(std::uint64_t now_ns, bool day_loss_tripped) {
    std::lock_guard<std::mutex> l(mu_);
    std::vector<Decision> out;
    // `now_ns > ref_ns_` guards the unsigned subtraction (audit B2): an
    // out-of-order / clock-skewed tick (now < ref) must NOT wrap to a huge
    // delta and spuriously mass-expire the book — mirror token_bucket.hpp's
    // non-monotonic guard.
    const bool engine_dead =
        cfg_.dead_man_window_ns != 0 && armed_ && now_ns > ref_ns_ &&
        now_ns - ref_ns_ > cfg_.dead_man_window_ns;
    if (engine_dead) {
      for (auto& [id, o] : resting_)
        out.push_back({Action::Expire, id, "dead_man: heartbeat lost"});
      resting_.clear();
    } else {
      // per-order TTL: expire only the individually-stale ones
      for (auto it = resting_.begin(); it != resting_.end();) {
        if (it->second.expiry_ns != 0 && now_ns >= it->second.expiry_ns) {
          out.push_back({Action::Expire, it->first, "dead_man: order ttl"});
          it = resting_.erase(it);
        } else {
          ++it;
        }
      }
    }
    emit_breaker_(out, day_loss_tripped);
    return out;
  }

  // (3)+(4) a NEW-QUOTE intent. Breaker first (QuoteStop beats admission), then
  // the rate limiter (Shed on saturation). Never returns Cancel.
  Decision on_quote_intent(std::uint64_t now_ns, bool day_loss_tripped) {
    std::lock_guard<std::mutex> l(mu_);
    // Breaker beats admission. The one-shot PanicRecommend is surfaced by
    // on_tick (the breaker's canonical evaluation point), so a quote intent
    // only refuses here — it does not consume the recommend.
    if (day_loss_tripped)
      return {Action::QuoteStop, "", "day_loss breaker: no new quotes"};
    if (!bucket_.try_reserve(cfg_.requote_cost, static_cast<std::int64_t>(now_ns)))
      return {Action::Shed, "", "rate_limit: write budget exhausted"};
    return {Action::Admit, "", "quote admitted"};
  }

  // (4) a CANCEL intent. Cancels are NEVER shed and NEVER rate-limited —
  // cancel starvation is the classic incident. Always Admit.
  Decision on_cancel_intent(const std::string& order_id, std::uint64_t /*now*/) {
    std::lock_guard<std::mutex> l(mu_);
    resting_.erase(order_id);
    return {Action::Cancel, order_id, "cancel admitted (never shed)"};
  }

 private:
  // Day-loss breaker emission: QuoteStop + a ONE-SHOT PanicRecommend the first
  // time the breaker is seen tripped (so a burst doesn't spam the recommend).
  void emit_breaker_(std::vector<Decision>& out, bool tripped) {
    if (!tripped) { panic_recommended_ = false; return; }
    out.push_back({Action::QuoteStop, "", "day_loss breaker tripped"});
    if (!panic_recommended_) {
      out.push_back({Action::PanicRecommend, "", "day_loss: consider panic"});
      panic_recommended_ = true;
    }
  }

  mutable std::mutex mu_;
  Config cfg_;
  TokenBucketI64 bucket_;
  std::unordered_map<std::string, RestingOrder> resting_;
  std::uint64_t ref_ns_ = 0;    // last "engine known alive" time (hb or 1st order)
  bool armed_ = false;          // do we have a dead-man reference at all
  bool panic_recommended_ = false;
};

}  // namespace kalshi::rules

#endif  // KALSHI_RULE_ENGINE_HPP
