// Fixture-driven tests for the nested v3.23.0 /account/limits parser + the F4
// tier-table safeguard + T1.2 fail-closed invariant checks. No network: parse
// functions are fed literal JSON.

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

  // Full nested body: advanced tier, read/write BucketLimits, one permanent + one
  // expiring grant, plus an unknown extra top-level field (must be tolerated).
  const char* full = R"({
    "usage_tier":"advanced",
    "read":{"refill_rate":300,"bucket_capacity":300},
    "write":{"refill_rate":300,"bucket_capacity":600},
    "grants":[
      {"exchange_instance":"event_contract","level":"advanced","source":"volume"},
      {"exchange_instance":"margined","level":"advanced","source":"manual","expires_ts":1893456000}
    ],
    "some_future_field":42
  })";
  {
    auto lim = parse_account_limits(full);
    check(lim.has_value(), "full nested body parses");
    check(lim && lim->usage_tier == "advanced", "usage_tier parsed");
    check(lim && lim->read.refill_rate == 300 && lim->read.bucket_capacity == 300, "read BucketLimit");
    check(lim && lim->write.refill_rate == 300 && lim->write.bucket_capacity == 600, "write BucketLimit");
    check(lim && lim->from_server, "from_server true on a real parse");
    check(lim && lim->grants.size() == 2, "two grants");
    check(lim && lim->grants[0].exchange_instance == ExchangeInstance::EventContract &&
              lim->grants[0].source == "volume" && !lim->grants[0].expires_ts,
          "grant 0: event_contract, volume, permanent (no expires_ts)");
    check(lim && lim->grants[1].exchange_instance == ExchangeInstance::Margined &&
              lim->grants[1].expires_ts.has_value() && *lim->grants[1].expires_ts == 1893456000,
          "grant 1: margined, expiring");
    // advanced published as 300/300 -> no tier-table warning.
    check(lim && tier_table_warnings(*lim).empty(), "advanced matches tier table (no warning)");
    check(lim && !limits_invariant_error(*lim), "sane limits pass invariant checks");
  }

  // Missing each required top-level field => nullopt.
  check(!parse_account_limits(R"({"read":{"refill_rate":1,"bucket_capacity":1},"write":{"refill_rate":1,"bucket_capacity":1},"grants":[]})").has_value(),
        "missing usage_tier => nullopt");
  check(!parse_account_limits(R"({"usage_tier":"basic","write":{"refill_rate":1,"bucket_capacity":1},"grants":[]})").has_value(),
        "missing read => nullopt");
  check(!parse_account_limits(R"({"usage_tier":"basic","read":{"refill_rate":1,"bucket_capacity":1},"grants":[]})").has_value(),
        "missing write => nullopt");
  check(!parse_account_limits(R"({"usage_tier":"basic","read":{"refill_rate":1,"bucket_capacity":1},"write":{"refill_rate":1,"bucket_capacity":1}})").has_value(),
        "missing grants => nullopt");
  // A BucketLimit missing a sub-field is also a parse failure.
  check(!parse_account_limits(R"({"usage_tier":"basic","read":{"refill_rate":1},"write":{"refill_rate":1,"bucket_capacity":1},"grants":[]})").has_value(),
        "read missing bucket_capacity => nullopt");
  // Non-object / malformed => nullopt (hardened parse).
  check(!parse_account_limits("[]").has_value(), "array top-level => nullopt");
  check(!parse_account_limits("not json").has_value(), "garbage => nullopt");
  check(!parse_account_limits("").has_value(), "empty => nullopt");

  // Unknown usage_tier value tolerated (open string); unknown grant instance => Unknown.
  {
    auto lim = parse_account_limits(R"({"usage_tier":"platinum_plus","read":{"refill_rate":9,"bucket_capacity":9},"write":{"refill_rate":9,"bucket_capacity":9},"grants":[{"exchange_instance":"crypto","level":"x","source":"manual"}]})");
    check(lim && lim->usage_tier == "platinum_plus", "unknown usage_tier tolerated");
    check(lim && lim->grants.size() == 1 && lim->grants[0].exchange_instance == ExchangeInstance::Unknown,
          "unknown exchange_instance => Unknown");
    check(lim && tier_table_warnings(*lim).empty(), "unknown tier => no tier-table check");
  }

  // Tier-table mismatch: server says basic but read refill 999 (table expects 200).
  {
    auto lim = parse_account_limits(R"({"usage_tier":"basic","read":{"refill_rate":999,"bucket_capacity":999},"write":{"refill_rate":100,"bucket_capacity":100},"grants":[]})");
    auto warns = lim ? tier_table_warnings(*lim) : std::vector<std::string>{};
    check(!warns.empty(), "tier-table mismatch produces a warning");
    check(lim && lim->read.refill_rate == 999, "server value kept, never rescaled");
  }

  // Fail-closed invariant violations.
  {
    auto zero = parse_account_limits(R"({"usage_tier":"x","read":{"refill_rate":0,"bucket_capacity":0},"write":{"refill_rate":1,"bucket_capacity":1},"grants":[]})");
    check(zero && limits_invariant_error(*zero).has_value(), "refill_rate 0 => invariant error");
    auto absurd = parse_account_limits(R"({"usage_tier":"x","read":{"refill_rate":100,"bucket_capacity":100000},"write":{"refill_rate":100,"bucket_capacity":100},"grants":[]})");
    check(absurd && limits_invariant_error(*absurd).has_value(), "capacity/refill ratio > 10 => invariant error");
    auto tiny = parse_account_limits(R"({"usage_tier":"x","read":{"refill_rate":100,"bucket_capacity":10},"write":{"refill_rate":100,"bucket_capacity":100},"grants":[]})");
    check(tiny && limits_invariant_error(*tiny).has_value(), "capacity < refill/2 => invariant error");
  }

  // Conservative fallback is a sane basic tier that passes invariants.
  {
    auto lim = conservative_limits();
    check(!lim.from_server && lim.usage_tier == "basic", "conservative_limits is basic, from_server=false");
    check(!limits_invariant_error(lim), "conservative_limits passes invariant checks");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
