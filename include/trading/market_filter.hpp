#pragma once
//
// Phase 4 — fast in-memory market filter (MARKET_DATA_PIPELINE_FILTER_DASHBOARD).
//
// Pure, deterministic, allocation-light: it reads a snapshot of hot in-memory
// market state and a set of operator controls, and returns a verdict + a
// FilteredMarketSignal. NO I/O, NO DuckDB, NO blocking — safe to call from a
// dashboard-serving thread over the latest published snapshots; it never sits
// inside the WebSocket read loop.
//
// Fixed-point only (trading::PriceE4 probability-space, trading::CountFp
// contracts x100). No float math on prices/sizes/volume/open-interest — the
// same rule the warehouse converter follows.

#include "trading/fixedpoint.hpp"

#include <algorithm>
#include <cstdint>
#include <string>
#include <string_view>

namespace trading {

// A snapshot of one market's hot state, assembled from the live orderbook (top
// of book + sizes), the trade tape (last trade size), and market/catalog
// metadata (volume, open interest, status, event/series/category). Absent price
// levels use 0 (kPriceMin); absent sizes/volume/OI use 0.
struct MarketState {
  std::string market_ticker;
  std::string event_ticker;
  std::string series_ticker;
  std::string category;
  std::string status;              // e.g. "active"

  PriceE4 yes_bid = 0;             // 0 => no bid resting
  PriceE4 yes_ask = 0;             // 0 => no ask (or 1.0000 - best no bid)
  CountFp yes_bid_size = 0;
  CountFp yes_ask_size = 0;
  CountFp last_trade_size = 0;
  CountFp volume = 0;
  CountFp open_interest = 0;

  std::int64_t freshness_ms = 0;   // age of the most recent update for this market
};

// Operator controls. Sentinel conventions keep every control independently
// optional: negative price/size thresholds and non-positive max_stale_ms mean
// "unset"; empty selector strings mean "any". Defaults pass everything.
struct FilterControls {
  PriceE4 max_spread = -1;         // < 0 => no spread cap
  CountFp min_volume = -1;
  CountFp min_open_interest = -1;
  CountFp min_bid_size = -1;
  CountFp min_ask_size = -1;
  PriceE4 price_min = kPriceMin;   // reference price (yes_bid, or yes_ask if no bid)
  PriceE4 price_max = kPriceMax;
  std::int64_t max_stale_ms = -1;  // <= 0 => no freshness cap
  std::string category;            // empty => any
  std::string series;              // empty => any
  std::string status;              // empty => any
  bool require_two_sided = true;   // require both a bid and an ask
};

// Output for a market the operator cares about (whether or not it passed — the
// dashboard tape shows reason/score for every market).
struct FilteredMarketSignal {
  std::string market_ticker;
  std::string event_ticker;
  std::string series_ticker;
  PriceE4 yes_bid = 0;
  PriceE4 yes_ask = 0;
  PriceE4 spread = -1;             // -1 => undefined (one-sided book)
  CountFp yes_bid_size = 0;
  CountFp yes_ask_size = 0;
  CountFp last_trade_size = 0;
  CountFp volume = 0;
  CountFp open_interest = 0;
  std::int64_t freshness_ms = 0;
  std::string reason;              // "pass" or the first failing rule
  int score = 0;                   // 0..100, deterministic
};

struct FilterVerdict {
  bool passed = false;
  FilteredMarketSignal signal;
};

// Stable rule identifiers (also the `reason` strings). Checked in this order;
// `reason` is the FIRST failing rule so the dashboard can explain a rejection.
inline constexpr const char* kReasonPass = "pass";
inline constexpr const char* kReasonOneSided = "one_sided";
inline constexpr const char* kReasonStatus = "status_mismatch";
inline constexpr const char* kReasonCategory = "category_mismatch";
inline constexpr const char* kReasonSeries = "series_mismatch";
inline constexpr const char* kReasonStale = "stale";
inline constexpr const char* kReasonSpread = "spread_too_wide";
inline constexpr const char* kReasonPrice = "price_out_of_range";
inline constexpr const char* kReasonVolume = "volume_too_low";
inline constexpr const char* kReasonOpenInterest = "open_interest_too_low";
inline constexpr const char* kReasonBidSize = "bid_size_too_low";
inline constexpr const char* kReasonAskSize = "ask_size_too_low";

namespace detail {

// Deterministic integer score in [0, 100]: reward tight spread, volume, and
// open interest; penalize staleness. All integer arithmetic (no float).
inline int score_state(const MarketState& s, bool two_sided, PriceE4 spread) {
  int score = 0;
  if (two_sided && spread >= 0)
    score += std::max(0, 50 - static_cast<int>(spread / 20));     // 1c(=100)->45, 10c->0
  const long long vol_contracts = s.volume / 100;                 // CountFp -> contracts
  score += static_cast<int>(std::min<long long>(30, vol_contracts / 50));
  const long long oi_contracts = s.open_interest / 100;
  score += static_cast<int>(std::min<long long>(20, oi_contracts / 100));
  score -= static_cast<int>(std::min<long long>(20, s.freshness_ms / 100));
  return std::clamp(score, 0, 100);
}

}  // namespace detail

// Pure evaluation. Populates the signal (fields + spread + score + reason) and
// returns whether the market passes every set control.
inline FilterVerdict evaluate(const MarketState& s, const FilterControls& c) {
  FilterVerdict v;
  FilteredMarketSignal& sig = v.signal;
  sig.market_ticker = s.market_ticker;
  sig.event_ticker = s.event_ticker;
  sig.series_ticker = s.series_ticker;
  sig.yes_bid = s.yes_bid;
  sig.yes_ask = s.yes_ask;
  sig.yes_bid_size = s.yes_bid_size;
  sig.yes_ask_size = s.yes_ask_size;
  sig.last_trade_size = s.last_trade_size;
  sig.volume = s.volume;
  sig.open_interest = s.open_interest;
  sig.freshness_ms = s.freshness_ms;

  const bool has_bid = s.yes_bid > 0;
  const bool has_ask = s.yes_ask > 0;
  const bool two_sided = has_bid && has_ask;
  sig.spread = two_sided ? static_cast<PriceE4>(s.yes_ask - s.yes_bid) : -1;
  sig.score = detail::score_state(s, two_sided, sig.spread);

  auto reject = [&](const char* reason) {
    sig.reason = reason;
    v.passed = false;
    return v;
  };

  if (c.require_two_sided && !two_sided) return reject(kReasonOneSided);
  if (!c.status.empty() && s.status != c.status) return reject(kReasonStatus);
  if (!c.category.empty() && s.category != c.category) return reject(kReasonCategory);
  if (!c.series.empty() && s.series_ticker != c.series) return reject(kReasonSeries);
  if (c.max_stale_ms > 0 && s.freshness_ms > c.max_stale_ms) return reject(kReasonStale);
  // Spread only checked when the book is two-sided (otherwise undefined).
  if (c.max_spread >= 0 && two_sided && sig.spread > c.max_spread)
    return reject(kReasonSpread);
  // Price range on the reference price: yes_bid if present, else yes_ask.
  const PriceE4 ref = has_bid ? s.yes_bid : s.yes_ask;
  if (ref > 0 && (ref < c.price_min || ref > c.price_max)) return reject(kReasonPrice);
  if (c.min_volume >= 0 && s.volume < c.min_volume) return reject(kReasonVolume);
  if (c.min_open_interest >= 0 && s.open_interest < c.min_open_interest)
    return reject(kReasonOpenInterest);
  if (c.min_bid_size >= 0 && s.yes_bid_size < c.min_bid_size) return reject(kReasonBidSize);
  if (c.min_ask_size >= 0 && s.yes_ask_size < c.min_ask_size) return reject(kReasonAskSize);

  sig.reason = kReasonPass;
  v.passed = true;
  return v;
}

// Compact NDJSON line for one signal (dashboard tape / telemetry). Pure string
// building; prices/counts go through the fixed-point formatters. Tickers are
// exchange-charset (uppercase, digits, -, _, .) so no JSON escaping is needed.
inline std::string to_ndjson(const FilteredMarketSignal& s) {
  std::string j;
  j.reserve(320);
  j += "{\"type\":\"filtered_market\",\"market_ticker\":\"";
  j += s.market_ticker;
  j += "\",\"event_ticker\":\"";
  j += s.event_ticker;
  j += "\",\"series_ticker\":\"";
  j += s.series_ticker;
  j += "\",\"yes_bid\":\"" + format_price_e4(s.yes_bid);
  j += "\",\"yes_ask\":\"" + format_price_e4(s.yes_ask);
  j += "\",\"spread\":" + (s.spread >= 0 ? "\"" + format_price_e4(s.spread) + "\"" : std::string("null"));
  j += ",\"yes_bid_size\":\"" + format_count_fp(s.yes_bid_size);
  j += "\",\"yes_ask_size\":\"" + format_count_fp(s.yes_ask_size);
  j += "\",\"last_trade_size\":\"" + format_count_fp(s.last_trade_size);
  j += "\",\"volume\":\"" + format_count_fp(s.volume);
  j += "\",\"open_interest\":\"" + format_count_fp(s.open_interest);
  j += "\",\"freshness_ms\":" + std::to_string(s.freshness_ms);
  j += ",\"reason\":\"" + s.reason;
  j += "\",\"score\":" + std::to_string(s.score);
  j += "}";
  return j;
}

}  // namespace trading
