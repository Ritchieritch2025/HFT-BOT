// Read-only rate-limit probe: fires authenticated batch-orderbook GETs as fast
// as possible, BYPASSING our local token bucket (raw client.request), to see
// whether the SERVER pushes back (429 = rate limited, F9). Tallies status codes.
// Never places an order.
//
//   rate_probe [num_calls=40]
//
// Env: KALSHI_ENV (demo|prod), KALSHI_ALLOW_PROD=1 for prod, KALSHI_API_KEY_ID,
//      KALSHI_PRIVATE_KEY_PATH, optional KALSHI_BASE_URL.

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/rest_api.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>
#include <vector>

using namespace kalshi;
using daemon::steady_now_ns;

int main(int argc, char** argv) {
  const int seconds = argc > 1 ? std::atoi(argv[1]) : 8;
  const int threads = argc > 2 ? std::atoi(argv[2]) : 24;

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
  cfg.pool_size = threads + 4;  // enough warm connections for the concurrent burst
  KalshiClient client(std::move(cfg));
  std::printf("host: %s\n", rt.rest_base_url.c_str());

  // Collect up to 100 open-market tickers for a heavy batch (one throttled call).
  RestApi api(client, rt);
  MarketsQuery q;
  q.limit = 100;
  q.status = "open";
  auto page = api.markets(q);
  if (!page) {
    std::fprintf(stderr, "markets fetch failed: %s (http=%ld)\n", page.error().message.c_str(),
                 page.error().http_status);
    return 1;
  }
  std::string batch = "/markets/orderbooks";
  int tickers = 0;
  for (const auto& m : page->markets) {
    batch += (tickers == 0 ? "?tickers=" : "&tickers=") + m.ticker;
    if (++tickers >= 100) break;
  }
  if (tickers == 0) { std::fprintf(stderr, "no open tickers to probe\n"); return 1; }
  std::printf("hammering with %d threads for %ds, %d-ticker batch calls, bypassing local bucket...\n",
              threads, seconds, tickers);

  std::atomic<int> n2xx{0}, n429{0}, n402{0}, nother{0}, nerr{0}, fired{0};
  std::atomic<bool> printed_body{false}, stop{false};
  const auto t0 = steady_now_ns();
  const std::uint64_t tend = t0 + std::uint64_t(seconds) * 1'000'000'000ULL;

  auto worker = [&] {
    while (!stop.load(std::memory_order_relaxed) && steady_now_ns() < tend) {
      auto r = client.request(Method::Get, batch);  // RAW: no local reservation
      fired.fetch_add(1, std::memory_order_relaxed);
      if (!r) { nerr.fetch_add(1, std::memory_order_relaxed); continue; }
      const long s = r->status;
      if (s >= 200 && s < 300) n2xx.fetch_add(1, std::memory_order_relaxed);
      else if (s == 429) n429.fetch_add(1, std::memory_order_relaxed);
      else if (s == 402) n402.fetch_add(1, std::memory_order_relaxed);
      else nother.fetch_add(1, std::memory_order_relaxed);
      if (s >= 400 && !printed_body.exchange(true))
        std::printf("first non-2xx: HTTP %ld body=%.200s\n", s, r->body.c_str());
      if (n429.load() >= 5) stop.store(true, std::memory_order_relaxed);  // confirmed
    }
  };

  std::vector<std::thread> pool;
  for (int i = 0; i < threads; ++i) pool.emplace_back(worker);
  for (auto& t : pool) t.join();
  const double el = (steady_now_ns() - t0) / 1e9;

  std::printf("\n=== rate probe: %d calls in %.2fs (%.1f req/s) ===\n", fired.load(), el,
              fired.load() / el);
  std::printf("2xx=%d  429(rate-limited)=%d  402=%d  other=%d  transport_err=%d\n",
              n2xx.load(), n429.load(), n402.load(), nother.load(), nerr.load());
  std::printf("%s\n", n429.load() > 0
                          ? "-> SERVER RATE-LIMITED us (429), as expected past the token budget."
                      : n402.load() > 0 ? "-> got 402 (unexpected for Kalshi)."
                                        : "-> no throttling seen; stayed within the tier's budget.");
  return 0;
}
