// Read-only account tier / rate-limit inspector. Fetches GET /account/limits and
// /account/endpoint_costs (PLAN_TOKEN_RULES T1) and prints the tier, per-bucket
// refill_rate/bucket_capacity, and grants. Never places an order.
//
// Env: KALSHI_ENV (demo|prod), KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH,
//      optional KALSHI_BASE_URL.

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/rest_api.hpp"

#include <cstdio>
#include <ctime>
#include <string>

using namespace kalshi;

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

  auto lim = api.account_limits();
  if (!lim) {
    std::fprintf(stderr, "account_limits failed: %s (kalshi_code=%s http=%ld)\n",
                 lim.error().message.c_str(), lim.error().kalshi_code.c_str(),
                 lim.error().http_status);
    return 1;
  }
  if (!lim->from_server) {
    std::fprintf(stderr,
                 "NOTE: returned the conservative fallback (server fetch failed) — "
                 "the values below are NOT your real tier.\n");
  }

  std::printf("\n==== Account API usage tier ====\n");
  std::printf("usage_tier : %s\n", lim->usage_tier.c_str());
  std::printf("read  bucket: refill_rate=%lld tokens/s  bucket_capacity=%lld\n",
              static_cast<long long>(lim->read.refill_rate),
              static_cast<long long>(lim->read.bucket_capacity));
  std::printf("write bucket: refill_rate=%lld tokens/s  bucket_capacity=%lld\n",
              static_cast<long long>(lim->write.refill_rate),
              static_cast<long long>(lim->write.bucket_capacity));

  std::printf("grants: %zu\n", lim->grants.size());
  for (const auto& g : lim->grants) {
    std::string expiry = "permanent";
    if (g.expires_ts) {
      const std::time_t t = static_cast<std::time_t>(*g.expires_ts);
      char buf[32];
      std::strftime(buf, sizeof(buf), "%Y-%m-%d", std::gmtime(&t));
      expiry = std::string("expires ") + buf;
    }
    std::printf("  - instance=%s level=%s source=%s (%s)\n",
                to_string(g.exchange_instance), g.level.c_str(), g.source.c_str(),
                expiry.c_str());
  }

  auto costs = api.endpoint_costs();
  if (costs) {
    std::printf("\n==== Official endpoint costs (GET /account/endpoint_costs) ====\n");
    std::printf("default_cost = %d tokens%s\n", costs->default_cost,
                costs->from_server ? "" : "  (FALLBACK — server fetch failed)");
    std::printf("non-default entries: %zu\n", costs->overrides.size());
    for (const auto& e : costs->overrides)
      std::printf("  %-6s %-48s cost=%d\n", method_name(e.method), e.path.c_str(), e.cost);
    if (costs->overrides.empty())
      std::printf("  (none listed -> every endpoint costs default_cost)\n");
  } else {
    std::fprintf(stderr, "endpoint_costs fetch failed: %s\n", costs.error().message.c_str());
  }
  return 0;
}
