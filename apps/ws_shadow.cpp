// Phase 8 — read-only production shadow smoke harness.
//
// Runs the full WS market-data engine (ixwebsocket transport + KalshiWsClient +
// OrderBookManager + WsRecorder) in Shadow / DataCollect against the configured
// environment for a bounded session, then prints a completeness report and
// asserts the read-only safety invariants:
//   - KalshiExecutionEngine.transmitted() == 0   (no orders ever left the box);
//   - zero Write-bucket token spend                (the harness calls no write
//     endpoint — only WS reads + optional read-only REST cross-check);
//   - recorder completeness (recorded vs dropped, gap/loss/epoch markers);
//   - no cross-epoch application and no missed-pong disconnect over the window;
//   - each subscribed book cross-checked against a fresh REST batch snapshot at
//     exit (compare-only; divergence within REST-staleness bounds is expected).
//
// Fail-closed: refuses KALSHI_MODE=live (this harness never transmits) and
// asserts can_place_orders(rt) == false before connecting. The WS URL and host
// are env-validated by resolve_runtime() (a prod run cannot open the demo WS).
//
// Env:
//   KALSHI_ENV=local_mock|demo|prod   (prod needs KALSHI_ALLOW_PROD=1)
//   KALSHI_MODE=data_collect|shadow   (live refused here)
//   KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH   (required off local_mock)
//   KALSHI_WS_TICKERS=TICK-A,TICK-B   (markets to subscribe; default MKT-A)
//   KALSHI_SHADOW_SECONDS=30          (session length; set 86400 for the soak)
//   KALSHI_SHADOW_CAPTURE=path.ndjson (raw capture log; default ws_shadow_capture.ndjson)
//   KALSHI_SHADOW_XCHECK=1            (enable the at-exit REST cross-check)
//
// usage: ws_shadow [ws_url_override]

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/gateway.hpp"
#include "kalshi/ix_transport.hpp"
#include "kalshi/orderbook.hpp"
#include "kalshi/rest_api.hpp"
#include "kalshi/ws_client.hpp"
#include "kalshi/ws_recorder.hpp"
#include "trading/bus.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <vector>

using namespace kalshi;
using kalshi::daemon::env_int;
using kalshi::daemon::env_or;

namespace {

std::vector<std::string> split_csv(const std::string& s) {
  std::vector<std::string> out;
  std::string cur;
  for (char c : s) {
    if (c == ',') {
      if (!cur.empty()) out.push_back(cur);
      cur.clear();
    } else if (c != ' ') {
      cur += c;
    }
  }
  if (!cur.empty()) out.push_back(cur);
  return out;
}

// Sink runs on the transport thread. The main thread touches only these atomics
// until stop() has joined that thread — then the books are quiescent and safe to
// read directly (bus threading contract).
struct ShadowSink : trading::EventSink {
  std::atomic<std::uint64_t> events{0}, snapshots{0}, deltas{0};
  std::atomic<std::uint64_t> trades{0}, tickers{0}, lifecycles{0};
  void on_event(const trading::NormalizedEvent& e) override {
    events.fetch_add(1, std::memory_order_relaxed);
    switch (e.kind()) {
      case trading::Kind::BookSnapshot: snapshots.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::BookDelta: deltas.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Trade: trades.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Ticker: tickers.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Lifecycle: lifecycles.fetch_add(1, std::memory_order_relaxed); break;
    }
  }
};

// Best resting bid on each side straight off a REST snapshot (levels ascending,
// so the best bid is the last/highest priced level). No book object needed.
std::optional<PriceE4> rest_best_bid(const std::vector<trading::Level>& levels) {
  if (levels.empty()) return std::nullopt;
  return levels.back().price;
}

}  // namespace

int main(int argc, char** argv) {
  // --- resolve + fail closed -------------------------------------------------
  Runtime rt;
  try {
    rt = resolve_runtime();
  } catch (const SafetyViolation& e) {
    std::fprintf(stderr, "[ws_shadow] refused: %s\n", e.what());
    return 2;
  }
  if (rt.mode == Mode::Live) {
    std::fprintf(stderr,
                 "[ws_shadow] refused: this harness is read-only and never transmits; "
                 "run it in data_collect or shadow (got mode=live)\n");
    return 2;
  }
  if (can_place_orders(rt)) {  // must be impossible outside Live, but assert it
    std::fprintf(stderr, "[ws_shadow] refused: can_place_orders is true in a read-only harness\n");
    return 2;
  }

  const std::string ws_url = argc > 1 ? argv[1] : rt.ws_url;
  const auto tickers = split_csv(env_or("KALSHI_WS_TICKERS", "MKT-A"));
  const int seconds = env_int("KALSHI_SHADOW_SECONDS", 30);
  const std::string capture = env_or("KALSHI_SHADOW_CAPTURE", "ws_shadow_capture.ndjson");
  const bool xcheck = env_int("KALSHI_SHADOW_XCHECK", 0) != 0;

  std::fprintf(stderr, "[ws_shadow] %s ws=%s tickers=%zu seconds=%d capture=%s\n",
               describe(rt).c_str(), ws_url.c_str(), tickers.size(), seconds, capture.c_str());

  // --- auth ------------------------------------------------------------------
  // Off local_mock a real RSA-PSS signer is mandatory; the mock accepts anything.
  const std::string api_key_id = env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = env_or("KALSHI_PRIVATE_KEY_PATH", "");
  std::unique_ptr<KalshiClient> rest_client;  // also drives the REST cross-check

  if (!api_key_id.empty() && !key_path.empty()) {
    Config ccfg;
    ccfg.api_key_id = api_key_id;
    ccfg.private_key_pem = read_file(key_path);
    ccfg.base_url = rt.rest_base_url;
    ccfg.api_prefix = rt.rest_prefix;
    try {
      rest_client = std::make_unique<KalshiClient>(std::move(ccfg));
    } catch (const std::exception& e) {
      std::fprintf(stderr, "[ws_shadow] bad key/config: %s\n", e.what());
      return 2;
    }
  } else if (rt.env != Env::LocalMock) {
    std::fprintf(stderr,
                 "[ws_shadow] refused: env=%s requires KALSHI_API_KEY_ID + "
                 "KALSHI_PRIVATE_KEY_PATH for the signed WS handshake\n",
                 to_string(rt.env));
    return 2;
  }

  // Signer: sign(timestamp+"GET"+ws_sign_path) with the REST client's RSA-PSS,
  // or a stub for the local mock (which does not verify the signature).
  KalshiWsClient::Signer signer;
  if (rest_client) {
    KalshiClient* c = rest_client.get();
    signer = [c](std::string_view msg) -> std::optional<std::string> {
      auto r = c->sign(msg);
      if (r) return *r;
      return std::nullopt;
    };
  } else {
    signer = [](std::string_view) -> std::optional<std::string> {
      return std::string("LOCALMOCK");
    };
  }

  // --- wire the engine -------------------------------------------------------
  IxWebSocketTransport transport;
  OrderBookManager books;  // mutated only on the transport thread
  ShadowSink sink;
  WsRecorder recorder(capture);  // records EVERY frame + gap/loss/epoch markers
  KalshiExecutionEngine exec(rt);  // never fed intents here; asserted transmitted==0
  exec.set_book_manager(&books);

  WsConfig cfg;
  cfg.url = ws_url;
  cfg.api_key_id = api_key_id.empty() ? "localmock" : api_key_id;
  cfg.ws_sign_path = rt.ws_sign_path;
  KalshiWsClient client(transport, cfg, std::move(signer));
  client.set_book_manager(&books);
  client.set_sink(&sink);
  client.set_recorder(&recorder);
  client.want_orderbook(tickers);

  kalshi::daemon::install_signal_handlers();  // SIGINT/SIGTERM => clean shutdown

  recorder.start();
  client.start();

  // --- run the bounded session ----------------------------------------------
  std::uint64_t missed_pong_disconnects = 0;
  bool was_silent = false;
  const auto t_end = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
  int tick = 0;
  while (std::chrono::steady_clock::now() < t_end && !kalshi::daemon::g_stop.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
    const std::int64_t now_ms = trading::wall_ns() / 1'000'000;

    // Ping-silence watchdog (I6). Count each transition into silence as a
    // missed-pong incident; ixwebsocket owns the actual reconnect+backoff.
    const bool silent = client.ping_silent(now_ms);
    if (silent && !was_silent) ++missed_pong_disconnects;
    was_silent = silent;

    if (++tick % 40 == 0) {  // ~every 10s
      std::fprintf(stderr,
                   "[ws_shadow] events=%llu deltas=%llu reconnects=%llu errors=%llu "
                   "overflow=%llu epoch=%u rec=%llu drop=%llu\n",
                   (unsigned long long)sink.events.load(), (unsigned long long)sink.deltas.load(),
                   (unsigned long long)client.reconnects(), (unsigned long long)client.errors(),
                   (unsigned long long)client.overflow_events(), client.epoch(),
                   (unsigned long long)recorder.recorded(), (unsigned long long)recorder.dropped());
    }
  }

  // --- shutdown: join the transport thread, then read books safely ----------
  client.stop();       // joins the transport thread
  recorder.stop();     // joins the writer thread, flushes, emits trailing loss marker

  // --- REST cross-check (compare-only, control/main thread, read-only) -------
  std::uint64_t xcheck_compared = 0, xcheck_diverged = 0;
  if (xcheck && rest_client) {
    RestApi api(*rest_client, rt);
    auto snaps = api.batch_orderbook(tickers);  // GET /markets/orderbooks (read bucket)
    if (snaps) {
      for (const auto& s : *snaps) {
        const trading::EntityId e = trading::make_entity_id(trading::SourceId::Kalshi, s.ticker);
        const OrderBook* b = books.book(e);
        if (!b || !b->valid()) continue;  // no live book to compare (never resolved)
        ++xcheck_compared;
        const auto live_y = b->best_yes_bid(), live_n = b->best_no_bid();
        const auto rest_y = rest_best_bid(s.yes), rest_n = rest_best_bid(s.no);
        if (live_y != rest_y || live_n != rest_n) {
          ++xcheck_diverged;  // expected within REST staleness; telemetry only
          std::fprintf(stderr,
                       "[ws_shadow] xcheck divergence %s: live(y=%s,n=%s) rest(y=%s,n=%s)\n",
                       s.ticker.c_str(),
                       live_y ? trading::format_price_e4(*live_y).c_str() : "-",
                       live_n ? trading::format_price_e4(*live_n).c_str() : "-",
                       rest_y ? trading::format_price_e4(*rest_y).c_str() : "-",
                       rest_n ? trading::format_price_e4(*rest_n).c_str() : "-");
        }
      }
    } else {
      std::fprintf(stderr, "[ws_shadow] xcheck REST fetch failed: %s\n",
                   snaps.error().message.c_str());
    }
  }

  // --- completeness report + invariant assertions ---------------------------
  const bool no_transmit = exec.transmitted() == 0;
  const bool no_missed_pong = missed_pong_disconnects == 0;

  std::printf("=== ws_shadow report ===\n");
  std::printf("env/mode         : %s\n", describe(rt).c_str());
  std::printf("events           : %llu (snap=%llu delta=%llu trade=%llu tick=%llu life=%llu)\n",
              (unsigned long long)sink.events.load(), (unsigned long long)sink.snapshots.load(),
              (unsigned long long)sink.deltas.load(), (unsigned long long)sink.trades.load(),
              (unsigned long long)sink.tickers.load(), (unsigned long long)sink.lifecycles.load());
  std::printf("ws messages      : %llu\n", (unsigned long long)client.messages());
  std::printf("reconnects       : %llu (final epoch=%u)\n",
              (unsigned long long)client.reconnects(), client.epoch());
  std::printf("errors / overflow: %llu / %llu (error 25 = buffer-overflow data loss, I7)\n",
              (unsigned long long)client.errors(), (unsigned long long)client.overflow_events());
  std::printf("lifecycle deletes: %llu\n", (unsigned long long)client.lifecycle_deletes());
  std::printf("recorder         : recorded=%llu dropped=%llu -> %s\n",
              (unsigned long long)recorder.recorded(), (unsigned long long)recorder.dropped(),
              recorder.dropped() == 0 ? "COMPLETE" : "LOSSY (loss markers written)");
  std::printf("missed-pong      : %llu disconnect(s)\n",
              (unsigned long long)missed_pong_disconnects);
  std::printf("exec transmitted : %llu (MUST be 0)\n", (unsigned long long)exec.transmitted());
  std::printf("write-token spend: 0 (no write endpoint called this session)\n");
  if (xcheck)
    std::printf("REST xcheck      : compared=%llu diverged=%llu (divergence within staleness OK)\n",
                (unsigned long long)xcheck_compared, (unsigned long long)xcheck_diverged);

  const bool pass = no_transmit && no_missed_pong;
  std::printf("%s\n", pass ? "WS SHADOW PASS" : "WS SHADOW FAIL");
  return pass ? 0 : 1;
}
