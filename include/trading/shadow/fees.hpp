#pragma once
//
// Fail-closed maker/taker fee schedule (WO-E doctrine, deep03 C-01/C-02):
// a market whose fee schedule is UNKNOWN is economically INELIGIBLE — it is
// never assumed free. No fee constant lives in code; entries come from a
// ratified config file (config/fees.json) with provenance, or from explicit
// injection in tests. Fee ratification is an operator decision gate.
//
// Formula per entry (Kalshi shape, integer math, round UP to the cent):
//   fee_cents(count, p) = ceil( rate_e4 * count * p * (100 - p) / 1e6 )
// where rate_e4 = dollar-rate x 1e4 (0.0175 -> 175), p = yes-price in cents.
// Accounting thread only — never on the hot path.

#include <cstdint>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>

namespace trading::shadow {

struct FeeEntry {
  std::string series_prefix;  // longest-prefix match against the ticker
  std::int64_t maker_rate_e4 = -1;  // -1 = unknown (fail closed)
  std::int64_t taker_rate_e4 = -1;
  std::string source;      // provenance: URL/doc that ratified this entry
  std::string fetched_at;  // provenance: when
};

class FeeSchedule {
 public:
  // Empty schedule: every lookup returns nullopt (everything ineligible).
  FeeSchedule() = default;

  void add(FeeEntry e) { entries_.push_back(std::move(e)); }
  std::size_t size() const { return entries_.size(); }

  // Longest-prefix match; nullopt = UNKNOWN = market ineligible (fail closed).
  std::optional<std::int64_t> maker_fee_cents(std::string_view ticker,
                                              std::int64_t count,
                                              std::int64_t price_cents) const {
    const FeeEntry* best = match(ticker);
    if (!best || best->maker_rate_e4 < 0) return std::nullopt;
    return fee_cents(best->maker_rate_e4, count, price_cents);
  }

  bool known(std::string_view ticker) const {
    const FeeEntry* best = match(ticker);
    return best && best->maker_rate_e4 >= 0;
  }

  static std::int64_t fee_cents(std::int64_t rate_e4, std::int64_t count,
                                std::int64_t p) {
    // ceil(rate_e4 * count * p * (100-p) / 1e6), all inputs validated upstream
    const std::int64_t num = rate_e4 * count * p * (100 - p);
    return (num + 999'999) / 1'000'000;
  }

  // Minimal loader for config/fees.json — one entry per line variant kept
  // deliberately simple: series_prefix,maker_rate_e4,taker_rate_e4,source,fetched_at
  // (CSV; '#' comments). JSON parsing on the accounting path buys nothing here.
  static FeeSchedule load_csv(const std::string& path) {
    FeeSchedule s;
    std::ifstream in(path);
    std::string line;
    while (std::getline(in, line)) {
      if (line.empty() || line[0] == '#') continue;
      std::stringstream ss(line);
      FeeEntry e;
      std::string maker, taker;
      if (!std::getline(ss, e.series_prefix, ',')) continue;
      if (!std::getline(ss, maker, ',')) continue;
      if (!std::getline(ss, taker, ',')) continue;
      std::getline(ss, e.source, ',');
      std::getline(ss, e.fetched_at, ',');
      try {
        e.maker_rate_e4 = std::stoll(maker);
        e.taker_rate_e4 = std::stoll(taker);
      } catch (...) {
        continue;  // malformed row = unknown, never a guess
      }
      s.add(std::move(e));
    }
    return s;
  }

 private:
  const FeeEntry* match(std::string_view ticker) const {
    const FeeEntry* best = nullptr;
    std::size_t best_len = 0;
    for (const auto& e : entries_) {
      if (e.series_prefix.size() >= best_len &&
          ticker.substr(0, e.series_prefix.size()) == e.series_prefix) {
        best = &e;
        best_len = e.series_prefix.size();
      }
    }
    return best;
  }

  std::vector<FeeEntry> entries_;
};

}  // namespace trading::shadow
