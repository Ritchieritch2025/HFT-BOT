// live_e2e — REAL end-to-end exchange test. No mocks anywhere.
//
// One run does the full chain against the configured Kalshi environment:
//   1. REST checks: signed auth accepted, clock skew vs exchange Date header.
//   2. WS market data: real signed handshake -> subscribe orderbook_delta for
//      N tickers -> maintain books (OrderBookManager) -> record EVERY frame
//      byte-exact via WsRecorder to <outdir>/capture.ndjson.
//   3. Timing: time-to-first-WS-activity, time-to-first-book, event counts,
//      reconnects/errors/overflows, recorder completeness.
//   4. Order flow (ONLY when live mode is explicitly enabled): R rounds of
//      place post-only 1c YES bid -> cancel, on a dedicated warm Lane —
//      the exact sign_request/Lane::send path tradingd uses. Per-round
//      sign/place/cancel latencies to <outdir>/orders.ndjson.
//   5. Final book depth per ticker to <outdir>/books.ndjson; run summary to
//      <outdir>/summary.json.
//
// Load everything into SQLite afterwards:  python3 tools/load_db.py <outdir>
//
// Safety: market-data leg runs in any mode. The order leg requires
// KALSHI_MODE=live + KALSHI_ALLOW_LIVE=1 (+ KALSHI_ALLOW_PROD=1 on prod) and
// passes require_orders_allowed(); otherwise it is skipped with a notice.
// Orders are post-only 1c bids (rest or reject, never fill on entry) and every
// placed order is canceled in the same round.
//
// usage: live_e2e [--seconds N] [--tickers A,B,...] [--order-rounds R]
//                 [--order-ticker T] [--outdir DIR]
// env:   KALSHI_ENV, KALSHI_MODE (+allow flags), KALSHI_API_KEY_ID,
//        KALSHI_PRIVATE_KEY_PATH, KALSHI_WS_TICKERS (fallback for --tickers)

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/ix_transport.hpp"
#include "kalshi/orderbook.hpp"
#include "kalshi/ws_client.hpp"
#include "kalshi/ws_recorder.hpp"
#include "kalshi/wire.hpp"
#include "trading/bus.hpp"
#include "simdjson.h"

#include <sys/stat.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <vector>

using namespace kalshi;
using daemon::env_or;
using daemon::logf;
using daemon::steady_now_ns;

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

double pct(std::vector<long long> v, double p) {
  if (v.empty()) return 0;
  std::sort(v.begin(), v.end());
  return static_cast<double>(
             v[std::min(v.size() - 1, static_cast<size_t>(p * static_cast<double>(v.size())))]) /
         1000.0;  // us -> ms
}

// Transport-thread sink: atomics only; books are read after stop() joins.
struct E2ESink : trading::EventSink {
  std::atomic<std::uint64_t> events{0}, snapshots{0}, deltas{0};
  std::atomic<std::uint64_t> trades{0}, tickers{0}, lifecycles{0};
  std::atomic<std::uint64_t> first_snapshot_mono_ns{0};
  void on_event(const trading::NormalizedEvent& e) override {
    events.fetch_add(1, std::memory_order_relaxed);
    switch (e.kind()) {
      case trading::Kind::BookSnapshot: {
        snapshots.fetch_add(1, std::memory_order_relaxed);
        std::uint64_t expected = 0;
        first_snapshot_mono_ns.compare_exchange_strong(
            expected, static_cast<std::uint64_t>(trading::mono_ns()));
        break;
      }
      case trading::Kind::BookDelta: deltas.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Trade: trades.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Ticker: tickers.fetch_add(1, std::memory_order_relaxed); break;
      case trading::Kind::Lifecycle: lifecycles.fetch_add(1, std::memory_order_relaxed); break;
    }
  }
};

// Pick open markets via REST when no tickers were given.
std::vector<std::string> pick_tickers(KalshiClient& client, size_t n) {
  std::vector<std::string> out;
  auto resp = client.request(Method::Get, "/markets?status=open&limit=50");
  if (!resp || !resp->ok()) return out;
  try {
    simdjson::padded_string j(resp->body);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(j);
    for (auto m : doc["markets"].get_array()) {
      std::string_view t;
      if (m["ticker"].get(t) == simdjson::SUCCESS && !t.empty() && t.size() <= 41) {
        out.emplace_back(t);
        if (out.size() >= n) break;
      }
    }
  } catch (...) {}
  return out;
}

std::string json_escape_min(const std::string& s) {
  std::string o;
  for (char c : s) {
    if (c == '"' || c == '\\') { o += '\\'; o += c; }
    else if (static_cast<unsigned char>(c) >= 0x20) o += c;
  }
  return o;
}

}  // namespace

int main(int argc, char** argv) {
  int seconds = 60, order_rounds = 0;
  std::string tickers_csv = env_or("KALSHI_WS_TICKERS", "");
  std::string order_ticker, outdir;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    auto next = [&]() -> const char* { return i + 1 < argc ? argv[++i] : ""; };
    if (a == "--seconds") seconds = std::atoi(next());
    else if (a == "--tickers") tickers_csv = next();
    else if (a == "--order-rounds") order_rounds = std::atoi(next());
    else if (a == "--order-ticker") order_ticker = next();
    else if (a == "--outdir") outdir = next();
    else { std::fprintf(stderr, "unknown arg: %s\n", a.c_str()); return 2; }
  }

  // --- runtime + fail-closed gates -----------------------------------------
  Runtime rt;
  try {
    rt = resolve_runtime();
  } catch (const SafetyViolation& e) {
    std::fprintf(stderr, "[e2e] config refused: %s\n", e.what());
    return 2;
  }
  const bool orders_wanted = order_rounds > 0;
  const bool orders_allowed = can_place_orders(rt);
  logf("live_e2e: %s", describe(rt).c_str());

  const std::string key_id = env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = env_or("KALSHI_PRIVATE_KEY_PATH", "");
  if (key_id.empty() || key_path.empty()) {
    std::fprintf(stderr, "[e2e] set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n");
    return 2;
  }

  if (outdir.empty()) {
    ::mkdir("work", 0755);
    outdir = "work/e2e_" + std::to_string(daemon::now_ns() / 1'000'000ULL);
  }
  ::mkdir(outdir.c_str(), 0755);
  logf("live_e2e: outdir=%s seconds=%d order_rounds=%d", outdir.c_str(), seconds, order_rounds);

  Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = rt.rest_base_url;
  cfg.api_prefix = rt.rest_prefix;
  cfg.pool_size = 2;
  KalshiClient client(std::move(cfg));
  simdjson::ondemand::parser parser;

  // --- 1. REST checks: auth + clock ----------------------------------------
  long long clock_skew_ms = 0;
  long long rest_status_rtt_us = 0;
  {
    auto s = client.request(Method::Get, "/exchange/status");
    if (!s) {
      std::fprintf(stderr, "[e2e] GET /exchange/status transport error: %s\n",
                   s.error().message.c_str());
      return 1;
    }
    rest_status_rtt_us = s->total_time_us;
    logf("live_e2e: REST /exchange/status HTTP %ld in %.1fms", s->status,
         s->total_time_us / 1000.0);
    if (s->server_date_ms > 0) {
      clock_skew_ms = static_cast<long long>(daemon::now_ns() / 1'000'000ULL) - s->server_date_ms;
      logf("live_e2e: clock skew vs exchange: %lldms (keep within ~2s; run NTP)", clock_skew_ms);
    }
    auto bal = client.request(Method::Get, "/portfolio/balance");
    if (bal && bal->ok()) logf("live_e2e: auth OK (balance endpoint accepted signature)");
    else logf("live_e2e: WARNING balance check failed (HTTP %ld) — key/env mismatch?",
              bal ? bal->status : -1);
  }

  // --- tickers ---------------------------------------------------------------
  std::vector<std::string> tickers = split_csv(tickers_csv);
  if (tickers.empty()) {
    tickers = pick_tickers(client, 3);
    logf("live_e2e: no --tickers given; auto-picked %zu open markets", tickers.size());
  }
  if (tickers.empty()) {
    std::fprintf(stderr, "[e2e] no tickers available\n");
    return 1;
  }
  for (const auto& t : tickers) logf("live_e2e: ticker %s", t.c_str());

  // --- 2+3. WS leg -----------------------------------------------------------
  IxWebSocketTransport transport;
  OrderBookManager books;
  E2ESink sink;
  WsRecorder recorder(outdir + "/capture.ndjson");
  WsConfig wcfg;
  wcfg.url = rt.ws_url;
  wcfg.api_key_id = key_id;
  wcfg.ws_sign_path = rt.ws_sign_path;
  KalshiClient* cptr = &client;
  KalshiWsClient ws(transport, wcfg,
                    [cptr](std::string_view msg) -> std::optional<std::string> {
                      auto r = cptr->sign(msg);
                      if (r) return *r;
                      return std::nullopt;
                    });
  ws.set_book_manager(&books);
  ws.set_sink(&sink);
  ws.set_recorder(&recorder);
  ws.want_orderbook(tickers);

  daemon::install_signal_handlers();
  recorder.start();
  const std::uint64_t t_ws_start = steady_now_ns();
  ws.start();

  std::uint64_t first_activity_mono = 0;
  const auto t_end = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
  int tick = 0;
  while (std::chrono::steady_clock::now() < t_end && !daemon::g_stop.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
    if (first_activity_mono == 0 && ws.last_activity_ms() != 0)
      first_activity_mono = steady_now_ns();
    if (++tick % 40 == 0) {
      logf("live_e2e: events=%llu (snap=%llu delta=%llu trade=%llu tick=%llu) "
           "msgs=%llu reconnects=%llu errors=%llu rec=%llu drop=%llu",
           (unsigned long long)sink.events.load(), (unsigned long long)sink.snapshots.load(),
           (unsigned long long)sink.deltas.load(), (unsigned long long)sink.trades.load(),
           (unsigned long long)sink.tickers.load(), (unsigned long long)ws.messages(),
           (unsigned long long)ws.reconnects(), (unsigned long long)ws.errors(),
           (unsigned long long)recorder.recorded(), (unsigned long long)recorder.dropped());
    }
  }
  ws.stop();        // joins transport thread — books now safe to read
  recorder.stop();  // joins writer thread, flushes

  const double first_activity_ms =
      first_activity_mono ? (first_activity_mono - t_ws_start) / 1e6 : -1.0;
  const std::uint64_t fs = sink.first_snapshot_mono_ns.load();
  const double first_book_ms = fs ? (fs - t_ws_start) / 1e6 : -1.0;

  // --- final book depth dump -------------------------------------------------
  {
    std::FILE* f = std::fopen((outdir + "/books.ndjson").c_str(), "wb");
    if (f) {
      for (const auto& t : tickers) {
        const trading::EntityId e = trading::make_entity_id(trading::SourceId::Kalshi, t);
        const OrderBook* b = books.book(e);
        std::string line = "{\"type\":\"book_final\",\"ticker\":\"" + json_escape_min(t) + "\"";
        if (!b) {
          line += ",\"present\":false}";
        } else {
          line += ",\"present\":true,\"valid\":";
          line += b->valid() ? "true" : "false";
          line += ",\"seq\":" + std::to_string(b->last_seq());
          line += ",\"yes\":[";
          bool first = true;
          for (const auto& l : b->yes_depth()) {
            if (!first) line += ',';
            first = false;
            line += "[" + std::to_string(l.price) + "," + std::to_string(l.size) + "]";
          }
          line += "],\"no\":[";
          first = true;
          for (const auto& l : b->no_depth()) {
            if (!first) line += ',';
            first = false;
            line += "[" + std::to_string(l.price) + "," + std::to_string(l.size) + "]";
          }
          line += "]}";
        }
        line += "\n";
        std::fwrite(line.data(), 1, line.size(), f);
        if (b && b->valid()) logf("live_e2e: %s %s", t.c_str(), b->dump_state().c_str());
      }
      std::fclose(f);
    }
  }

  // --- 4. order leg (real orders; explicit live gates only) ------------------
  std::vector<long long> sign_us, place_us, cancel_us;
  int placed_n = 0, canceled_n = 0, rejected_n = 0;
  if (orders_wanted) {
    if (!orders_allowed) {
      logf("live_e2e: ORDER LEG SKIPPED — requires KALSHI_MODE=live + KALSHI_ALLOW_LIVE=1 "
           "(and KALSHI_ALLOW_PROD=1 on prod). Market-data results are unaffected.");
    } else {
      try {
        require_orders_allowed(rt);  // the same single choke point as tradingd
      } catch (const SafetyViolation& e) {
        std::fprintf(stderr, "[e2e] order gate refused: %s\n", e.what());
        return 1;
      }
      std::string ot = order_ticker.empty() ? tickers.front() : order_ticker;
      logf("live_e2e: order leg on %s — %d rounds of post-only 1c YES bid + cancel",
           ot.c_str(), order_rounds);
      auto lane = client.make_lane();
      {
        auto warm = lane.ping();
        logf("live_e2e: lane %s", (warm && warm->ok()) ? "warm" : "warmup failed");
      }
      std::FILE* of = std::fopen((outdir + "/orders.ndjson").c_str(), "ab");

      for (int i = 0; i < order_rounds && !daemon::g_stop.load(); ++i) {
        wire::ExecPayload p;
        p.action = wire::kActionBuy;
        p.side = wire::kSideYes;
        p.order_type = wire::kTypeLimit;
        p.count = 1;
        p.price_cents = 1;
        p.strategy_id = 0;
        p.seq = static_cast<std::uint64_t>(i) + 1;
        p.ts_ns = daemon::now_ns();
        p.set_ticker(ot);
        const std::string coid = wire::client_order_id(p);
        const std::string body = wire::order_json(p, /*post_only=*/true);

        const std::uint64_t t0 = steady_now_ns();
        auto req = client.sign_request(Method::Post, wire::kCreateOrderPath, body);
        const std::uint64_t t1 = steady_now_ns();
        long place_status = 0, cancel_status = 0;
        std::string order_id;
        std::uint64_t t2 = t1, t3 = 0, t4 = 0;
        if (!req) {
          logf("live_e2e: round %d sign failed: %s", i, req.error().message.c_str());
        } else {
          auto placed = lane.send(*req);
          t2 = steady_now_ns();
          if (!placed) {
            logf("live_e2e: round %d transport: %s", i, placed.error().message.c_str());
            place_status = -1;
          } else {
            place_status = placed->status;
            if (placed->status == 201 || placed->status == 200) {
              ++placed_n;
              sign_us.push_back(static_cast<long long>((t1 - t0) / 1000));
              place_us.push_back(static_cast<long long>((t2 - t1) / 1000));
              try {
                simdjson::padded_string j(placed->body);
                auto doc = parser.iterate(j);
                std::string_view id;
                if (doc["order_id"].get(id) == simdjson::SUCCESS) order_id = std::string(id);
                else if (doc["order"]["order_id"].get(id) == simdjson::SUCCESS)
                  order_id = std::string(id);
              } catch (...) {}
            } else {
              ++rejected_n;
              logf("live_e2e: round %d HTTP %ld: %.200s", i, placed->status,
                   placed->body.c_str());
            }
          }
          if (!order_id.empty()) {
            t3 = steady_now_ns();
            auto creq = client.sign_request(
                Method::Delete, std::string(wire::kCreateOrderPath) + "/" + order_id);
            if (creq) {
              auto cancel = lane.send(*creq);
              t4 = steady_now_ns();
              if (cancel) cancel_status = cancel->status;
              if (cancel && cancel->ok()) {
                ++canceled_n;
                cancel_us.push_back(static_cast<long long>((t4 - t3) / 1000));
              } else {
                logf("live_e2e: round %d CANCEL FAILED for %s — resolve manually!", i,
                     order_id.c_str());
              }
            }
          }
        }
        if (of) {
          std::fprintf(of,
                       "{\"type\":\"order_round\",\"ts_ms\":%llu,\"round\":%d,"
                       "\"ticker\":\"%s\",\"client_order_id\":\"%s\",\"order_id\":\"%s\","
                       "\"sign_us\":%lld,\"place_us\":%lld,\"cancel_us\":%lld,"
                       "\"place_status\":%ld,\"cancel_status\":%ld}\n",
                       (unsigned long long)(daemon::now_ns() / 1'000'000ULL), i,
                       json_escape_min(ot).c_str(), coid.c_str(), order_id.c_str(),
                       (long long)((t1 - t0) / 1000), (long long)((t2 - t1) / 1000),
                       t4 > t3 ? (long long)((t4 - t3) / 1000) : -1LL, place_status,
                       cancel_status);
          std::fflush(of);
        }
      }
      if (of) std::fclose(of);
    }
  }

  // --- 5. summary -------------------------------------------------------------
  {
    std::string s = "{\n";
    s += "  \"env\": \"" + std::string(to_string(rt.env)) + "\",\n";
    s += "  \"mode\": \"" + std::string(to_string(rt.mode)) + "\",\n";
    s += "  \"ws_url\": \"" + json_escape_min(rt.ws_url) + "\",\n";
    s += "  \"seconds\": " + std::to_string(seconds) + ",\n";
    s += "  \"tickers\": [";
    for (size_t i = 0; i < tickers.size(); ++i) {
      if (i) s += ",";
      s += "\"" + json_escape_min(tickers[i]) + "\"";
    }
    s += "],\n";
    s += "  \"clock_skew_ms\": " + std::to_string(clock_skew_ms) + ",\n";
    s += "  \"rest_status_rtt_ms\": " + std::to_string(rest_status_rtt_us / 1000.0) + ",\n";
    s += "  \"ws_first_activity_ms\": " + std::to_string(first_activity_ms) + ",\n";
    s += "  \"ws_first_book_ms\": " + std::to_string(first_book_ms) + ",\n";
    s += "  \"events\": " + std::to_string(sink.events.load()) + ",\n";
    s += "  \"snapshots\": " + std::to_string(sink.snapshots.load()) + ",\n";
    s += "  \"deltas\": " + std::to_string(sink.deltas.load()) + ",\n";
    s += "  \"trades\": " + std::to_string(sink.trades.load()) + ",\n";
    s += "  \"ws_messages\": " + std::to_string(ws.messages()) + ",\n";
    s += "  \"reconnects\": " + std::to_string(ws.reconnects()) + ",\n";
    s += "  \"errors\": " + std::to_string(ws.errors()) + ",\n";
    s += "  \"overflow_events\": " + std::to_string(ws.overflow_events()) + ",\n";
    s += "  \"recorder_recorded\": " + std::to_string(recorder.recorded()) + ",\n";
    s += "  \"recorder_dropped\": " + std::to_string(recorder.dropped()) + ",\n";
    s += "  \"orders_placed\": " + std::to_string(placed_n) + ",\n";
    s += "  \"orders_canceled\": " + std::to_string(canceled_n) + ",\n";
    s += "  \"orders_rejected\": " + std::to_string(rejected_n) + ",\n";
    s += "  \"sign_ms_p50\": " + std::to_string(pct(sign_us, 0.5)) + ",\n";
    s += "  \"place_ms_p50\": " + std::to_string(pct(place_us, 0.5)) + ",\n";
    s += "  \"place_ms_p99\": " + std::to_string(pct(place_us, 0.99)) + ",\n";
    s += "  \"cancel_ms_p50\": " + std::to_string(pct(cancel_us, 0.5)) + "\n";
    s += "}\n";
    if (std::FILE* f = std::fopen((outdir + "/summary.json").c_str(), "wb")) {
      std::fwrite(s.data(), 1, s.size(), f);
      std::fclose(f);
    }
  }

  // --- report ------------------------------------------------------------------
  std::printf("=== live_e2e report (%s) ===\n", outdir.c_str());
  std::printf("env/mode          : %s\n", describe(rt).c_str());
  std::printf("clock skew        : %lldms\n", clock_skew_ms);
  std::printf("WS first activity : %.1fms   first book: %.1fms\n", first_activity_ms,
              first_book_ms);
  std::printf("events            : %llu (snap=%llu delta=%llu trade=%llu tick=%llu)\n",
              (unsigned long long)sink.events.load(), (unsigned long long)sink.snapshots.load(),
              (unsigned long long)sink.deltas.load(), (unsigned long long)sink.trades.load(),
              (unsigned long long)sink.tickers.load());
  std::printf("reconnects/errors : %llu / %llu (overflow=%llu)\n",
              (unsigned long long)ws.reconnects(), (unsigned long long)ws.errors(),
              (unsigned long long)ws.overflow_events());
  std::printf("recorder          : recorded=%llu dropped=%llu -> %s\n",
              (unsigned long long)recorder.recorded(), (unsigned long long)recorder.dropped(),
              recorder.dropped() == 0 ? "COMPLETE" : "LOSSY (loss markers written)");
  if (orders_wanted && orders_allowed) {
    std::printf("orders            : placed=%d canceled=%d rejected=%d\n", placed_n, canceled_n,
                rejected_n);
    if (!sign_us.empty()) {
      std::printf("RSA-PSS sign      : p50=%.1fms\n", pct(sign_us, 0.5));
      std::printf("place wire->ack   : p50=%.1fms p99=%.1fms\n", pct(place_us, 0.5),
                  pct(place_us, 0.99));
      std::printf("cancel wire->ack  : p50=%.1fms\n", pct(cancel_us, 0.5));
      std::printf("decision->ack est : p50=%.1fms (sign + place, warm lane)\n",
                  pct(sign_us, 0.5) + pct(place_us, 0.5));
    }
  } else if (orders_wanted) {
    std::printf("orders            : SKIPPED (live gates not enabled)\n");
  }
  std::printf("next              : python3 tools/load_db.py %s\n", outdir.c_str());

  const bool md_ok = sink.snapshots.load() > 0 && recorder.recorded() > 0;
  const bool order_ok = !orders_wanted || !orders_allowed || (placed_n > 0 && canceled_n == placed_n);
  std::printf("%s\n", (md_ok && order_ok) ? "LIVE E2E PASS" : "LIVE E2E FAIL");
  return (md_ok && order_ok) ? 0 : 1;
}
