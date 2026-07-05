// Placement acceptance gate: measures cold and warm RTT from THIS machine to
// the Kalshi API over a dedicated lane (exactly the connection an order
// rides). Run it on any candidate instance before deploying there — the box
// with the lowest warm p50 wins. Uses a throwaway key when no creds are set
// (only public endpoints are hit).
//
// usage: bench_rtt [n_warm=30] [base_url]

#include "daemon_util.hpp"
#include "feed.hpp"
#include "kalshi/client.hpp"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

using namespace kalshi;

int main(int argc, char** argv) {
  const int n = argc > 1 ? std::atoi(argv[1]) : 30;
  Config cfg;
  cfg.base_url = argc > 2 ? argv[2]
                          : daemon::env_or("KALSHI_BASE_URL",
                                           "https://external-api.kalshi.com");
  cfg.api_key_id = daemon::env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = daemon::env_or("KALSHI_PRIVATE_KEY_PATH", "");
  if (!cfg.api_key_id.empty() && !key_path.empty()) {
    cfg.private_key_pem = read_file(key_path);
  } else {
    std::printf("(no creds in env — signing with a throwaway key; public endpoint only)\n");
    cfg.api_key_id = "00000000-0000-0000-0000-000000000000";
    cfg.private_key_pem = feed::throwaway_key_pem();
  }
  cfg.pool_size = 1;
  KalshiClient client(std::move(cfg));
  auto lane = client.make_lane();

  std::printf("target: %s\n", client.config().base_url.c_str());
  auto cold = lane.ping();
  if (!cold) {
    std::printf("cold request failed: %s\n", cold.error().message.c_str());
    return 1;
  }
  std::printf("cold (TCP+TLS handshake + first request): %.1f ms\n",
              static_cast<double>(cold->total_time_us) / 1000.0);

  std::vector<long long> warm;
  warm.reserve(static_cast<size_t>(n));
  for (int i = 0; i < n; ++i) {
    auto r = lane.ping();
    if (r && r->ok()) warm.push_back(r->total_time_us);
  }
  if (warm.empty()) {
    std::printf("no successful warm requests\n");
    return 1;
  }
  std::sort(warm.begin(), warm.end());
  const auto pct = [&](double p) {
    return static_cast<double>(
               warm[std::min(warm.size() - 1,
                             static_cast<size_t>(p * static_cast<double>(warm.size())))]) /
           1000.0;
  };
  const double p50 = pct(0.50);
  std::printf("warm lane RTT, n=%zu: min=%.1f p50=%.1f p90=%.1f p99=%.1f ms\n",
              warm.size(), static_cast<double>(warm.front()) / 1000.0, p50,
              pct(0.90), pct(0.99));

  if (cold->server_date_ms > 0) {
    const long long skew =
        static_cast<long long>(daemon::now_ns() / 1'000'000ULL) - cold->server_date_ms;
    std::printf("clock skew vs exchange: %lldms (Date header, 1s resolution)\n", skew);
  }

  if (p50 < 5.0) {
    std::printf("PLACEMENT OK: warm p50 %.1fms supports a sub-5ms order path\n", p50);
  } else {
    std::printf("PLACEMENT TOO FAR: warm p50 %.1fms — for sub-5ms, deploy in AWS "
                "us-east-1 (Kalshi's origin region)\n", p50);
  }
  return 0;
}
