// RequestSpec: path normalization, prefix strip/round-trip, F6 Read/Write
// classification, and cost resolution. Pure string logic (no network).

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

  // --- normalization: strip query, ensure full wire path, idempotent ---
  check(normalize_endpoint_path("/markets") == "/trade-api/v2/markets", "prefix-less path gets wire prefix");
  check(normalize_endpoint_path("/trade-api/v2/markets") == "/trade-api/v2/markets", "full wire path unchanged");
  check(normalize_endpoint_path("/markets?limit=100&cursor=x") == "/trade-api/v2/markets", "query stripped");
  check(normalize_endpoint_path("markets") == "/trade-api/v2/markets", "missing leading slash handled");
  check(normalize_endpoint_path(normalize_endpoint_path("/markets")) == "/trade-api/v2/markets", "idempotent");

  // --- prefix strip round-trips with the signing client's re-add ---
  check(strip_wire_prefix("/trade-api/v2/portfolio/events/orders") == "/portfolio/events/orders",
        "strip_wire_prefix removes the prefix");
  check(strip_wire_prefix("/portfolio/events/orders") == "/portfolio/events/orders",
        "strip_wire_prefix leaves prefix-less path alone");
  // normalize -> strip returns the prefix-less form the client expects.
  check(strip_wire_prefix(normalize_endpoint_path("/markets?x=1")) == "/markets",
        "normalize then strip == original prefix-less path");

  // --- classification (F6) ---
  MutationKind mk;
  check(classify(Method::Get, "/trade-api/v2/markets", &mk) == BucketKind::Read && mk == MutationKind::None,
        "GET markets => Read/None");
  check(classify(Method::Get, "/trade-api/v2/account/limits") == BucketKind::Read, "GET account/limits => Read");

  // Current create path is /portfolio/events/orders; legacy /portfolio/orders too.
  check(classify(Method::Post, "/trade-api/v2/portfolio/events/orders", &mk) == BucketKind::Write &&
            mk == MutationKind::CreateOrder,
        "POST events/orders => Write/CreateOrder");
  check(classify(Method::Post, "/trade-api/v2/portfolio/orders", &mk) == BucketKind::Write &&
            mk == MutationKind::CreateOrder,
        "POST legacy portfolio/orders => Write/CreateOrder");
  check(classify(Method::Delete, "/trade-api/v2/portfolio/events/orders/abc-123", &mk) == BucketKind::Write &&
            mk == MutationKind::CancelOrder,
        "DELETE order id => Write/CancelOrder");
  check(classify(Method::Post, "/trade-api/v2/portfolio/events/orders/abc/amend", &mk) == BucketKind::Write &&
            mk == MutationKind::AmendOrder,
        "POST .../amend => Write/AmendOrder");
  check(classify(Method::Post, "/trade-api/v2/portfolio/orders/batched", &mk) == BucketKind::Write &&
            mk == MutationKind::OrderGroup,
        "POST orders/batched => Write/OrderGroup");
  check(classify(Method::Delete, "/trade-api/v2/portfolio/order_groups/g1", &mk) == BucketKind::Write &&
            mk == MutationKind::OrderGroup,
        "order_groups => Write/OrderGroup");
  check(classify(Method::Post, "/trade-api/v2/portfolio/rfqs", &mk) == BucketKind::Write &&
            mk == MutationKind::Quote,
        "rfqs => Write/Quote");
  // A GET on an orders path (list/lookup) is a READ, not a write.
  check(classify(Method::Get, "/trade-api/v2/portfolio/events/orders", &mk) == BucketKind::Read &&
            mk == MutationKind::None,
        "GET orders (list) => Read/None");
  // Block-trade accept is a Write ASSUMPTION only when mutating.
  check(classify(Method::Post, "/trade-api/v2/portfolio/block_trades/accept") == BucketKind::Write,
        "POST block_trades/accept => Write (assumption)");
  check(classify(Method::Get, "/trade-api/v2/portfolio/block_trades") == BucketKind::Read,
        "GET block_trades => Read");

  // --- make_request_spec: full spec + cost resolution + idempotency flag ---
  EndpointCostTable costs;
  costs.default_cost = 10;
  costs.overrides.push_back({Method::Post, "/trade-api/v2/portfolio/events/orders", 10});
  costs.overrides.push_back({Method::Get, "/trade-api/v2/markets", 1});
  {
    RequestSpec s = make_request_spec(Method::Post, "/portfolio/events/orders", costs);
    check(s.bucket == BucketKind::Write && s.mutation == MutationKind::CreateOrder,
          "spec: create order classified Write");
    check(s.normalized_path == "/trade-api/v2/portfolio/events/orders", "spec: normalized to full wire path");
    check(s.cost_tokens == 10, "spec: create order cost resolved");
    check(s.idempotency_required, "spec: create order requires idempotency");
  }
  {
    RequestSpec s = make_request_spec(Method::Get, "/markets?limit=5", costs);
    check(s.bucket == BucketKind::Read && s.cost_tokens == 1 && !s.idempotency_required,
          "spec: GET markets Read, cost 1, no idempotency");
  }
  {
    RequestSpec s = make_request_spec(Method::Get, "/exchange/status", costs);
    check(s.cost_tokens == 10, "spec: unlisted path => default_cost");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
