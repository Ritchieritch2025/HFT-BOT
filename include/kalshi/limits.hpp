#pragma once
//
// Account rate-limit + endpoint-cost metadata (docs/PLAN_TOKEN_RULES.md T1,
// facts F3/F4/F5/F7). The v3.23.0 /account/limits schema is NESTED:
//   { usage_tier, read: BucketLimit, write: BucketLimit, grants: [] }
// with BucketLimit = { refill_rate (tokens/sec), bucket_capacity (max tokens) }.
// Buckets are initialized DIRECTLY from the server's bucket_capacity (F7) — no
// local 1s/2s burst derivation. The tier table is a warn-only SAFEGUARD (F4),
// never a rescale.
//
// Parsing lives in src/limits.cpp (simdjson) and is exposed as pure functions so
// tests feed fixture JSON directly — no live server needed.

#include "kalshi/client.hpp"  // Method

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace kalshi {

// Telemetry event names (stable strings). The NDJSON telemetry sink lands in T5;
// token_accounting_drift feeds the DATA_QUALITY_GATES global trip.
inline constexpr const char* kEvLimitsTableMismatch = "limits_table_mismatch";
inline constexpr const char* kEvTokenAccountingDrift = "token_accounting_drift";

struct BucketLimit {
  std::int64_t refill_rate = 0;      // tokens per second (F4: explicitly per-second)
  std::int64_t bucket_capacity = 0;  // max tokens; server-provided (F7)
};

enum class ExchangeInstance : std::uint8_t { EventContract, Margined, Unknown };
const char* to_string(ExchangeInstance);

struct ApiUsageLevelGrant {
  ExchangeInstance exchange_instance = ExchangeInstance::Unknown;
  std::string level;
  std::string source;                      // "volume" | "manual" (open string)
  std::optional<std::int64_t> expires_ts;  // absent = permanent grant
};

struct AccountLimits {
  std::string usage_tier;  // open string (7 public tiers today; tolerate unknown)
  BucketLimit read;
  BucketLimit write;
  std::vector<ApiUsageLevelGrant> grants;
  bool from_server = false;  // false => conservative fallback, not a real pull
};

// Parse the nested v3.23.0 /account/limits body. All four top-level fields
// (usage_tier, read, write, grants) are REQUIRED — missing any => nullopt.
// Unknown extra fields tolerated; unknown usage_tier / exchange_instance values
// tolerated (grant instance => Unknown). Malformed JSON => nullopt.
std::optional<AccountLimits> parse_account_limits(std::string_view json);

// Conservative fallback (basic tier) used OFF-LIVE when the fetch/parse fails.
// Never used in live mode — live fails closed instead (T1.5).
AccountLimits conservative_limits();

// Fail-closed invariant checks (T1.2): for each of read/write, refill_rate must
// be > 0, bucket_capacity >= refill_rate/2, and bucket_capacity <= refill_rate*10.
// Returns a human-readable error if any bucket violates; nullopt if all sane.
std::optional<std::string> limits_invariant_error(const AccountLimits&);

// Warn-only tier-table safeguard (F4). For a KNOWN public tier, compares the
// server refill_rates to the published table and returns a warning per mismatch.
// Never rescales; empty => no mismatch (or an unknown/unpublished tier). On
// mismatch the server values are authoritative and kept.
std::vector<std::string> tier_table_warnings(const AccountLimits&);

// --- endpoint costs (F5) ---

struct EndpointCost {
  Method method = Method::Get;
  std::string path;  // full wire path; may be a server template with {seg} wildcards
  int cost = 0;
};

struct EndpointCostTable {
  int default_cost = 10;  // F5: currently 10; absent in body => this default
  std::vector<EndpointCost> overrides;
  bool from_server = false;

  // Cost for a full wire path (query already stripped). Exact (method,path) match
  // first, then segment-wise template match ({x} matches exactly one segment),
  // else default_cost. Method must match.
  int cost_for(Method method, std::string_view wire_path) const;
};

// Parse /account/endpoint_costs. Absent default_cost => 10; absent/empty list =>
// no overrides. Malformed entries skipped. Malformed JSON => default table.
EndpointCostTable parse_endpoint_costs(std::string_view json);

}  // namespace kalshi
