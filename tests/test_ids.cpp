// Identity tests: EntityId determinism (stable across runs), Unknown handling,
// SourceId stringification, TraceId monotonicity. Pure C++, no deps.

#include "trading/ids.hpp"

#include <iostream>
#include <set>
#include <string>

using namespace trading;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
}  // namespace

int main() {
  // Deterministic: same (source,ticker) -> same id, every run. These are golden
  // FNV-1a values; if make_entity_id's algorithm changes, this test breaks on
  // purpose (stored normalized logs would otherwise become unreadable).
  const EntityId a = make_entity_id(SourceId::Kalshi, "KXBTC-25-T100");
  const EntityId a2 = make_entity_id(SourceId::Kalshi, "KXBTC-25-T100");
  check(a == a2, "same source+ticker -> identical id");
  check(a.v != 0, "valid id never 0");

  // Different ticker or source -> different id.
  check(make_entity_id(SourceId::Kalshi, "KXBTC-25-T101") != a,
        "different ticker -> different id");
  check(make_entity_id(SourceId::SportsOdds, "KXBTC-25-T100") != a,
        "different source, same ticker -> different id (source is mixed in)");

  // No accidental collisions across a spread of tickers.
  std::set<std::uint64_t> ids;
  int n = 0;
  for (int i = 0; i < 5000; ++i) {
    ids.insert(make_entity_id(SourceId::Kalshi, "MKT-" + std::to_string(i)).v);
    ++n;
  }
  check(static_cast<int>(ids.size()) == n, "5000 distinct tickers -> 5000 distinct ids");

  // Golden constants (recompute FNV-1a here independently to catch algorithm drift).
  auto fnv = [](SourceId s, std::string_view t) {
    std::uint64_t h = 1469598103934665603ULL;
    const std::uint64_t p = 1099511628211ULL;
    h ^= static_cast<std::uint8_t>(s); h *= p;
    for (unsigned char c : t) { h ^= c; h *= p; }
    return h == 0 ? p : h;
  };
  check(a.v == fnv(SourceId::Kalshi, "KXBTC-25-T100"),
        "EntityId matches independent FNV-1a reference");

  // SourceId stringification round-trips through all values.
  check(std::string(to_string(SourceId::Unknown)) == "Unknown", "Unknown->string");
  check(std::string(to_string(SourceId::Kalshi)) == "Kalshi", "Kalshi->string");
  check(std::string(to_string(SourceId::Custom)) == "Custom", "Custom->string");

  // TraceId strictly increasing.
  const TraceId t1 = next_trace_id();
  const TraceId t2 = next_trace_id();
  check(t2.v > t1.v, "trace ids strictly increase");

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
