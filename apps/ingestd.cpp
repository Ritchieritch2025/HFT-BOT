// Market-tape recorder (cold path — NOT part of the live order path).
//
// Publishes normalized MarketEvents onto the Redis event channel so research/
// replay/backtest tooling can subscribe or record. Live trading runs inside
// tradingd, which consumes the same feed sources in-process.
//
//   ingestd --poll [interval_ms]   live Kalshi REST polling
//
// Env: REDIS_HOST/REDIS_PORT, KALSHI_BASE_URL, INGEST_MARKET_LIMIT,
//      KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH (optional for --poll;
//      falls back to a throwaway key — public data only).

#include "daemon_util.hpp"
#include "feed.hpp"
#include "kalshi/resp.hpp"
#include "kalshi/wire.hpp"

#include <cinttypes>
#include <string>

using namespace kalshi;
using daemon::logf;

namespace {

class TapePublisher {
 public:
  TapePublisher(const std::string& host, int port) : redis_(host, port) {}

  void operator()(wire::MarketEvent& ev) {
    if (auto r = redis_.publish(wire::kEventChannel, wire::as_bytes(ev)); !r) {
      logf("ingestd: publish failed: %s", r.error().message.c_str());
      redis_.close();  // reconnect on next publish
    }
  }

 private:
  resp::RespClient redis_;
};

}  // namespace

int main(int argc, char** argv) {
  daemon::install_signal_handlers();
  TapePublisher publish(daemon::env_or("REDIS_HOST", "127.0.0.1"),
                        daemon::env_int("REDIS_PORT", 6379));
  const feed::EventHandler handler = [&](wire::MarketEvent& ev,
                                         const feed::EventTiming&) { publish(ev); };

  const std::string mode = argc > 1 ? argv[1] : "";
  if (mode == "--poll") {
    const int interval = argc > 2 ? std::atoi(argv[2]) : 1000;
    Config cfg;
    cfg.api_key_id = daemon::env_or("KALSHI_API_KEY_ID", "");
    const std::string key_path = daemon::env_or("KALSHI_PRIVATE_KEY_PATH", "");
    if (cfg.api_key_id.empty() || key_path.empty()) {
      logf("ingestd: no API creds in env; using throwaway key (public data only)");
      cfg.api_key_id = "00000000-0000-0000-0000-000000000000";
      cfg.private_key_pem = feed::throwaway_key_pem();
    } else {
      cfg.private_key_pem = read_file(key_path);
    }
    cfg.base_url = daemon::env_or("KALSHI_BASE_URL", "https://external-api.kalshi.com");
    cfg.pool_size = 1;
    KalshiClient client(std::move(cfg));
    return feed::run_poll(client, interval, handler);
  }
  std::fprintf(stderr, "usage: ingestd --poll [interval_ms]\n");
  return 2;
}
