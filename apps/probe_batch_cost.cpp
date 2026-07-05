// Empirical batch-orderbook token-cost probe (PLAN_TOKEN_RULES T6 / F8).
// READ-ONLY: sends batch orderbook GETs (never an order) in a tight loop at a
// known tier and reports how many succeed before the server throttles, so the
// per-batch token cost can be inferred (tier_read_rate / calls_per_sec) and
// recorded in the rulebook (T9). Requires a real demo/prod key.
//
// Env: KALSHI_ENV (demo|prod), KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH.
// Args: probe_batch_cost <seconds> <ticker> [<ticker> ...]

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/rest_api.hpp"

#include <chrono>
#include <cstdio>
#include <string>
#include <vector>

using namespace kalshi;

int main(int argc, char** argv) {
  if (argc < 3) {
    std::fprintf(stderr, "usage: probe_batch_cost <seconds> <ticker> [ticker ...]\n");
    return 2;
  }
  const int seconds = std::atoi(argv[1]);
  std::vector<std::string> tickers;
  for (int i = 2; i < argc; ++i) tickers.emplace_back(argv[i]);

  Runtime rt;
  try {
    rt = resolve_runtime();
  } catch (const SafetyViolation& e) {
    std::fprintf(stderr, "[probe] refused: %s\n", e.what());
    return 2;
  }
  if (rt.env == Env::LocalMock) {
    std::fprintf(stderr, "[probe] needs a real demo/prod key (KALSHI_ENV=demo)\n");
    return 2;
  }

  const std::string key_id = daemon::env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = daemon::env_or("KALSHI_PRIVATE_KEY_PATH", "");
  if (key_id.empty() || key_path.empty()) {
    std::fprintf(stderr, "[probe] set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH\n");
    return 2;
  }
  Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = rt.rest_base_url;
  KalshiClient client(std::move(cfg));

  RestApi api(client, rt);
  if (auto lim = api.account_limits()) {
    api.configure_limits(*lim);
    if (auto costs = api.endpoint_costs()) api.set_cost_table(*costs);
    std::printf("[probe] tier=%s read=%lld/s write=%lld/s, batch of %zu tickers\n",
                lim->usage_tier.c_str(), static_cast<long long>(lim->read.refill_rate),
                static_cast<long long>(lim->write.refill_rate), tickers.size());
  } else {
    std::fprintf(stderr, "[probe] account_limits fetch failed: %s\n", lim.error().message.c_str());
    return 1;
  }

  std::uint64_t ok = 0, refused = 0;
  const auto t0 = std::chrono::steady_clock::now();
  const auto tend = t0 + std::chrono::seconds(seconds);
  while (std::chrono::steady_clock::now() < tend) {
    auto r = api.batch_orderbook(tickers);
    if (r) {
      ++ok;
    } else {
      ++refused;
      if (r.error().kalshi_code == "rate_limited" || r.error().http_status == 429) {
        // local throttle or server 429: the interesting event
      }
    }
  }
  const double el = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
  std::printf("[probe] %.1fs: ok=%llu refused=%llu -> %.1f batch calls/s (infer cost = read_rate / calls_per_s)\n",
              el, static_cast<unsigned long long>(ok), static_cast<unsigned long long>(refused),
              ok / el);
  return 0;
}
