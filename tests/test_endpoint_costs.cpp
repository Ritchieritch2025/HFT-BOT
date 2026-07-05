// Fixture-driven tests for the /account/endpoint_costs parser + cost_for()
// matching (exact -> server template {seg} wildcard -> default_cost). No network.

#include "kalshi/limits.hpp"

#include <iostream>
#include <string>

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
}  // namespace

int main() {
  using namespace kalshi;

  const char* body = R"({
    "default_cost":10,
    "endpoint_costs":[
      {"method":"POST","path":"/trade-api/v2/portfolio/orders","cost":10},
      {"method":"DELETE","path":"/trade-api/v2/portfolio/orders/{order_id}","cost":2},
      {"method":"GET","path":"/trade-api/v2/markets","cost":1},
      {"method":"GET","path":"/trade-api/v2/markets/{ticker}/orderbook","cost":1}
    ]
  })";
  {
    auto t = parse_endpoint_costs(body);
    check(t.from_server, "from_server true on real parse");
    check(t.default_cost == 10, "default_cost 10");
    check(t.overrides.size() == 4, "four overrides parsed");
    // Exact match.
    check(t.cost_for(Method::Post, "/trade-api/v2/portfolio/orders") == 10, "exact POST orders => 10");
    check(t.cost_for(Method::Get, "/trade-api/v2/markets") == 1, "exact GET markets => 1");
    // Template match: one {seg} wildcard matches exactly one segment.
    check(t.cost_for(Method::Delete, "/trade-api/v2/portfolio/orders/abc-123") == 2,
          "template DELETE orders/{order_id} => 2");
    check(t.cost_for(Method::Get, "/trade-api/v2/markets/INXD-24/orderbook") == 1,
          "template GET markets/{ticker}/orderbook => 1");
    // Method must match: DELETE on a POST-only path falls through to default.
    check(t.cost_for(Method::Delete, "/trade-api/v2/portfolio/orders") == 10,
          "wrong method => default_cost");
    // Unlisted path => default.
    check(t.cost_for(Method::Get, "/trade-api/v2/exchange/status") == 10, "unlisted => default_cost");
    // Wrong segment count must not match the template.
    check(t.cost_for(Method::Delete, "/trade-api/v2/portfolio/orders/a/b") == 10,
          "extra segment doesn't match single-wildcard template");
  }

  // Absent default_cost => 10; empty/absent list => no overrides, all default.
  {
    auto t = parse_endpoint_costs(R"({"endpoint_costs":[]})");
    check(t.default_cost == 10 && t.overrides.empty(), "absent default_cost => 10, empty list");
    check(t.cost_for(Method::Post, "/anything") == 10, "everything default when no overrides");
  }
  {
    auto t = parse_endpoint_costs(R"({"default_cost":5})");
    check(t.default_cost == 5 && t.overrides.empty(), "default_cost honored, no list");
    check(t.cost_for(Method::Get, "/x") == 5, "custom default applied");
  }

  // Kalshi's REAL cost-table path syntax uses ":param" and "*glob", not "{seg}"
  // (verified live 2026-07-05). The matcher must handle all three.
  {
    const char* real = R"({"default_cost":10,"endpoint_costs":[
      {"method":"DELETE","path":"/trade-api/v2/portfolio/events/orders/:order_id","cost":2},
      {"method":"DELETE","path":"/trade-api/v2/portfolio/events/orders/batched","cost":2},
      {"method":"GET","path":"/trade-api/v2/portfolio/orders/:order_id","cost":2},
      {"method":"GET","path":"/trade-api/v2/cfbenchmarks/*endpoint","cost":50}
    ]})";
    auto t = parse_endpoint_costs(real);
    // ":order_id" matches exactly one concrete segment.
    check(t.cost_for(Method::Delete, "/trade-api/v2/portfolio/events/orders/ABC-123") == 2,
          ":order_id param matches a real order id (cancel = 2, not default 10)");
    check(t.cost_for(Method::Get, "/trade-api/v2/portfolio/orders/xyz") == 2,
          ":order_id matches on GET order lookup");
    // The literal "batched" segment must NOT be swallowed by the :order_id entry.
    check(t.cost_for(Method::Delete, "/trade-api/v2/portfolio/events/orders/batched") == 2,
          "flat batched cancel = 2 (literal beats param, both are 2 here)");
    // "*endpoint" glob matches one OR MORE trailing segments.
    check(t.cost_for(Method::Get, "/trade-api/v2/cfbenchmarks/a") == 50 &&
              t.cost_for(Method::Get, "/trade-api/v2/cfbenchmarks/a/b/c") == 50,
          "*glob matches the remaining path");
    // Wrong segment count / no trailing segment => no match => default.
    check(t.cost_for(Method::Delete, "/trade-api/v2/portfolio/events/orders") == 10,
          ":order_id template does not match a shorter path");
    check(t.cost_for(Method::Get, "/trade-api/v2/cfbenchmarks") == 10,
          "*glob needs >=1 trailing segment");
  }

  // A malformed entry (missing cost) is skipped; the rest still parse.
  {
    auto t = parse_endpoint_costs(R"({"default_cost":10,"endpoint_costs":[{"method":"POST","path":"/p"},{"method":"GET","path":"/q","cost":3}]})");
    check(t.overrides.size() == 1 && t.cost_for(Method::Get, "/q") == 3, "malformed entry skipped, valid kept");
  }

  // Malformed / non-object JSON => plain default table (default_cost 10, from_server=false).
  {
    auto t = parse_endpoint_costs("[]");
    check(t.default_cost == 10 && t.overrides.empty() && !t.from_server, "array top-level => default table");
    auto g = parse_endpoint_costs("garbage");
    check(g.default_cost == 10 && !g.from_server, "garbage => default table");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
