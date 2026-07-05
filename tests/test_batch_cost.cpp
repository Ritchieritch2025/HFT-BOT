// Batch token-cost math (F8): batch endpoints bill per item; the whole batch
// must fit the bucket (no partial send). The behavioral no-partial-send / refuse
// path is exercised end-to-end in test_request_executor (cost_override); this
// pins the arithmetic + the per-item cost lookup.

#include "kalshi/limits.hpp"
#include "kalshi/request_spec.hpp"

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

  // Documented examples: 25 creates x 10 = 250; 25 cancels x 2 = 50.
  check(batch_total_cost(25, 10) == 250, "25 creates x 10 = 250");
  check(batch_total_cost(25, 2) == 50, "25 cancels x 2 = 50");
  check(batch_total_cost(100, 1) == 100, "100 reads x 1 = 100");
  check(batch_total_cost(1, 10) == 10, "single-item batch = per-item cost");
  // Degenerate inputs => 0 (caller rejects an empty/zero-cost batch).
  check(batch_total_cost(0, 10) == 0 && batch_total_cost(25, 0) == 0 &&
            batch_total_cost(-5, 10) == 0,
        "non-positive items/cost => 0");

  // Per-item cost comes from the cost table for the SINGLE-item endpoint.
  EndpointCostTable costs;
  costs.default_cost = 10;
  costs.overrides.push_back({Method::Post, "/trade-api/v2/portfolio/events/orders", 10});
  costs.overrides.push_back({Method::Delete, "/trade-api/v2/portfolio/events/orders/{id}", 2});
  costs.overrides.push_back({Method::Get, "/trade-api/v2/markets/{ticker}/orderbook", 1});

  const int create_cost = costs.cost_for(Method::Post, "/trade-api/v2/portfolio/events/orders");
  const int cancel_cost = costs.cost_for(Method::Delete, "/trade-api/v2/portfolio/events/orders/x");
  const int read_cost = costs.cost_for(Method::Get, "/trade-api/v2/markets/INXD/orderbook");
  check(batch_total_cost(25, create_cost) == 250, "batch create total from table = 250");
  check(batch_total_cost(25, cancel_cost) == 50, "batch cancel total from table = 50");
  check(batch_total_cost(30, read_cost) == 30, "batch read total (per-item ASSUMPTION) = 30");

  // Unlisted single endpoint => default_cost feeds the batch.
  const int unknown = costs.cost_for(Method::Get, "/trade-api/v2/markets/_/orderbook");
  check(unknown == 1, "template matches single-orderbook cost for the batch per-item lookup");

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
