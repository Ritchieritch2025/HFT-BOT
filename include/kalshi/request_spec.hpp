#pragma once
//
// RequestSpec: the classified description of one outbound REST call
// (docs/PLAN_TOKEN_RULES.md T2, fact F6). Every authenticated request the
// RequestExecutor (T5) sends is first turned into a RequestSpec: query stripped,
// path normalized to a FULL wire path, Read/Write bucket + mutation kind + token
// cost resolved.
//
// PREFIX CONVENTION (resolves the double-prefix hazard, synthesis §5.1):
//   * The Kalshi signing client (sign_request/request) prepends cfg_.api_prefix
//     (= "/trade-api/v2") to whatever path it is handed, and signs the prefixed
//     path. Callers therefore historically pass PREFIX-LESS paths ("/markets").
//   * Cost tables + Kalshi's docs use FULL wire paths ("/trade-api/v2/markets").
//   * So RequestSpec normalizes to the FULL wire path (for classification + cost
//     lookup), and the executor calls strip_wire_prefix() before handing the
//     path back to the signing client — which re-adds it. Net: signed exactly
//     once, classified/costed against the documented path.

#include "kalshi/client.hpp"  // Method
#include "kalshi/limits.hpp"  // EndpointCostTable

#include <cstdint>
#include <string>
#include <string_view>

namespace kalshi {

// The wire prefix every Kalshi trade-api v2 path carries.
inline constexpr std::string_view kWirePrefix = "/trade-api/v2";

enum class BucketKind : std::uint8_t { Read, Write };
const char* to_string(BucketKind);

// What an order-ish request mutates — drives idempotency + the T4 retry matrix.
enum class MutationKind : std::uint8_t {
  None,         // a read
  CreateOrder,  // order placement (idempotency required)
  CancelOrder,  // order cancel
  AmendOrder,   // order amend/decrease
  OrderGroup,   // batched orders / order-group ops
  Quote,        // RFQ quote flow
};
const char* to_string(MutationKind);

struct RequestSpec {
  Method method = Method::Get;
  std::string raw_path;         // exactly as the caller passed it (may have query)
  std::string normalized_path;  // query stripped, FULL wire path (with prefix)
  BucketKind bucket = BucketKind::Read;
  int cost_tokens = 0;
  MutationKind mutation = MutationKind::None;
  bool idempotency_required = false;  // true for CreateOrder (reconcile-before-resend)
};

// Strip the query string and normalize to a full wire path: if `raw_path` does
// not already start with kWirePrefix, prepend it. Idempotent.
std::string normalize_endpoint_path(std::string_view raw_path);

// Remove kWirePrefix from a full wire path (executor -> signing client, which
// re-adds cfg_.api_prefix). If the prefix is absent, returns the path unchanged.
std::string strip_wire_prefix(std::string_view wire_path);

// Classify a (method, normalized full-wire path) into Read/Write + mutation kind
// per F6: Write = order placement / amend / cancel / order groups / RFQ quotes;
// Read = GET and anything not explicitly a write. Writes to `out_mutation` when
// non-null.
BucketKind classify(Method method, std::string_view normalized_path,
                    MutationKind* out_mutation = nullptr);

// Build a complete RequestSpec, resolving the token cost from the table
// (cost_for on the normalized full wire path).
RequestSpec make_request_spec(Method method, std::string_view raw_path,
                              const EndpointCostTable& costs);

}  // namespace kalshi
