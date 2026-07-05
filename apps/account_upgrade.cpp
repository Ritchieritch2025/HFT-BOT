// One-shot API usage-tier upgrade (PLAN_TOKEN_RULES T7 / F10). Prints the tier
// BEFORE, reserves 30 Write tokens and POSTs /account/api_usage_level/upgrade
// through the RequestExecutor, then prints the tier AFTER to confirm.
//
//   201 => permanent Advanced grant created (Predictions instance).
//   403 => "no API-created order in your last 100 Predictions orders".
//
// This is the ONLY write this tool performs; it never places an order.
//
// Env: KALSHI_ENV (demo|prod), KALSHI_ALLOW_PROD=1 for prod, KALSHI_API_KEY_ID,
//      KALSHI_PRIVATE_KEY_PATH, optional KALSHI_BASE_URL.

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/rest_api.hpp"

#include <cstdio>
#include <ctime>
#include <limits>
#include <string>

using namespace kalshi;

namespace {
void print_limits(const char* when, const AccountLimits& lim) {
  std::printf("[%s] tier=%s  read=%lld/%lld  write=%lld/%lld  grants=%zu\n", when,
              lim.usage_tier.c_str(), static_cast<long long>(lim.read.refill_rate),
              static_cast<long long>(lim.read.bucket_capacity),
              static_cast<long long>(lim.write.refill_rate),
              static_cast<long long>(lim.write.bucket_capacity), lim.grants.size());
  for (const auto& g : lim.grants) {
    std::string exp = "permanent";
    if (g.expires_ts) {
      const std::time_t t = static_cast<std::time_t>(*g.expires_ts);
      char buf[32];
      std::strftime(buf, sizeof(buf), "%Y-%m-%d", std::gmtime(&t));
      exp = std::string("expires ") + buf;
    }
    std::printf("        grant: instance=%s level=%s source=%s (%s)\n",
                to_string(g.exchange_instance), g.level.c_str(), g.source.c_str(), exp.c_str());
  }
}
}  // namespace

int main() {
  Runtime rt;
  try {
    rt = resolve_runtime();
  } catch (const SafetyViolation& e) {
    std::fprintf(stderr, "refused: %s\n", e.what());
    return 2;
  }
  const std::string key_id = daemon::env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = daemon::env_or("KALSHI_PRIVATE_KEY_PATH", "");
  if (key_id.empty() || key_path.empty()) {
    std::fprintf(stderr, "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n");
    return 2;
  }
  Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = rt.rest_base_url;
  KalshiClient client(std::move(cfg));
  RestApi api(client, rt);
  std::printf("host: %s\n", rt.rest_base_url.c_str());

  // BEFORE
  auto before = api.account_limits();
  if (!before) {
    std::fprintf(stderr, "account_limits (before) failed: %s\n", before.error().message.c_str());
    return 1;
  }
  api.configure_limits(*before);  // real write-bucket capacity for the reservation
  print_limits("before", *before);

  // UPGRADE: reserve 30 Write tokens (F10), then POST through the executor.
  RequestExecutor& exec = api.executor();
  const std::int64_t deadline =
      trading::mono_ns() + std::int64_t(30) * 1'000'000'000;  // 30s budget
  const Reservation res = exec.write_bucket().reserve_or_wait(30, trading::mono_ns(), deadline);
  if (!res.granted) {
    std::fprintf(stderr, "could not reserve 30 Write tokens within budget — try again shortly\n");
    return 1;
  }
  SendOpts opts;
  opts.cost_override = 0;  // the 30-token Write cost was reserved above; don't double-charge read
  auto r = exec.send(Method::Post, "/account/api_usage_level/upgrade", "{}", opts);
  if (!r) {
    std::fprintf(stderr, "upgrade request failed: %s (kalshi_code=%s http=%ld)\n",
                 r.error().message.c_str(), r.error().kalshi_code.c_str(), r.error().http_status);
    return 1;
  }
  std::printf("[upgrade] HTTP %ld: %s\n", r->status,
              r->status == 201 ? "permanent Advanced grant created/refreshed"
              : r->status == 403 ? "REFUSED: no API-created order in your last 100 Predictions orders"
                                 : r->body.c_str());

  // AFTER
  auto after = api.account_limits();
  if (after) {
    print_limits("after", *after);
    std::printf("\nRESULT: tier is now '%s'.\n", after->usage_tier.c_str());
  } else {
    std::fprintf(stderr, "account_limits (after) failed: %s\n", after.error().message.c_str());
  }
  return 0;
}
