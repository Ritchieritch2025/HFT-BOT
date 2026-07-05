#include "kalshi/limits.hpp"

#include "simdjson.h"

#include <array>

namespace kalshi {

namespace {

ExchangeInstance instance_from_wire(std::string_view s) {
  if (s == "event_contract") return ExchangeInstance::EventContract;
  if (s == "margined") return ExchangeInstance::Margined;
  return ExchangeInstance::Unknown;
}

Method method_from_wire(std::string_view s) {
  // Case-sensitive per OpenAPI (methods are upper-case); tolerate exact strings.
  if (s == "POST") return Method::Post;
  if (s == "PUT") return Method::Put;
  if (s == "DELETE") return Method::Delete;
  return Method::Get;  // GET + anything unrecognized routes as a read
}

// Parse a nested BucketLimit {refill_rate, bucket_capacity}. Both required.
bool parse_bucket(simdjson::ondemand::object& parent, const char* key, BucketLimit& out) {
  simdjson::ondemand::object b;
  if (parent[key].get(b) != simdjson::SUCCESS) return false;
  std::int64_t rr, cap;
  if (b["refill_rate"].get(rr) != simdjson::SUCCESS) return false;
  if (b["bucket_capacity"].get(cap) != simdjson::SUCCESS) return false;
  out.refill_rate = rr;
  out.bucket_capacity = cap;
  return true;
}

// Published tier refill_rates (F4 safeguard table): {read, write}. Tiers whose
// budgets Kalshi has not published (expert, prestige) are intentionally absent.
struct TierRow { const char* tier; std::int64_t read_refill; std::int64_t write_refill; };
constexpr std::array<TierRow, 5> kTierTable{{
    {"basic", 200, 100},
    {"advanced", 300, 300},
    {"premier", 1000, 1000},
    {"paragon", 2000, 2000},
    {"prime", 4000, 4000},
}};

std::vector<std::string_view> split_segments(std::string_view path) {
  std::vector<std::string_view> segs;
  std::size_t i = 0;
  while (i < path.size()) {
    if (path[i] == '/') { ++i; continue; }
    std::size_t j = path.find('/', i);
    if (j == std::string_view::npos) j = path.size();
    segs.push_back(path.substr(i, j - i));
    i = j;
  }
  return segs;
}

// A single-segment wildcard in a Kalshi cost-table path: ":order_id" or the
// OpenAPI "{ticker}" form both stand for exactly one path segment.
bool seg_is_param(std::string_view s) {
  return (!s.empty() && s.front() == ':') ||
         (s.size() >= 2 && s.front() == '{' && s.back() == '}');
}

bool template_match(std::string_view tmpl, std::string_view path) {
  const auto ts = split_segments(tmpl);
  const auto ps = split_segments(path);
  for (std::size_t k = 0; k < ts.size(); ++k) {
    // "*endpoint" is a glob: it matches all REMAINING path segments (>= 1).
    if (!ts[k].empty() && ts[k].front() == '*') return ps.size() > k;
    if (k >= ps.size()) return false;         // template longer than path
    if (seg_is_param(ts[k])) continue;        // ":id" / "{id}" => any one segment
    if (ts[k] != ps[k]) return false;         // literal must match exactly
  }
  return ts.size() == ps.size();              // no leftover path segments
}

}  // namespace

const char* to_string(ExchangeInstance e) {
  switch (e) {
    case ExchangeInstance::EventContract: return "event_contract";
    case ExchangeInstance::Margined: return "margined";
    case ExchangeInstance::Unknown: return "unknown";
  }
  return "unknown";
}

std::optional<AccountLimits> parse_account_limits(std::string_view json) {
  try {
    simdjson::padded_string padded(json);
    simdjson::ondemand::parser parser;
    simdjson::ondemand::document doc;
    if (parser.iterate(padded).get(doc) != simdjson::SUCCESS) return std::nullopt;
    simdjson::ondemand::object root;
    if (doc.get_object().get(root) != simdjson::SUCCESS) return std::nullopt;

    AccountLimits lim;
    lim.from_server = true;

    std::string_view tier;
    if (root["usage_tier"].get(tier) != simdjson::SUCCESS) return std::nullopt;  // required
    lim.usage_tier = std::string(tier);

    if (!parse_bucket(root, "read", lim.read)) return std::nullopt;   // required
    if (!parse_bucket(root, "write", lim.write)) return std::nullopt;  // required

    simdjson::ondemand::array grants;
    if (root["grants"].get(grants) != simdjson::SUCCESS) return std::nullopt;  // required (may be empty)
    for (auto g : grants) {
      simdjson::ondemand::object go;
      if (g.get(go) != simdjson::SUCCESS) continue;
      ApiUsageLevelGrant grant;
      std::string_view sv;
      if (go["exchange_instance"].get(sv) == simdjson::SUCCESS)
        grant.exchange_instance = instance_from_wire(sv);
      if (go["level"].get(sv) == simdjson::SUCCESS) grant.level = std::string(sv);
      if (go["source"].get(sv) == simdjson::SUCCESS) grant.source = std::string(sv);
      std::int64_t exp;
      if (go["expires_ts"].get(exp) == simdjson::SUCCESS) grant.expires_ts = exp;  // absent = permanent
      lim.grants.push_back(std::move(grant));
    }
    return lim;
  } catch (const simdjson::simdjson_error&) {
    return std::nullopt;
  }
}

AccountLimits conservative_limits() {
  // Basic tier, capacity == refill (1s of budget) — the safest assumption off-live.
  AccountLimits lim;
  lim.usage_tier = "basic";
  lim.read = {200, 200};
  lim.write = {100, 100};
  lim.from_server = false;
  return lim;
}

std::optional<std::string> limits_invariant_error(const AccountLimits& lim) {
  const auto check = [](const char* which, const BucketLimit& b) -> std::optional<std::string> {
    if (b.refill_rate <= 0)
      return std::string(which) + " refill_rate <= 0 (" + std::to_string(b.refill_rate) + ")";
    if (b.bucket_capacity < b.refill_rate / 2)
      return std::string(which) + " bucket_capacity " + std::to_string(b.bucket_capacity) +
             " < refill_rate/2 (" + std::to_string(b.refill_rate / 2) + ")";
    if (b.bucket_capacity > b.refill_rate * 10)
      return std::string(which) + " bucket_capacity " + std::to_string(b.bucket_capacity) +
             " > refill_rate*10 (" + std::to_string(b.refill_rate * 10) + ")";
    return std::nullopt;
  };
  if (auto e = check("read", lim.read)) return e;
  if (auto e = check("write", lim.write)) return e;
  return std::nullopt;
}

std::vector<std::string> tier_table_warnings(const AccountLimits& lim) {
  std::vector<std::string> out;
  for (const auto& row : kTierTable) {
    if (lim.usage_tier != row.tier) continue;
    if (lim.read.refill_rate != row.read_refill)
      out.push_back(std::string("tier '") + row.tier + "' read refill_rate is " +
                    std::to_string(lim.read.refill_rate) + " but table expects " +
                    std::to_string(row.read_refill) + " (keeping server value)");
    if (lim.write.refill_rate != row.write_refill)
      out.push_back(std::string("tier '") + row.tier + "' write refill_rate is " +
                    std::to_string(lim.write.refill_rate) + " but table expects " +
                    std::to_string(row.write_refill) + " (keeping server value)");
    break;  // tier names are unique
  }
  return out;
}

int EndpointCostTable::cost_for(Method method, std::string_view wire_path) const {
  // Exact match wins over template match.
  for (const auto& e : overrides)
    if (e.method == method && e.path == wire_path) return e.cost;
  for (const auto& e : overrides)
    if (e.method == method && template_match(e.path, wire_path)) return e.cost;
  return default_cost;
}

EndpointCostTable parse_endpoint_costs(std::string_view json) {
  EndpointCostTable table;  // default_cost=10, empty overrides
  try {
    simdjson::padded_string padded(json);
    simdjson::ondemand::parser parser;
    simdjson::ondemand::document doc;
    if (parser.iterate(padded).get(doc) != simdjson::SUCCESS) return table;
    simdjson::ondemand::object root;
    if (doc.get_object().get(root) != simdjson::SUCCESS) return table;

    std::int64_t dc;
    if (root["default_cost"].get(dc) == simdjson::SUCCESS) table.default_cost = static_cast<int>(dc);

    simdjson::ondemand::array arr;
    if (root["endpoint_costs"].get(arr) == simdjson::SUCCESS) {
      for (auto item : arr) {
        simdjson::ondemand::object o;
        if (item.get(o) != simdjson::SUCCESS) continue;
        EndpointCost ec;
        std::string_view m, p;
        std::int64_t c;
        if (o["method"].get(m) != simdjson::SUCCESS) continue;
        if (o["path"].get(p) != simdjson::SUCCESS) continue;
        if (o["cost"].get(c) != simdjson::SUCCESS) continue;
        ec.method = method_from_wire(m);
        ec.path = std::string(p);
        ec.cost = static_cast<int>(c);
        table.overrides.push_back(std::move(ec));
      }
    }
    table.from_server = true;
  } catch (const simdjson::simdjson_error&) {
    return EndpointCostTable{};  // any trouble => plain default table
  }
  return table;
}

}  // namespace kalshi
