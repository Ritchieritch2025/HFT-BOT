#pragma once
//
// Fixed-point price and count types for the source-agnostic trading bus.
// No floating point anywhere — parsing is pure integer digit accumulation, so
// there is no rounding drift between the wire string and the stored value.
//
// PriceE4 is a NORMALIZED PROBABILITY-SPACE price: value x 10^4, valid only in
// [0, 10000] (i.e. $0.0000 .. $1.0000). This is a bus-wide invariant, not a
// Kalshi quirk — every source gateway (Kalshi dollar-strings/cents, and later
// sports American/decimal odds -> implied probability) converts its native
// quoting into probability space INSIDE the gateway before an event reaches
// the bus. PriceE4 is a *price* type only: do NOT reuse it for fees, notionals,
// balances, or P&L — those get their own type when needed.
//
// CountFp is contracts x 10^2 (2-decimal fixed point). Two distinct parsers:
//   parse_count_fp  — absolute quantity, must be >= 0
//   parse_delta_fp  — signed delta, may be negative

#include <cstdint>
#include <cstdio>
#include <optional>
#include <string>
#include <string_view>

namespace trading {

using PriceE4 = std::int32_t;   // dollars x 10^4, valid [0, 10000]
using CountFp = std::int64_t;   // contracts x 10^2

inline constexpr PriceE4 kPriceMin = 0;
inline constexpr PriceE4 kPriceMax = 10000;  // == $1.0000

namespace detail {

// Parse an unsigned decimal string with AT MOST `max_frac` fractional digits,
// returning the value scaled by 10^max_frac. Rejects (nullopt):
//   - empty / no digits
//   - any non-digit outside a single '.'
//   - MORE than max_frac fractional digits (excess precision — never truncated)
//   - trailing garbage
inline std::optional<std::int64_t> parse_scaled_unsigned(std::string_view s,
                                                         int max_frac) {
  if (s.empty() || s.size() > 24) return std::nullopt;
  std::size_t i = 0;
  std::int64_t intp = 0;
  bool any = false;
  for (; i < s.size() && s[i] >= '0' && s[i] <= '9'; ++i) {
    intp = intp * 10 + (s[i] - '0');
    any = true;
    if (intp > 1'000'000'000'000LL) return std::nullopt;  // absurd magnitude
  }
  std::int64_t frac = 0;
  int frac_digits = 0;
  if (i < s.size() && s[i] == '.') {
    ++i;
    for (; i < s.size() && s[i] >= '0' && s[i] <= '9'; ++i) {
      if (frac_digits >= max_frac) return std::nullopt;  // excess precision
      frac = frac * 10 + (s[i] - '0');
      ++frac_digits;
      any = true;
    }
  }
  if (i != s.size() || !any) return std::nullopt;  // trailing junk or empty

  std::int64_t scale = 1;
  for (int k = 0; k < max_frac; ++k) scale *= 10;
  std::int64_t frac_scale = 1;
  for (int k = 0; k < max_frac - frac_digits; ++k) frac_scale *= 10;
  return intp * scale + frac * frac_scale;
}

}  // namespace detail

// "0.0800" -> 800, "0.960" -> 9600, "1" -> 10000, "1.0000" -> 10000.
// Rejects negatives, >4 fractional digits ("1.00001"), and out-of-range
// (">1.0000", e.g. "1.0001").
inline std::optional<PriceE4> parse_price_e4(std::string_view s) {
  if (!s.empty() && (s.front() == '-' || s.front() == '+')) return std::nullopt;
  auto v = detail::parse_scaled_unsigned(s, 4);
  if (!v) return std::nullopt;
  if (*v < kPriceMin || *v > kPriceMax) return std::nullopt;
  return static_cast<PriceE4>(*v);
}

// Absolute count, must be >= 0. "300.00" -> 30000, "5" -> 500.
inline std::optional<CountFp> parse_count_fp(std::string_view s) {
  if (!s.empty() && (s.front() == '-' || s.front() == '+')) return std::nullopt;
  auto v = detail::parse_scaled_unsigned(s, 2);
  if (!v) return std::nullopt;
  return static_cast<CountFp>(*v);
}

// Signed delta count. "-54.00" -> -5400, "12.5" -> 1250, "300.00" -> 30000.
inline std::optional<CountFp> parse_delta_fp(std::string_view s) {
  bool neg = false;
  if (!s.empty() && (s.front() == '-' || s.front() == '+')) {
    neg = s.front() == '-';
    s.remove_prefix(1);
  }
  auto v = detail::parse_scaled_unsigned(s, 2);
  if (!v) return std::nullopt;
  return static_cast<CountFp>(neg ? -*v : *v);
}

// Canonical 4-decimal dollar string. 800 -> "0.0800", 10000 -> "1.0000".
inline std::string format_price_e4(PriceE4 v) {
  char buf[24];
  std::snprintf(buf, sizeof(buf), "%d.%04d", v / 10000, v % 10000);
  return buf;
}

// Canonical 2-decimal count string. 30000 -> "300.00", -5400 -> "-54.00".
inline std::string format_count_fp(CountFp v) {
  const bool neg = v < 0;
  const std::int64_t a = neg ? -v : v;
  char buf[32];
  std::snprintf(buf, sizeof(buf), "%s%lld.%02lld", neg ? "-" : "",
                static_cast<long long>(a / 100), static_cast<long long>(a % 100));
  return buf;
}

// Integer-cents bridge for endpoints that quote whole cents (some v2 orderbook
// responses historically do). 1 cent == $0.01 == 100 in PriceE4 units.
inline constexpr PriceE4 cents_to_e4(int cents) {
  return static_cast<PriceE4>(cents) * 100;
}
// Round-to-nearest-cent (a deci-cent E4 value is not cent-aligned).
inline constexpr int e4_to_cents(PriceE4 v) { return (v + 50) / 100; }

}  // namespace trading
