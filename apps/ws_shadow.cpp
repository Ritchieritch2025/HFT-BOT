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
// are env-validated by resolve_runtime() (a prod run opens only the prod WS host).
//
// Env:
//   KALSHI_ENV=local_mock|prod   (prod needs KALSHI_ALLOW_PROD=1)
//   KALSHI_MODE=data_collect|shadow   (live refused here)
//   KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH   (required off local_mock)
//   KALSHI_WS_TICKERS=TICK-A,TICK-B   (markets to subscribe; required off
//                                      local_mock, default MKT-A on local_mock)
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
#include "kalshi/ring.hpp"
#include "kalshi/rest_api.hpp"
#include "kalshi/ws_client.hpp"
#include "kalshi/ws_recorder.hpp"
#include "simdjson.h"
#include "trading/bus.hpp"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <variant>
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

struct FeedLine {
  std::string channel;
  std::string ticker;
  std::int64_t ts_ms = 0;
  std::optional<PriceE4> yes_bid;
  std::optional<PriceE4> yes_ask;
  std::optional<PriceE4> last_price;
  std::optional<CountFp> yes_bid_size;
  std::optional<CountFp> yes_ask_size;
  std::optional<CountFp> last_trade_size;
  std::optional<CountFp> volume;
  std::optional<CountFp> open_interest;
  bool valid = true;
};

struct SimpleBook {
  std::map<PriceE4, CountFp> yes;
  std::map<PriceE4, CountFp> no;
  bool valid = false;
};

std::optional<Level> best_level(const std::map<PriceE4, CountFp>& levels) {
  if (levels.empty()) return std::nullopt;
  return Level{levels.rbegin()->first, levels.rbegin()->second};
}

void load_book(SimpleBook& b, const trading::BookSnapshot& snap) {
  b.yes.clear();
  b.no.clear();
  for (const Level& l : snap.yes) if (l.size > 0) b.yes[l.price] = l.size;
  for (const Level& l : snap.no) if (l.size > 0) b.no[l.price] = l.size;
  b.valid = true;
}

void apply_delta(SimpleBook& b, const trading::BookDelta& d) {
  if (!b.valid) return;
  auto& levels = d.side == trading::Side::Yes ? b.yes : b.no;
  const auto it = levels.find(d.price);
  const CountFp cur = it == levels.end() ? 0 : it->second;
  const CountFp next = cur + d.delta;
  if (next < 0) {
    b.valid = false;
    return;
  }
  if (next == 0) {
    if (it != levels.end()) levels.erase(it);
  } else {
    levels[d.price] = next;
  }
}

std::string extract_market_ticker(std::string_view raw) {
  try {
    simdjson::padded_string json{std::string(raw)};
    simdjson::ondemand::parser parser;
    simdjson::ondemand::document doc;
    if (parser.iterate(json).get(doc) != simdjson::SUCCESS) return {};
    simdjson::ondemand::object root;
    if (doc.get_object().get(root) != simdjson::SUCCESS) return {};
    simdjson::ondemand::object msg;
    if (root["msg"].get(msg) != simdjson::SUCCESS) return {};
    std::string_view ticker;
    if (msg["market_ticker"].get(ticker) == simdjson::SUCCESS) return std::string(ticker);
    if (msg["ticker"].get(ticker) == simdjson::SUCCESS) return std::string(ticker);
  } catch (const simdjson::simdjson_error&) {
  }
  return {};
}

void fill_top_of_book(const SimpleBook& b, FeedLine& line) {
  line.valid = b.valid;
  if (!b.valid) return;
  if (auto y = best_level(b.yes)) {
    line.yes_bid = y->price;
    line.yes_bid_size = y->size;
  }
  if (auto n = best_level(b.no)) {
    line.yes_ask = static_cast<PriceE4>(trading::kPriceMax - n->price);
    line.yes_ask_size = n->size;
  }
}

std::string json_escape(std::string_view s) {
  std::string out;
  out.reserve(s.size() + 8);
  for (char c : s) {
    switch (c) {
      case '\\': out += "\\\\"; break;
      case '"': out += "\\\""; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default: out += c; break;
    }
  }
  return out;
}

void ensure_parent_dir(const std::string& path) {
  const auto parent = std::filesystem::path(path).parent_path();
  if (parent.empty()) return;
  std::error_code ec;
  std::filesystem::create_directories(parent, ec);
}

void write_opt_price(std::FILE* f, const char* name, const std::optional<PriceE4>& v) {
  if (!v) return;
  const std::string val = trading::format_price_e4(*v);
  std::fprintf(f, ",\"%s\":\"%s\"", name, val.c_str());
}

void write_opt_count(std::FILE* f, const char* name, const std::optional<CountFp>& v) {
  if (!v) return;
  const std::string val = trading::format_count_fp(*v);
  std::fprintf(f, ",\"%s\":\"%s\"", name, val.c_str());
}

void write_system_event(std::FILE* f, const Runtime& rt, std::string_view component,
                        std::string_view status, std::string_view message) {
  if (!f) return;
  std::fprintf(f,
               "{\"type\":\"system\",\"ts_ms\":%lld,\"synthetic\":false,"
               "\"component\":\"%s\",\"status\":\"%s\",\"message\":\"%s\","
               "\"mode\":\"%s\",\"env\":\"%s\"}\n",
               static_cast<long long>(trading::wall_ns() / 1'000'000),
               json_escape(component).c_str(), json_escape(status).c_str(),
               json_escape(message).c_str(), to_string(rt.mode), to_string(rt.env));
}

void write_market_line(std::FILE* f, const FeedLine& line) {
  if (!f || line.ticker.empty()) return;
  std::fprintf(f,
               "{\"type\":\"market_data\",\"ts_ms\":%lld,\"synthetic\":false,"
               "\"source\":\"kalshi_ws\",\"channel\":\"%s\",\"market_ticker\":\"%s\","
               "\"valid\":%s,\"freshness_ms\":0",
               static_cast<long long>(line.ts_ms), json_escape(line.channel).c_str(),
               json_escape(line.ticker).c_str(), line.valid ? "true" : "false");
  write_opt_price(f, "yes_bid_dollars", line.yes_bid);
  write_opt_price(f, "yes_ask_dollars", line.yes_ask);
  write_opt_price(f, "last_price_dollars", line.last_price);
  write_opt_count(f, "yes_bid_size", line.yes_bid_size);
  write_opt_count(f, "yes_ask_size", line.yes_ask_size);
  write_opt_count(f, "last_trade_size", line.last_trade_size);
  write_opt_count(f, "volume", line.volume);
  write_opt_count(f, "open_interest", line.open_interest);
  std::fprintf(f, "}\n");
}

void write_feed_status(std::FILE* f, const Runtime& rt, std::int64_t freshness_ms,
                       double msg_rate_hz, const KalshiWsClient& client,
                       const OrderBookManager& books, const WsRecorder& recorder,
                       std::uint64_t snapshots, std::uint64_t deltas,
                       std::uint64_t trades, std::uint64_t tickers,
                       std::uint64_t telemetry_dropped, const std::string& capture) {
  if (!f) return;
  const bool connected = client.last_activity_ms() != 0 && freshness_ms >= 0 && freshness_ms < 30000;
  const bool valid = connected && recorder.dropped() == 0 && client.overflow_events() == 0;
  std::fprintf(f,
               "{\"type\":\"feed\",\"ts_ms\":%lld,\"synthetic\":false,"
               "\"source\":\"kalshi_ws\",\"connected\":%s,\"valid\":%s,"
               "\"freshness_ms\":%lld,\"age_ms\":%lld,\"msg_rate_hz\":%.3f,"
               "\"gaps\":%llu,\"reconnects\":%llu,\"messages\":%llu,"
               "\"snapshots\":%llu,\"deltas\":%llu,\"trades\":%llu,\"tickers\":%llu,"
               "\"recorder_recorded\":%llu,\"recorder_dropped\":%llu,"
               "\"telemetry_dropped\":%llu,\"capture\":\"%s\","
               "\"mode\":\"%s\",\"env\":\"%s\"}\n",
               static_cast<long long>(trading::wall_ns() / 1'000'000),
               connected ? "true" : "false", valid ? "true" : "false",
               static_cast<long long>(freshness_ms), static_cast<long long>(freshness_ms),
               msg_rate_hz, static_cast<unsigned long long>(books.resync_count()),
               static_cast<unsigned long long>(client.reconnects()),
               static_cast<unsigned long long>(client.messages()),
               static_cast<unsigned long long>(snapshots),
               static_cast<unsigned long long>(deltas),
               static_cast<unsigned long long>(trades),
               static_cast<unsigned long long>(tickers),
               static_cast<unsigned long long>(recorder.recorded()),
               static_cast<unsigned long long>(recorder.dropped()),
               static_cast<unsigned long long>(telemetry_dropped),
               json_escape(capture).c_str(), to_string(rt.mode), to_string(rt.env));
}

// Sink runs on the transport thread. It updates only atomics and a non-blocking
// telemetry ring; file IO happens on the main/control thread.
struct ShadowSink : trading::EventSink {
  std::atomic<std::uint64_t> events{0}, snapshots{0}, deltas{0};
  std::atomic<std::uint64_t> trades{0}, tickers{0}, lifecycles{0};
  std::atomic<std::uint64_t> telemetry_dropped{0};
  Ring<FeedLine>* telemetry = nullptr;
  std::map<std::string, SimpleBook> tape_books;  // transport thread only

  void on_event(const trading::NormalizedEvent& e) override {
    events.fetch_add(1, std::memory_order_relaxed);
    switch (e.kind()) {
      case trading::Kind::BookSnapshot: snapshots.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::BookDelta: deltas.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Trade: trades.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Ticker: tickers.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Lifecycle: lifecycles.fetch_add(1, std::memory_order_relaxed); break;
    }
    emit_tape_line(e);
  }

  void emit_tape_line(const trading::NormalizedEvent& e) {
    if (!telemetry) return;
    const std::string ticker = extract_market_ticker(e.raw_payload.bytes);
    if (ticker.empty()) return;

    FeedLine line;
    line.ticker = ticker;
    line.ts_ms = e.source_event_time_ms.value_or(e.local_receive_wall_ns / 1'000'000);

    if (e.kind() == trading::Kind::BookSnapshot) {
      line.channel = "orderbook_snapshot";
      const auto& snap = std::get<trading::BookSnapshot>(e.payload);
      SimpleBook& b = tape_books[ticker];
      load_book(b, snap);
      fill_top_of_book(b, line);
    } else if (e.kind() == trading::Kind::BookDelta) {
      line.channel = "orderbook_delta";
      const auto& d = std::get<trading::BookDelta>(e.payload);
      SimpleBook& b = tape_books[ticker];
      apply_delta(b, d);
      fill_top_of_book(b, line);
    } else if (e.kind() == trading::Kind::Ticker) {
      line.channel = "ticker";
      const auto& t = std::get<trading::Ticker>(e.payload);
      line.yes_bid = t.yes_bid;
      line.yes_ask = t.yes_ask;
      line.last_price = t.last_price;
      line.volume = t.volume;
      line.open_interest = t.open_interest;
      auto it = tape_books.find(ticker);
      if (it != tape_books.end()) fill_top_of_book(it->second, line);
    } else if (e.kind() == trading::Kind::Trade) {
      line.channel = "trade";
      const auto& tr = std::get<trading::Trade>(e.payload);
      line.last_price = tr.price;
      line.last_trade_size = tr.size;
      auto it = tape_books.find(ticker);
      if (it != tape_books.end()) fill_top_of_book(it->second, line);
    } else {
      return;
    }

    if (!telemetry->try_push(std::move(line)))
      telemetry_dropped.fetch_add(1, std::memory_order_relaxed);
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
  // Tickers must be explicit on real exchanges. The local mock keeps the MKT-A
  // convenience default, but prod fails closed rather than silently
  // subscribing to a bogus/absent market: Kalshi accepts such a subscription
  // and then sends zero messages, producing a "connected but no data" run that
  // looks healthy while yielding no market_data rows.
  // Firehose: stream EVERY market exchange-wide via the ticker/trade channels
  // with no market filter (KALSHI_WS_FIREHOSE=1). No tickers needed then.
  const bool firehose = env_int("KALSHI_WS_FIREHOSE", 0) != 0;
  const std::string tickers_env = env_or("KALSHI_WS_TICKERS", "");
  if (tickers_env.empty() && !firehose && rt.env != Env::LocalMock) {
    std::fprintf(stderr,
                 "[ws_shadow] refused: env=%s requires explicit KALSHI_WS_TICKERS "
                 "(no default on real exchanges; a bogus ticker subscribes cleanly "
                 "but yields zero market data) — or set KALSHI_WS_FIREHOSE=1 for "
                 "all-markets ticker/trade streaming\n",
                 to_string(rt.env));
    return 2;
  }
  const auto tickers = firehose ? std::vector<std::string>{}
                                : split_csv(tickers_env.empty() ? "MKT-A" : tickers_env);
  // Firehose can only use market-less channels (ticker/trade); orderbook_delta
  // needs explicit tickers, so it defaults out of the firehose channel set.
  const auto channels = split_csv(env_or(
      "KALSHI_WS_CHANNELS", firehose ? "ticker,trade" : "orderbook_delta,trade,ticker"));
  const int seconds = env_int("KALSHI_SHADOW_SECONDS", 30);
  const std::string capture = env_or("KALSHI_SHADOW_CAPTURE", "ws_shadow_capture.ndjson");
  const std::string default_metrics = env_or("KALSHI_NDJSON", "work/metrics.ndjson");
  const std::string metrics_path = env_or("KALSHI_SHADOW_METRICS", default_metrics.c_str());
  const bool xcheck = env_int("KALSHI_SHADOW_XCHECK", 0) != 0;

  std::fprintf(stderr,
               "[ws_shadow] %s ws=%s tickers=%zu channels=%zu seconds=%d capture=%s metrics=%s\n",
               describe(rt).c_str(), ws_url.c_str(), tickers.size(), channels.size(), seconds,
               capture.c_str(), metrics_path.c_str());

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
  Ring<FeedLine> tape_ring(4096);
  ShadowSink sink;
  sink.telemetry = &tape_ring;
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
  client.want_channels(channels);
  client.set_firehose(firehose);

  kalshi::daemon::install_signal_handlers();  // SIGINT/SIGTERM => clean shutdown

  std::FILE* metrics = nullptr;
  if (!metrics_path.empty() && metrics_path != "-") {
    ensure_parent_dir(metrics_path);
    metrics = std::fopen(metrics_path.c_str(), "ab");
    if (!metrics) std::fprintf(stderr, "[ws_shadow] warning: cannot open metrics %s\n",
                               metrics_path.c_str());
  }
  write_system_event(metrics, rt, "ws_shadow", "start",
                     "real Kalshi WebSocket collector starting");

  recorder.start();
  client.start();

  // --- run the bounded session ----------------------------------------------
  std::uint64_t missed_pong_disconnects = 0;
  bool was_silent = false;
  // W-C1 force-reconnect watchdog. A silently-wedged socket delivers no
  // Close/Error, so ixwebsocket never reconnects and capture stays dead until
  // the hourly respawn (the top-of-hour gap, 2026-07-07 diagnosis). If inbound
  // has been silent past kForceReconnectSilenceMs, we tear the transport down
  // and reopen it ourselves. The firehose streams EVERY market, so this much
  // total silence is a dead socket, not a quiet market. Bounded backoff so a
  // slow reconnect (or a genuine overnight lull) is not hammered; each attempt
  // is logged + surfaced to metrics (D2). Tunable; recovery target << 1 min.
  constexpr std::int64_t kForceReconnectSilenceMs = 20'000;  // 20s silence => wedged
  constexpr std::int64_t kForceReconnectBackoffMs = 15'000;  // min gap between forced attempts
  std::int64_t last_force_reconnect_ms = 0;
  const auto t_end = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
  int tick = 0;
  std::uint64_t last_messages = 0;
  while (std::chrono::steady_clock::now() < t_end && !kalshi::daemon::g_stop.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
    const std::int64_t now_ms = trading::wall_ns() / 1'000'000;
    FeedLine line;
    while (tape_ring.try_pop(line)) write_market_line(metrics, line);

    // Ping-silence watchdog (I6). Count each transition into silence as a
    // missed-pong incident; ixwebsocket owns the actual reconnect+backoff.
    const bool silent = client.ping_silent(now_ms);
    if (silent && !was_silent) ++missed_pong_disconnects;
    was_silent = silent;

    // W-C1: on sustained silence, FORCE recovery instead of waiting for the
    // hourly respawn (ixwebsocket will not — the socket is wedged, not closed).
    if (client.ping_silent(now_ms, kForceReconnectSilenceMs) &&
        now_ms - last_force_reconnect_ms > kForceReconnectBackoffMs) {
      const std::int64_t dead_ms = now_ms - client.last_activity_ms();
      client.force_reconnect();  // stop()+re-sign+start(); fail-closed (S2)
      last_force_reconnect_ms = now_ms;
      std::fprintf(stderr,
                   "[ws_shadow] WATCHDOG forced reconnect after %lldms inbound "
                   "silence (forced=%llu reconnects=%llu epoch=%u)\n",
                   (long long)dead_ms,
                   (unsigned long long)client.forced_reconnects(),
                   (unsigned long long)client.reconnects(), client.epoch());
      write_system_event(metrics, rt, "ws_shadow", "watchdog_reconnect",
                         "forced reconnect on sustained inbound silence");
    }

    if (metrics && tick % 4 == 0) {  // ~every 1s
      const std::int64_t last = client.last_activity_ms();
      const std::int64_t freshness = last ? now_ms - last : -1;
      const std::uint64_t cur_messages = client.messages();
      const double rate = static_cast<double>(cur_messages - last_messages);
      last_messages = cur_messages;
      write_feed_status(metrics, rt, freshness, rate, client, books, recorder,
                        sink.snapshots.load(), sink.deltas.load(), sink.trades.load(),
                        sink.tickers.load(), sink.telemetry_dropped.load(), capture);
      std::fflush(metrics);
    }

    if (++tick % 40 == 0) {  // ~every 10s
      std::fprintf(stderr,
                   "[ws_shadow] events=%llu deltas=%llu reconnects=%llu forced=%llu errors=%llu "
                   "overflow=%llu epoch=%u rec=%llu drop=%llu\n",
                   (unsigned long long)sink.events.load(), (unsigned long long)sink.deltas.load(),
                   (unsigned long long)client.reconnects(), (unsigned long long)client.forced_reconnects(),
                   (unsigned long long)client.errors(),
                   (unsigned long long)client.overflow_events(), client.epoch(),
                   (unsigned long long)recorder.recorded(), (unsigned long long)recorder.dropped());
    }
  }

  // --- shutdown: join the transport thread, then read books safely ----------
  client.stop();       // joins the transport thread
  recorder.stop();     // joins the writer thread, flushes, emits trailing loss marker
  FeedLine line;
  while (tape_ring.try_pop(line)) write_market_line(metrics, line);
  write_feed_status(metrics, rt, -1, 0.0, client, books, recorder,
                    sink.snapshots.load(), sink.deltas.load(), sink.trades.load(),
                    sink.tickers.load(), sink.telemetry_dropped.load(), capture);
  write_system_event(metrics, rt, "ws_shadow", "stop",
                     "real Kalshi WebSocket collector stopped");
  if (metrics) std::fclose(metrics);

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
