// Ingestion layer daemon.
//
// Normalizes market data into fixed-size MarketEvents and PUBLISHes them on
// the Redis event channel for stratd. Two sources:
//
//   ingestd --synthetic [count] [interval_ms]
//       scripted TEST-* ticker tape for pipeline testing (no network)
//
//   ingestd --poll [interval_ms]
//       live Kalshi REST polling of GET /markets (public data). Publishes an
//       event whenever a market's top-of-book/last/volume changes. Uses
//       KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH if set; otherwise signs
//       with a throwaway generated key (fine for public endpoints).
//
// A WebSocket source can replace polling behind the same publish loop once
// a websocket-capable libcurl (or a native WSS client) lands.
//
// Env: REDIS_HOST/REDIS_PORT, KALSHI_BASE_URL, INGEST_MARKET_LIMIT (default 200)

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/resp.hpp"
#include "kalshi/wire.hpp"
#include "simdjson.h"

#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>

#include <cinttypes>
#include <string>
#include <thread>
#include <unordered_map>

using namespace kalshi;
using daemon::g_stop;
using daemon::logf;

namespace {

std::string throwaway_key_pem() {
  EVP_PKEY* key = EVP_RSA_gen(2048);
  if (!key) throw std::runtime_error("ingestd: RSA keygen failed");
  BIO* bio = BIO_new(BIO_s_mem());
  if (!bio || PEM_write_bio_PrivateKey(bio, key, nullptr, nullptr, 0, nullptr,
                                       nullptr) != 1) {
    EVP_PKEY_free(key);
    throw std::runtime_error("ingestd: PEM encode failed");
  }
  char* data = nullptr;
  const long len = BIO_get_mem_data(bio, &data);
  std::string pem(data, static_cast<size_t>(len));
  BIO_free(bio);
  EVP_PKEY_free(key);
  return pem;
}

class Publisher {
 public:
  Publisher(const std::string& host, int port) : redis_(host, port) {}

  void publish(wire::MarketEvent& ev) {
    ev.magic = wire::kEventMagic;
    ev.version = wire::kWireVersion;
    ev.ts_ns = daemon::now_ns();
    ev.seq = ++seq_;
    if (auto r = redis_.publish(wire::kEventChannel, wire::as_bytes(ev)); !r) {
      logf("ingestd: publish failed: %s", r.error().message.c_str());
      redis_.close();  // reconnect on next publish
    }
  }

  std::uint64_t seq() const { return seq_; }

 private:
  resp::RespClient redis_;
  std::uint64_t seq_ = 0;
};

int run_synthetic(Publisher& pub, int count, int interval_ms) {
  logf("ingestd: synthetic tape, %d events @ %dms", count, interval_ms);
  // A little scripted market: spread narrows, ask walks down through the demo
  // strategies' trigger levels, then widens again.
  for (int i = 0; i < count && !g_stop.load(std::memory_order_relaxed); ++i) {
    wire::MarketEvent ev;
    ev.kind = wire::kEventTicker;
    ev.set_ticker(i % 2 == 0 ? "TEST-MKT-A" : "TEST-MKT-B");
    ev.yes_bid = 30 + (i % 7);
    ev.yes_ask = 60 - (i % 20);       // dips to 41 -> trips ThresholdBuyer(<=45)
    ev.last_price = (ev.yes_bid + ev.yes_ask) / 2;
    ev.volume = 1000 + i;
    ev.open_interest = 5000;
    pub.publish(ev);
    std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms));
  }
  logf("ingestd: synthetic tape done (%" PRIu64 " events)", pub.seq());
  return 0;
}

int run_poll(Publisher& pub, int interval_ms) {
  Config cfg;
  cfg.api_key_id = daemon::env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = daemon::env_or("KALSHI_PRIVATE_KEY_PATH", "");
  if (cfg.api_key_id.empty() || key_path.empty()) {
    logf("ingestd: no API creds in env; using throwaway key (public data only)");
    cfg.api_key_id = "00000000-0000-0000-0000-000000000000";
    cfg.private_key_pem = throwaway_key_pem();
  } else {
    cfg.private_key_pem = read_file(key_path);
  }
  cfg.base_url = daemon::env_or("KALSHI_BASE_URL", "https://api.elections.kalshi.com");
  cfg.pool_size = 1;
  KalshiClient client(std::move(cfg));

  const int limit = daemon::env_int("INGEST_MARKET_LIMIT", 200);
  const std::string path = "/markets?status=open&limit=" + std::to_string(limit);
  logf("ingestd: polling %s every %dms", path.c_str(), interval_ms);

  struct Top {
    std::int32_t bid, ask, last;
    std::int64_t vol;
  };
  std::unordered_map<std::string, Top> book;
  simdjson::ondemand::parser parser;

  while (!g_stop.load(std::memory_order_relaxed)) {
    const auto t_next = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(interval_ms);
    auto resp = client.request(Method::Get, path);
    if (!resp) {
      logf("ingestd: poll transport error: %s", resp.error().message.c_str());
    } else if (!resp->ok()) {
      logf("ingestd: poll HTTP %ld: %.120s", resp->status, resp->body.c_str());
    } else {
      try {
        simdjson::padded_string json(resp->body);
        auto doc = parser.iterate(json);
        int published = 0;
        for (auto m : doc["markets"].get_array()) {
          const std::string_view ticker = m["ticker"].get_string();
          Top t{};
          t.bid = static_cast<std::int32_t>(int64_t(m["yes_bid"].get_int64()));
          t.ask = static_cast<std::int32_t>(int64_t(m["yes_ask"].get_int64()));
          int64_t last = 0, vol = 0;
          if (m["last_price"].get(last)) last = -1;
          if (m["volume"].get(vol)) vol = -1;
          t.last = static_cast<std::int32_t>(last);
          t.vol = vol;

          auto [it, inserted] = book.try_emplace(std::string(ticker), t);
          if (!inserted && it->second.bid == t.bid && it->second.ask == t.ask &&
              it->second.last == t.last && it->second.vol == t.vol) {
            continue;  // unchanged
          }
          it->second = t;

          wire::MarketEvent ev;
          ev.kind = wire::kEventTicker;
          ev.set_ticker(ticker);
          ev.yes_bid = t.bid;
          ev.yes_ask = t.ask;
          ev.last_price = t.last;
          ev.volume = t.vol;
          pub.publish(ev);
          ++published;
        }
        if (published > 0)
          logf("ingestd: %d changed markets (seq=%" PRIu64 ")", published, pub.seq());
      } catch (const simdjson::simdjson_error& e) {
        logf("ingestd: parse error: %s", e.what());
      }
    }
    std::this_thread::sleep_until(t_next);
  }
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  daemon::install_signal_handlers();
  Publisher pub(daemon::env_or("REDIS_HOST", "127.0.0.1"),
                daemon::env_int("REDIS_PORT", 6379));

  const std::string mode = argc > 1 ? argv[1] : "--synthetic";
  if (mode == "--synthetic") {
    const int count = argc > 2 ? std::atoi(argv[2]) : 100;
    const int interval = argc > 3 ? std::atoi(argv[3]) : 10;
    return run_synthetic(pub, count, interval);
  }
  if (mode == "--poll") {
    const int interval = argc > 2 ? std::atoi(argv[2]) : 1000;
    return run_poll(pub, interval);
  }
  std::fprintf(stderr, "usage: ingestd --synthetic [count] [interval_ms] | --poll [interval_ms]\n");
  return 2;
}
