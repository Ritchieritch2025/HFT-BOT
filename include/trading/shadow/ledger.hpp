#pragma once
//
// Position ledger + settlement accounting + post-fill markout for the shadow
// MM engine (ACCEPTANCE items 5-7 + deliverables 1-3).
//
// - Inventory is held to settlement and marked at the REAL settlement value
//   (0/100), never at mid (item 6).
// - Every fill is charged the real maker fee from the fail-closed
//   FeeSchedule; a market with UNKNOWN fees contributes NO net-PnL claim —
//   it is counted and excluded (WO-E doctrine).
// - Markout (item 3 / deliverable 3): signed drift of the last trade price
//   `horizon_ns` after each fill. Systematically negative markout = the
//   adverse-selection asymmetry is alive in the fill model.
//
// Accounting thread only. NDJSON emission is the caller's business.

#include "trading/shadow/fees.hpp"
#include "trading/shadow/fill_sim.hpp"

#include <cstdint>
#include <deque>
#include <string>
#include <unordered_map>
#include <vector>

namespace trading::shadow {

struct MarketBook {
  std::string ticker;
  std::string family;             // reporting family (category)
  std::int64_t bought = 0;        // YES contracts bought (as maker)
  std::int64_t bought_cents = 0;  // sum of buy price*qty
  std::int64_t sold = 0;
  std::int64_t sold_cents = 0;
  std::int64_t fee_cents = 0;
  bool fee_known = false;
  bool settled = false;
  bool settled_yes = false;
  std::int32_t last_trade_cents = -1;
};

struct FamilyReport {
  std::int64_t markets = 0, settled_markets = 0;
  std::int64_t orders = 0, fills = 0, filled_contracts = 0;
  std::int64_t quoted_contracts = 0;
  std::int64_t gross_pnl_cents = 0;   // settlement PnL before fees
  std::int64_t fee_cents = 0;
  std::int64_t net_pnl_cents = 0;     // only markets with KNOWN fees
  std::int64_t excluded_fee_unknown_markets = 0;
  double markout_sum_cents = 0;       // signed, per filled contract
  std::int64_t markout_n = 0;
};

class Ledger {
 public:
  Ledger(const FeeSchedule& fees, std::uint64_t markout_horizon_ns)
      : fees_(fees), horizon_ns_(markout_horizon_ns) {}

  std::uint32_t market_index(const std::string& ticker,
                             const std::string& family) {
    auto it = index_.find(ticker);
    if (it != index_.end()) return it->second;
    const auto idx = static_cast<std::uint32_t>(books_.size());
    MarketBook b;
    b.ticker = ticker;
    b.family = family;
    b.fee_known = fees_.known(ticker);
    books_.push_back(std::move(b));
    index_.emplace(ticker, idx);
    return idx;
  }

  void on_order_placed(std::uint32_t mkt, std::int32_t count) {
    ++orders_;
    books_[mkt];  // bounds guard in debug
    quoted_contracts_ += count;
  }

  void on_fill(const Fill& f) {
    MarketBook& b = books_[f.market];
    if (f.buy_yes) {
      b.bought += f.qty;
      b.bought_cents += static_cast<std::int64_t>(f.qty) * f.price_cents;
    } else {
      b.sold += f.qty;
      b.sold_cents += static_cast<std::int64_t>(f.qty) * f.price_cents;
    }
    if (b.fee_known) {
      // Real maker fee, per fill, rounded up (item 5).
      b.fee_cents += *fees_.maker_fee_cents(b.ticker, f.qty, f.price_cents);
    }
    ++fills_;
    filled_contracts_ += f.qty;
    pending_markouts_.push_back(
        Markout{f.market, f.price_cents, f.qty, f.buy_yes, f.ts_ns});
  }

  // Tape trade — advances markout evaluation and the per-market last price.
  void on_trade(std::uint32_t mkt, std::int32_t price_cents,
                std::uint64_t ts_ns) {
    books_[mkt].last_trade_cents = price_cents;
    while (!pending_markouts_.empty() &&
           ts_ns >= pending_markouts_.front().fill_ts_ns + horizon_ns_) {
      const Markout m = pending_markouts_.front();
      pending_markouts_.pop_front();
      const std::int32_t ref = books_[m.market].last_trade_cents;
      if (ref < 0) continue;
      // Signed per-contract drift: for a buy, price falling after the fill is
      // adverse (negative); for a sell, price rising is adverse.
      const double drift =
          m.buy_yes ? (ref - m.price_cents) : (m.price_cents - ref);
      markout_sum_ += drift * m.qty;
      markout_n_ += m.qty;
      per_family_markout_[books_[m.market].family].first += drift * m.qty;
      per_family_markout_[books_[m.market].family].second += m.qty;
    }
  }

  void on_settle(std::uint32_t mkt, bool yes_wins) {
    MarketBook& b = books_[mkt];
    b.settled = true;
    b.settled_yes = yes_wins;
  }

  // Settlement PnL of one market, gross of fees, in cents (item 6).
  static std::int64_t gross_pnl_cents(const MarketBook& b) {
    const std::int64_t s = b.settled_yes ? 100 : 0;
    return (b.bought * s - b.bought_cents) + (b.sold_cents - b.sold * s);
  }

  std::unordered_map<std::string, FamilyReport> report() const {
    std::unordered_map<std::string, FamilyReport> out;
    for (const auto& b : books_) {
      FamilyReport& r = out[b.family];
      ++r.markets;
      if (!b.settled) continue;
      ++r.settled_markets;
      const std::int64_t gross = gross_pnl_cents(b);
      r.gross_pnl_cents += gross;
      if (b.fee_known) {
        r.fee_cents += b.fee_cents;
        r.net_pnl_cents += gross - b.fee_cents;
      } else if (b.bought + b.sold > 0) {
        ++r.excluded_fee_unknown_markets;
      }
    }
    for (auto& [fam, r] : out) {
      auto it = per_family_markout_.find(fam);
      if (it != per_family_markout_.end()) {
        r.markout_sum_cents = it->second.first;
        r.markout_n = it->second.second;
      }
    }
    // Order/fill counters are engine-global; families share one funnel line.
    for (auto& [fam, r] : out) {
      r.orders = orders_;
      r.fills = fills_;
      r.filled_contracts = filled_contracts_;
      r.quoted_contracts = quoted_contracts_;
    }
    return out;
  }

  // Deliverable 2: fills / quoted contracts, engine-wide.
  double fill_rate() const {
    return quoted_contracts_ ? static_cast<double>(filled_contracts_) /
                                   static_cast<double>(quoted_contracts_)
                             : 0.0;
  }
  double avg_markout_cents() const {
    return markout_n_ ? markout_sum_ / static_cast<double>(markout_n_) : 0.0;
  }
  std::int64_t fills() const { return fills_; }
  std::int64_t orders() const { return orders_; }
  const MarketBook& book(std::uint32_t mkt) const { return books_[mkt]; }
  std::size_t markets() const { return books_.size(); }

 private:
  struct Markout {
    std::uint32_t market;
    std::int32_t price_cents;
    std::int32_t qty;
    bool buy_yes;
    std::uint64_t fill_ts_ns;
  };

  const FeeSchedule& fees_;
  std::uint64_t horizon_ns_;
  std::vector<MarketBook> books_;
  std::unordered_map<std::string, std::uint32_t> index_;
  std::deque<Markout> pending_markouts_;
  std::unordered_map<std::string, std::pair<double, std::int64_t>>
      per_family_markout_;
  std::int64_t orders_ = 0, fills_ = 0, filled_contracts_ = 0,
               quoted_contracts_ = 0;
  double markout_sum_ = 0;
  std::int64_t markout_n_ = 0;
};

}  // namespace trading::shadow
