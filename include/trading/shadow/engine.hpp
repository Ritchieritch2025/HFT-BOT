#pragma once
//
// ShadowEngine — glues registry + fill simulator + ledger into the record-only
// engine driven by tape events (replay today; the same consumption loop is the
// accounting thread when driven live off the Vyukov ring — 10.6: shadow and
// live share structures, only the send route differs).
//
// The engine is fully deterministic: identical tape + identical params =>
// identical ledger (D3-D01 spirit; the test hashes two runs).

#include "trading/shadow/fees.hpp"
#include "trading/shadow/fill_sim.hpp"
#include "trading/shadow/ledger.hpp"
#include "trading/shadow/order_registry.hpp"

#include <algorithm>
#include <cstdint>
#include <string>
#include <vector>

namespace trading::shadow {

class ShadowEngine {
 public:
  ShadowEngine(const FeeSchedule& fees, Track track, SpeedParams speed,
               std::uint64_t markout_horizon_ns = 60'000'000'000ULL)
      : reg_(8192),
        sim_(reg_, track),
        ledger_(fees, markout_horizon_ns),
        speed_(speed) {}

  std::uint32_t market(const std::string& ticker, const std::string& family) {
    return ledger_.market_index(ticker, family);
  }

  // Place a maker order (decision at ts). displayed_at_level = displayed
  // contracts at our level for the Queue track; -1 = unknown (fail closed).
  // Returns cid, or 0 when the registry is full (explicit drop).
  std::uint64_t place(std::uint32_t mkt, std::int32_t price_cents,
                      std::int32_t count, bool buy_yes, std::uint64_t ts,
                      std::int64_t displayed_at_level = -1) {
    const std::uint64_t cid = ++cid_seq_;
    const std::int64_t idx =
        reg_.place(cid, mkt, price_cents, count, buy_yes, ts,
                   ts + speed_.place_latency_ns);
    if (idx < 0) {
      ++dropped_full_;
      return 0;
    }
    ledger_.on_order_placed(mkt, count);
    pending_activation_.push_back(
        Pending{static_cast<std::uint32_t>(idx), displayed_at_level});
    if (mkt >= by_market_.size()) by_market_.resize(mkt + 1);
    by_market_[mkt].push_back(static_cast<std::uint32_t>(idx));
    return cid;
  }

  // Cancel decision at ts: effective after the cancel latency (item 4) —
  // fills inside the window still land, that exposure is the point.
  void cancel(std::uint64_t cid, std::uint64_t ts) {
    const std::int64_t idx = reg_.lookup(cid);
    if (idx < 0) return;
    OrderRec& r = reg_.at(static_cast<std::uint32_t>(idx));
    if (OrderRegistry::is_terminal(r.state) || r.state == OrdState::PendingCancel)
      return;
    r.cancel_eff_ns = ts + speed_.cancel_latency_ns;
    if (r.state == OrdState::Open)
      reg_.transition(static_cast<std::uint32_t>(idx), OrdState::PendingCancel);
    canceling_.push_back(static_cast<std::uint32_t>(idx));
  }

  // Advance engine time to `ts` (tape order), then consume one trade print.
  void on_trade(std::uint32_t mkt, std::int32_t yes_price, std::int32_t size,
                bool taker_yes, std::uint64_t ts) {
    advance(ts);
    sim_.on_trade(mkt, yes_price, size, taker_yes, ts, fills_scratch_);
    for (const Fill& f : fills_scratch_) {
      ledger_.on_fill(f);
      if (reg_.at(f.order_idx).state == OrdState::Filled)
        reg_.release(f.order_idx);  // recycle the slot (registry is finite)
    }
    fills_scratch_.clear();
    ledger_.on_trade(mkt, yes_price, ts);
  }

  void on_settle(std::uint32_t mkt, bool yes_wins, std::uint64_t ts) {
    advance(ts);
    // Sweep only this market's orders (recycled slot indices are validated
    // against market + live state before acting).
    if (mkt < by_market_.size()) {
      for (const std::uint32_t i : by_market_[mkt]) {
        OrderRec& r = reg_.at(i);
        if (r.market != mkt) continue;  // slot recycled to another market
        if (r.state == OrdState::Open || r.state == OrdState::PendingCancel) {
          sim_.remove(i);
          reg_.transition(i, r.state == OrdState::PendingCancel
                                 ? OrdState::Canceled
                                 : OrdState::Expired);
          reg_.release(i);
        }
      }
      by_market_[mkt].clear();
    }
    ledger_.on_settle(mkt, yes_wins);
  }

  // Move activation/cancel clocks forward. Called by every tape event.
  void advance(std::uint64_t now) {
    for (std::size_t i = 0; i < pending_activation_.size();) {
      const Pending p = pending_activation_[i];
      OrderRec& r = reg_.at(p.idx);
      if (now >= r.active_ts_ns) {
        // Cancel raced the activation and already covers it? Then it opens
        // and immediately begins dying (still fillable until cancel_eff).
        reg_.transition(p.idx, OrdState::Open);
        if (!sim_.activate(p.idx, p.displayed_at_level)) {
          reg_.transition(p.idx, OrdState::Expired);  // ineligible this track
          ++ineligible_;
        } else if (r.cancel_eff_ns != 0) {
          reg_.transition(p.idx, OrdState::PendingCancel);
        }
        pending_activation_[i] = pending_activation_.back();
        pending_activation_.pop_back();
      } else {
        ++i;
      }
    }
    // Cancel sweep: only orders with a pending cancel are ever visited.
    for (std::size_t k = 0; k < canceling_.size();) {
      const std::uint32_t i = canceling_[k];
      OrderRec& r = reg_.at(i);
      bool done = false;
      if (r.state == OrdState::PendingCancel) {
        if (now >= r.cancel_eff_ns) {
          sim_.remove(i);
          reg_.transition(i, OrdState::Canceled);
          reg_.release(i);
          done = true;
        }
      } else if (r.state != OrdState::PendingNew &&
                 r.state != OrdState::Open) {
        done = true;  // filled/expired/recycled before the cancel landed
      }
      if (done) {
        canceling_[k] = canceling_.back();
        canceling_.pop_back();
      } else {
        ++k;
      }
    }
  }

  Ledger& ledger() { return ledger_; }
  OrderRegistry& registry() { return reg_; }
  FillSim& sim() { return sim_; }
  std::uint64_t dropped_full() const { return dropped_full_; }
  std::uint64_t ineligible() const { return ineligible_; }
  const SpeedParams& speed() const { return speed_; }

 private:
  struct Pending {
    std::uint32_t idx;
    std::int64_t displayed_at_level;
  };
  std::uint32_t reg_size() const { return 8192; }

  OrderRegistry reg_;
  FillSim sim_;
  Ledger ledger_;
  SpeedParams speed_;
  std::uint64_t cid_seq_ = 0;
  std::uint64_t dropped_full_ = 0;
  std::uint64_t ineligible_ = 0;
  std::vector<Pending> pending_activation_;
  std::vector<Fill> fills_scratch_;
  std::vector<std::uint32_t> canceling_;
  std::vector<std::vector<std::uint32_t>> by_market_;
};

// Reference symmetric quoter — NOT a strategy deliverable, just enough brain
// to exercise place/cancel/fill on real tape: keep one sell-YES quote `off`
// cents above the last print (and optionally a buy-YES below), requote when
// the last price moves, per-market inventory cap.
class SimpleQuoter {
 public:
  SimpleQuoter(ShadowEngine& eng, std::int32_t off, std::int32_t size,
               std::int32_t max_inventory, bool quote_buy_side)
      : eng_(eng), off_(off), size_(size), cap_(max_inventory),
        buy_side_(quote_buy_side) {}

  void on_trade(std::uint32_t mkt, std::int32_t last, std::uint64_t ts) {
    if (mkt >= state_.size()) state_.resize(mkt + 1);
    Quote& q = state_[mkt];
    const std::int32_t ask = last + off_, bid = last - off_;
    if (q.ask_px != ask && ask >= 2 && ask <= 99) {
      if (q.ask_cid) eng_.cancel(q.ask_cid, ts);
      const auto& b = eng_.ledger().book(mkt);
      if (b.sold - b.bought < cap_) {
        q.ask_cid = eng_.place(mkt, ask, size_, /*buy_yes=*/false, ts);
        q.ask_px = ask;
      }
    }
    if (buy_side_ && q.bid_px != bid && bid >= 1 && bid <= 98) {
      if (q.bid_cid) eng_.cancel(q.bid_cid, ts);
      const auto& b = eng_.ledger().book(mkt);
      if (b.bought - b.sold < cap_) {
        q.bid_cid = eng_.place(mkt, bid, size_, /*buy_yes=*/true, ts);
        q.bid_px = bid;
      }
    }
  }

 private:
  struct Quote {
    std::uint64_t ask_cid = 0, bid_cid = 0;
    std::int32_t ask_px = -1, bid_px = -1;
  };
  ShadowEngine& eng_;
  std::int32_t off_, size_, cap_;
  bool buy_side_;
  std::vector<Quote> state_;
};

}  // namespace trading::shadow
