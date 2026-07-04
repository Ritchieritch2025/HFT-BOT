// Fixed-point parse/format tests: strict range, no truncation, abs-vs-delta,
// round-trips, cents bridge. Pure C++, no deps.

#include "trading/fixedpoint.hpp"

#include <iostream>
#include <string>

using namespace trading;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
template <class T>
bool eq(const std::optional<T>& o, T v) { return o.has_value() && *o == v; }
}  // namespace

int main() {
  // --- parse_price_e4: valid ---
  check(eq(parse_price_e4("0.0800"), 800), "price 0.0800 -> 800");
  check(eq(parse_price_e4("0.960"), 9600), "price 0.960 -> 9600 (3dp)");
  check(eq(parse_price_e4("0.480"), 4800), "price 0.480 -> 4800");
  check(eq(parse_price_e4("1.0000"), 10000), "price 1.0000 -> 10000");
  check(eq(parse_price_e4("1"), 10000), "price 1 -> 10000");
  check(eq(parse_price_e4("0"), 0), "price 0 -> 0");
  check(eq(parse_price_e4("0.0001"), 1), "price 0.0001 -> 1 (deci-cent tick)");

  // --- parse_price_e4: rejects ---
  check(!parse_price_e4("1.0001").has_value(), "reject 1.0001 (>1.0000)");
  check(!parse_price_e4("1.00001").has_value(), "reject 1.00001 (excess precision)");
  check(!parse_price_e4("0.08000").has_value(), "reject 0.08000 (5 frac digits, no truncation)");
  check(!parse_price_e4("-0.5").has_value(), "reject negative price");
  check(!parse_price_e4("+0.5").has_value(), "reject leading +");
  check(!parse_price_e4("").has_value(), "reject empty");
  check(!parse_price_e4("0.5x").has_value(), "reject trailing junk");
  check(!parse_price_e4("abc").has_value(), "reject non-numeric");
  check(!parse_price_e4("2").has_value(), "reject 2 (>range)");

  // --- parse_count_fp: absolute, >=0 ---
  check(eq(parse_count_fp("300.00"), CountFp{30000}), "count 300.00 -> 30000");
  check(eq(parse_count_fp("5"), CountFp{500}), "count 5 -> 500");
  check(eq(parse_count_fp("0.01"), CountFp{1}), "count 0.01 -> 1");
  check(!parse_count_fp("-5.00").has_value(), "count rejects negative");
  check(!parse_count_fp("5.001").has_value(), "count rejects 3 frac digits");

  // --- parse_delta_fp: signed ---
  check(eq(parse_delta_fp("-54.00"), CountFp{-5400}), "delta -54.00 -> -5400");
  check(eq(parse_delta_fp("12.5"), CountFp{1250}), "delta 12.5 -> 1250");
  check(eq(parse_delta_fp("300.00"), CountFp{30000}), "delta 300.00 -> 30000");
  check(eq(parse_delta_fp("-0.01"), CountFp{-1}), "delta -0.01 -> -1");
  check(!parse_delta_fp("-5.999").has_value(), "delta rejects 3 frac digits");

  // --- formatters + round-trip ---
  check(format_price_e4(800) == "0.0800", "format 800 -> 0.0800");
  check(format_price_e4(10000) == "1.0000", "format 10000 -> 1.0000");
  check(format_price_e4(9600) == "0.9600", "format 9600 -> 0.9600");
  check(format_count_fp(30000) == "300.00", "format 30000 -> 300.00");
  check(format_count_fp(-5400) == "-54.00", "format -5400 -> -54.00");
  bool rt = true;
  for (PriceE4 p = 0; p <= 10000; p += 7)
    rt = rt && eq(parse_price_e4(format_price_e4(p)), p);
  check(rt, "price format->parse round-trip over full range");

  // --- cents bridge ---
  check(cents_to_e4(42) == 4200, "cents_to_e4(42) -> 4200");
  check(e4_to_cents(4200) == 42, "e4_to_cents(4200) -> 42");
  check(e4_to_cents(4250) == 43, "e4_to_cents(4250) rounds to 43");

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
