#pragma once
// Market-data feed sources, shared by tradingd (live trading, in-process
// dispatch) and ingestd (tape recorder). Each runs a blocking loop on the
// calling thread, invoking the handler once per normalized MarketEvent
// (ts_ns/seq stamped) until daemon::g_stop is set. A future WebSocket feed
// slots in with the same shape: blocking read loop, one callback per event.

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/wire.hpp"
#include "simdjson.h"

#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>

#include <cinttypes>
#include <functional>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>

namespace kalshi::feed {

using EventHandler = std::function<void(wire::MarketEvent&)>;

// For running against public endpoints without account credentials (market
// data GETs ignore the key). Order paths need real credentials.
inline std::string throwaway_key_pem() {
  EVP_PKEY* key = EVP_RSA_gen(2048);
  if (!key) throw std::runtime_error("feed: RSA keygen failed");
  BIO* bio = BIO_new(BIO_s_mem());
  if (!bio || PEM_write_bio_PrivateKey(bio, key, nullptr, nullptr, 0, nullptr,
                                       nullptr) != 1) {
    EVP_PKEY_free(key);
    throw std::runtime_error("feed: PEM encode failed");
  }
  char* data = nullptr;
  const long len = BIO_get_mem_data(bio, &data);
  std::string pem(data, static_cast<size_t>(len));
  BIO_free(bio);
  EVP_PKEY_free(key);
  return pem;
}

// Scripted TEST-* tape: spread narrows, the ask walks down through the demo
// strategies' trigger levels, then widens again.
inline int run_synthetic(int count, int interval_ms, const EventHandler& on_event) {
  daemon::logf("feed: synthetic tape, %d events @ %dms", count, interval_ms);
  std::uint64_t seq = 0;
  for (int i = 0; i < count && !daemon::g_stop.load(std::memory_order_relaxed); ++i) {
    wire::MarketEvent ev;
    ev.kind = wire::kEventTicker;
    ev.set_ticker(i % 2 == 0 ? "TEST-MKT-A" : "TEST-MKT-B");
    ev.yes_bid = 30 + (i % 7);
    ev.yes_ask = 60 - (i % 20);  // dips to 41 -> trips ThresholdBuyer(<=45)
    ev.last_price = (ev.yes_bid + ev.yes_ask) / 2;
    ev.volume = 1000 + i;
    ev.open_interest = 5000;
    ev.ts_ns = daemon::now_ns();
    ev.seq = ++seq;
    on_event(ev);
    std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms));
  }
  daemon::logf("feed: synthetic tape done (%" PRIu64 " events)", seq);
  return 0;
}

// Live REST polling of GET /markets (public data): emits an event whenever a
// market's top-of-book/last/volume changes. `client` should be a dedicated
// pool_size=1 KalshiClient so market-data RTT never starves order lanes.
inline int run_poll(KalshiClient& client, int interval_ms,
                    const EventHandler& on_event) {
  const int limit = daemon::env_int("INGEST_MARKET_LIMIT", 200);
  const std::string path = "/markets?status=open&limit=" + std::to_string(limit);
  daemon::logf("feed: polling %s every %dms", path.c_str(), interval_ms);

  struct Top {
    std::int32_t bid, ask, last;
    std::int64_t vol;
  };
  std::unordered_map<std::string, Top> book;
  simdjson::ondemand::parser parser;
  std::uint64_t seq = 0;

  while (!daemon::g_stop.load(std::memory_order_relaxed)) {
    const auto t_next = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(interval_ms);
    auto resp = client.request(Method::Get, path);
    if (!resp) {
      daemon::logf("feed: poll transport error: %s", resp.error().message.c_str());
    } else if (!resp->ok()) {
      daemon::logf("feed: poll HTTP %ld: %.120s", resp->status, resp->body.c_str());
    } else {
      try {
        simdjson::padded_string json(resp->body);
        auto doc = parser.iterate(json);
        int published = 0;
        for (auto m : doc["markets"].get_array()) {
          // Field-tolerant: live market objects omit fields (no bid, never
          // traded, ...). A missing field is "absent" (-1), never an abort.
          std::string_view ticker;
          if (m["ticker"].get(ticker) != simdjson::SUCCESS) continue;
          Top t{};
          std::int64_t v = 0;
          t.bid = m["yes_bid"].get(v) ? -1 : static_cast<std::int32_t>(v);
          t.ask = m["yes_ask"].get(v) ? -1 : static_cast<std::int32_t>(v);
          t.last = m["last_price"].get(v) ? -1 : static_cast<std::int32_t>(v);
          t.vol = m["volume"].get(v) ? -1 : v;

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
          ev.ts_ns = daemon::now_ns();
          ev.seq = ++seq;
          on_event(ev);
          ++published;
        }
        if (published > 0)
          daemon::logf("feed: %d changed markets (seq=%" PRIu64 ")", published, seq);
      } catch (const simdjson::simdjson_error& e) {
        daemon::logf("feed: parse error: %s", e.what());
      }
    }
    std::this_thread::sleep_until(t_next);
  }
  return 0;
}

}  // namespace kalshi::feed
