// Phase 4 fast-filter tests (MARKET_DATA_PIPELINE_FILTER_DASHBOARD). Pure/offline:
// deterministic evaluate() over fixture market states — no network, no DuckDB, no
// I/O. Verifies each control rejects for the right reason, defaults pass, spread
// and score are computed correctly, and the reason precedence order is stable.
//
// usage: test_market_filter

#include "trading/market_filter.hpp"

#include <iostream>
#include <string>

using namespace trading;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

// A healthy two-sided market: 0.40/0.42 (2c spread), sizes 300/250, 1500
// contracts volume, 2000 OI, 100ms fresh, active. Prices are PriceE4 (x1e4),
// counts CountFp (contracts x100).
MarketState healthy() {
  MarketState s;
  s.market_ticker = "KXTEST-A";
  s.event_ticker = "KXTEST";
  s.series_ticker = "KXSER";
  s.category = "crypto";
  s.status = "active";
  s.yes_bid = 4000;   // $0.40
  s.yes_ask = 4200;   // $0.42
  s.yes_bid_size = 30000;   // 300 contracts
  s.yes_ask_size = 25000;   // 250 contracts
  s.last_trade_size = 500;  // 5 contracts
  s.volume = 150000;        // 1500 contracts
  s.open_interest = 200000; // 2000 contracts
  s.freshness_ms = 100;
  return s;
}

}  // namespace

int main() {
  // 1. Defaults pass a healthy two-sided market; spread computed.
  {
    auto v = evaluate(healthy(), FilterControls{});
    check(v.passed, "healthy market passes default controls");
    check(v.signal.reason == kReasonPass, "healthy reason == pass");
    check(v.signal.spread == 200, "spread = ask - bid = 0.02 (200 E4)");
    check(v.signal.market_ticker == "KXTEST-A", "signal carries market_ticker");
  }

  // 2. One-sided book rejected when two-sided required; spread undefined.
  {
    MarketState s = healthy();
    s.yes_ask = 0;  // no ask
    auto v = evaluate(s, FilterControls{});
    check(!v.passed && v.signal.reason == kReasonOneSided, "one-sided book rejected");
    check(v.signal.spread == -1, "one-sided spread is undefined (-1)");
    // ...but allowed when require_two_sided=false (and no other control trips).
    FilterControls c;
    c.require_two_sided = false;
    auto v2 = evaluate(s, c);
    check(v2.passed, "one-sided book passes when two-sided not required");
  }

  // 3. Spread cap.
  {
    FilterControls c;
    c.max_spread = 100;  // 1c cap; healthy has a 2c spread
    auto v = evaluate(healthy(), c);
    check(!v.passed && v.signal.reason == kReasonSpread, "wide spread rejected");
    c.max_spread = 200;  // exactly the spread -> allowed (<=)
    check(evaluate(healthy(), c).passed, "spread == cap passes");
  }

  // 4. Volume / open-interest / size minimums.
  {
    FilterControls c;
    c.min_volume = 150001;  // one more than healthy's 1500 contracts
    check(evaluate(healthy(), c).signal.reason == kReasonVolume, "volume floor rejects");

    c = FilterControls{};
    c.min_open_interest = 200001;
    check(evaluate(healthy(), c).signal.reason == kReasonOpenInterest, "OI floor rejects");

    c = FilterControls{};
    c.min_bid_size = 30001;
    check(evaluate(healthy(), c).signal.reason == kReasonBidSize, "bid-size floor rejects");

    c = FilterControls{};
    c.min_ask_size = 25001;
    check(evaluate(healthy(), c).signal.reason == kReasonAskSize, "ask-size floor rejects");
  }

  // 5. Freshness (staleness) cap.
  {
    MarketState s = healthy();
    s.freshness_ms = 5000;
    FilterControls c;
    c.max_stale_ms = 1000;
    check(evaluate(s, c).signal.reason == kReasonStale, "stale market rejected");
    c.max_stale_ms = 5000;  // exactly at limit -> allowed
    check(evaluate(s, c).passed, "freshness == cap passes");
  }

  // 6. Price range on the reference (yes_bid) price.
  {
    FilterControls c;
    c.price_min = 4100;  // healthy bid is 4000 -> below range
    check(evaluate(healthy(), c).signal.reason == kReasonPrice, "bid below price_min rejected");
    c = FilterControls{};
    c.price_max = 3900;  // healthy bid 4000 -> above range
    check(evaluate(healthy(), c).signal.reason == kReasonPrice, "bid above price_max rejected");
  }

  // 7. Category / series / status selectors.
  {
    FilterControls c;
    c.category = "sports";  // healthy is crypto
    check(evaluate(healthy(), c).signal.reason == kReasonCategory, "category mismatch rejected");
    c = FilterControls{};
    c.series = "OTHER";
    check(evaluate(healthy(), c).signal.reason == kReasonSeries, "series mismatch rejected");
    c = FilterControls{};
    c.status = "closed";
    check(evaluate(healthy(), c).signal.reason == kReasonStatus, "status mismatch rejected");
    // Matching selectors pass.
    c = FilterControls{};
    c.category = "crypto"; c.series = "KXSER"; c.status = "active";
    check(evaluate(healthy(), c).passed, "matching selectors pass");
  }

  // 8. Reason precedence is stable: one_sided is reported before spread/volume.
  {
    MarketState s = healthy();
    s.yes_ask = 0;          // one-sided
    s.volume = 0;           // also below a volume floor
    FilterControls c;
    c.min_volume = 100;
    c.require_two_sided = true;
    check(evaluate(s, c).signal.reason == kReasonOneSided,
          "one_sided takes precedence over later rules");
  }

  // 9. Score is deterministic and ordered: tighter+deeper markets score higher.
  {
    const int healthy_score = evaluate(healthy(), FilterControls{}).signal.score;
    MarketState wide = healthy();
    wide.yes_ask = 5000;    // 10c spread -> tightness component drops to 0
    const int wide_score = evaluate(wide, FilterControls{}).signal.score;
    check(healthy_score > wide_score, "tighter spread scores higher");

    MarketState thin = healthy();
    thin.volume = 0; thin.open_interest = 0;
    const int thin_score = evaluate(thin, FilterControls{}).signal.score;
    check(healthy_score > thin_score, "more volume/OI scores higher");
    check(healthy_score >= 0 && healthy_score <= 100, "score within [0,100]");
  }

  // 10. NDJSON serialization is well-formed and carries key fields.
  {
    auto v = evaluate(healthy(), FilterControls{});
    const std::string j = to_ndjson(v.signal);
    check(j.front() == '{' && j.back() == '}', "ndjson is a JSON object");
    check(j.find("\"market_ticker\":\"KXTEST-A\"") != std::string::npos, "ndjson has ticker");
    check(j.find("\"spread\":\"0.0200\"") != std::string::npos, "ndjson spread formatted");
    check(j.find("\"reason\":\"pass\"") != std::string::npos, "ndjson has reason");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
