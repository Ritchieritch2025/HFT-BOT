// Integration smoke: real ixwebsocket transport + KalshiWsClient against
// tests/mock_ws_exchange.py (plain ws://). Verifies the vendored library
// handshakes with our KALSHI-ACCESS-* headers, delivers subscribe ->
// subscribed -> snapshot -> deltas, and echoes the "heartbeat" ping (the mock
// prints HEARTBEAT_OK on receiving the pong). Read-only; never sends orders.
//
// usage: ws_smoke [ws://host:port/trade-api/ws/v2]

#include "kalshi/ix_transport.hpp"
#include "kalshi/orderbook.hpp"
#include "kalshi/ws_client.hpp"
#include "trading/bus.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <string>
#include <thread>

using namespace kalshi;

// Sink runs on the transport thread; main reads only atomics until stop() has
// joined that thread (then the book is safe to touch). Avoids a data race.
struct SmokeSink : trading::EventSink {
  std::atomic<std::uint64_t> events{0};
  std::atomic<std::uint64_t> last_seq{0};
  std::atomic<bool> got_snapshot{false};
  void on_event(const trading::NormalizedEvent& e) override {
    events.fetch_add(1, std::memory_order_relaxed);
    if (e.kind() == trading::Kind::BookSnapshot) got_snapshot.store(true);
    if (e.source_sequence) last_seq.store(*e.source_sequence);
  }
};

int main(int argc, char** argv) {
  const std::string url = argc > 1 ? argv[1] : "ws://127.0.0.1:18200/trade-api/ws/v2";

  IxWebSocketTransport transport;
  OrderBookManager books;  // mutated only on the transport thread
  SmokeSink sink;
  WsConfig cfg;
  cfg.url = url;
  cfg.api_key_id = "smoke";
  KalshiWsClient client(transport, cfg,
                        [](std::string_view) { return std::string("SMOKESIG"); });
  client.set_book_manager(&books);
  client.set_sink(&sink);
  client.want_orderbook({"MKT-A"});

  std::printf("connecting %s\n", url.c_str());
  client.start();

  bool ok = false;
  for (int i = 0; i < 80; ++i) {  // up to ~4s, polling atomics only
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    if (sink.got_snapshot.load() && sink.last_seq.load() >= 3) { ok = true; break; }
  }
  // Let the heartbeat pong round-trip, then stop (joins the transport thread).
  std::this_thread::sleep_for(std::chrono::milliseconds(300));
  client.stop();

  // Transport thread is now joined — safe to read the book directly.
  const trading::EntityId A = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-A");
  if (const OrderBook* b = books.book(A); b && b->valid())
    std::printf("book: %s\n", b->dump_state().c_str());
  std::printf("events=%llu last_seq=%llu epoch=%u\n",
              static_cast<unsigned long long>(sink.events.load()),
              static_cast<unsigned long long>(sink.last_seq.load()), client.epoch());

  std::printf(ok ? "WS SMOKE PASS\n" : "WS SMOKE FAIL\n");
  return ok ? 0 : 1;
}
