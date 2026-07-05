#include "kalshi/request_spec.hpp"

#include <array>
#include <vector>

namespace kalshi {

const char* to_string(BucketKind b) {
  switch (b) {
    case BucketKind::Read: return "read";
    case BucketKind::Write: return "write";
  }
  return "read";
}

const char* to_string(MutationKind m) {
  switch (m) {
    case MutationKind::None: return "none";
    case MutationKind::CreateOrder: return "create_order";
    case MutationKind::CancelOrder: return "cancel_order";
    case MutationKind::AmendOrder: return "amend_order";
    case MutationKind::OrderGroup: return "order_group";
    case MutationKind::Quote: return "quote";
  }
  return "none";
}

namespace {

// Split a path into '/'-delimited segments (query already stripped).
std::vector<std::string_view> segs(std::string_view path) {
  std::vector<std::string_view> out;
  std::size_t i = 0;
  while (i < path.size()) {
    if (path[i] == '/') { ++i; continue; }
    std::size_t j = path.find('/', i);
    if (j == std::string_view::npos) j = path.size();
    out.push_back(path.substr(i, j - i));
    i = j;
  }
  return out;
}

bool has_seg(const std::vector<std::string_view>& s, std::string_view want) {
  for (auto v : s) if (v == want) return true;
  return false;
}

}  // namespace

std::string normalize_endpoint_path(std::string_view raw_path) {
  std::string_view p = raw_path.substr(0, raw_path.find('?'));  // strip query
  if (p.rfind(kWirePrefix, 0) == 0) return std::string(p);      // already full wire
  std::string out(kWirePrefix);
  if (p.empty() || p.front() != '/') out += '/';
  out += p;
  return out;
}

std::string strip_wire_prefix(std::string_view wire_path) {
  if (wire_path.rfind(kWirePrefix, 0) == 0)
    return std::string(wire_path.substr(kWirePrefix.size()));
  return std::string(wire_path);
}

BucketKind classify(Method method, std::string_view normalized_path, MutationKind* out) {
  const auto s = segs(normalized_path);
  const bool mutating = method != Method::Get;
  MutationKind mk = MutationKind::None;
  BucketKind bucket = BucketKind::Read;

  // F6 write surfaces. Matched on path segments so both the legacy
  // /portfolio/orders and the current /portfolio/events/orders forms classify
  // the same, regardless of the {order_id} trailing segment.
  const bool is_orders = has_seg(s, "orders");
  const bool is_order_groups = has_seg(s, "order_groups");
  const bool is_amend = has_seg(s, "amend") || has_seg(s, "decrease");
  const bool is_batched = has_seg(s, "batched");
  const bool is_rfq = has_seg(s, "rfqs") || has_seg(s, "quotes");
  // ASSUMPTION (F6, not in docs): block-trade accepts route to the Write bucket.
  const bool is_block_trade = has_seg(s, "block_trades");

  if (is_order_groups) {
    bucket = BucketKind::Write;
    mk = MutationKind::OrderGroup;
  } else if (is_rfq) {
    bucket = BucketKind::Write;
    mk = MutationKind::Quote;
  } else if (is_orders) {
    bucket = BucketKind::Write;
    if (is_batched) {
      mk = MutationKind::OrderGroup;  // batched create/cancel
    } else if (is_amend) {
      mk = MutationKind::AmendOrder;
    } else if (method == Method::Delete) {
      mk = MutationKind::CancelOrder;
    } else if (method == Method::Post || method == Method::Put) {
      mk = MutationKind::CreateOrder;
    } else {
      // GET on an orders path (list/lookup) is a READ, not a write.
      bucket = BucketKind::Read;
      mk = MutationKind::None;
    }
  } else if (is_block_trade && mutating) {
    bucket = BucketKind::Write;
    mk = MutationKind::None;
  }

  if (out) *out = mk;
  return bucket;
}

RequestSpec make_request_spec(Method method, std::string_view raw_path,
                              const EndpointCostTable& costs) {
  RequestSpec spec;
  spec.method = method;
  spec.raw_path = std::string(raw_path);
  spec.normalized_path = normalize_endpoint_path(raw_path);
  spec.bucket = classify(method, spec.normalized_path, &spec.mutation);
  spec.cost_tokens = costs.cost_for(method, spec.normalized_path);
  spec.idempotency_required = spec.mutation == MutationKind::CreateOrder;
  return spec;
}

}  // namespace kalshi
