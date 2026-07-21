// KalshiWsClient logic tests via MockWebSocketTransport (no sockets, no network):
// auth-header construction, command builders, envelope routing into the
// sid-aware OrderBookManager, control-seq handling, gap -> resync, error 25,
// heartbeat liveness, and reconnect epoch bump + resubscribe.

#include "kalshi/orderbook.hpp"
#include "kalshi/ws_client.hpp"
#include "kalshi/ws_transport.hpp"

#include <iostream>
#include <string>
#include <vector>

using namespace kalshi;
using trading::EntityId;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
bool has(const std::string& hay, const std::string& needle) {
  return hay.find(needle) != std::string::npos;
}
std::string header_val(const WsHeaders& h, const std::string& key) {
  for (const auto& [k, v] : h)
    if (k == key) return v;
  return {};
}
struct RecResync : ResyncHandler {
  int calls = 0;
  void request_resync(std::uint64_t, const std::vector<EntityId>&) override { ++calls; }
};
WsConfig cfg() {
  WsConfig c;
  c.url = "ws://127.0.0.1:0/trade-api/ws/v2";
  c.api_key_id = "key-1";
  return c;
}
}  // namespace

int main() {
  const EntityId A = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-A");

  // --- auth headers: signer receives ts+"GET"+sign_path; 3 headers ---
  {
    std::string signed_msg;
    MockWebSocketTransport t;
    KalshiWsClient c(t, cfg(), [&](std::string_view m) -> std::optional<std::string> {
      signed_msg = std::string(m);
      return std::string("BASE64SIG");
    });
    auto h = c.build_auth_headers(1700000000000LL);
    check(has(signed_msg, "1700000000000") && has(signed_msg, "GET") &&
              has(signed_msg, "/trade-api/ws/v2"),
          "signed message = timestamp + GET + ws_sign_path");
    check(h.size() == 3, "three auth headers");
    bool key = false, sig = false, ts = false;
    for (auto& [k, v] : h) {
      if (k == "KALSHI-ACCESS-KEY" && v == "key-1") key = true;
      if (k == "KALSHI-ACCESS-SIGNATURE" && v == "BASE64SIG") sig = true;
      if (k == "KALSHI-ACCESS-TIMESTAMP" && v == "1700000000000") ts = true;
    }
    check(key && sig && ts, "auth headers carry key/signature/timestamp");
  }

  // --- command builders ---
  {
    MockWebSocketTransport t;
    KalshiWsClient c(t, cfg(), [](std::string_view) { return std::string("s"); });
    const std::string sub = c.build_subscribe(1, {"MKT-A", "MKT-B"});
    check(has(sub, R"("cmd":"subscribe")") && has(sub, R"("channels":["orderbook_delta"])") &&
              has(sub, R"("market_tickers":["MKT-A","MKT-B"])") &&
              has(sub, R"("use_yes_price":false)"),
          "subscribe cmd: orderbook_delta + tickers + explicit use_yes_price:false");
    c.want_channels({"orderbook_delta", "trade", "ticker"});
    const std::string sub2 = c.build_subscribe(4, {"MKT-A"});
    check(has(sub2, R"("channels":["orderbook_delta","trade","ticker"])"),
          "subscribe cmd can include orderbook_delta + trade + ticker channels");
    check(has(c.build_unsubscribe(2, {7}), R"("cmd":"unsubscribe")") &&
              has(c.build_unsubscribe(2, {7}), R"("sids":[7])"),
          "unsubscribe cmd carries sids");
    check(has(c.build_get_snapshot(3, {7}, {"MKT-A"}), R"("action":"get_snapshot")") &&
              has(c.build_get_snapshot(3, {7}, {"MKT-A"}), R"("market_tickers":["MKT-A"])"),
          "get_snapshot cmd: action + tickers");
  }

  // --- routing: open -> subscribe sent; snapshot/deltas applied; control seq;
  //     gap -> resync; error 25; heartbeat liveness ---
  {
    MockWebSocketTransport t;
    RecResync rz;
    OrderBookManager books(&rz);
    KalshiWsClient c(t, cfg(), [](std::string_view) { return std::string("s"); });
    c.set_book_manager(&books);
    c.want_orderbook({"MKT-A"});
    c.start();
    check(t.is_open() && !t.sent().empty() && has(t.last_sent(), R"("cmd":"subscribe")"),
          "on open -> subscribe command sent");

    t.inject_text(R"({"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":7}})");
    t.inject_text(R"({"type":"orderbook_snapshot","sid":7,"seq":1,"msg":{"market_ticker":"MKT-A","yes_dollars_fp":[["0.4000","500.00"]],"no_dollars_fp":[["0.5500","100.00"]]}})");
    check(books.book(A) && books.book(A)->valid() && books.book(A)->best_yes_bid() == 4000,
          "snapshot applied: book valid, best yes bid 0.4000");

    t.inject_text(R"({"type":"orderbook_delta","sid":7,"seq":2,"msg":{"market_ticker":"MKT-A","price_dollars":"0.4000","delta_fp":"100.00","side":"yes"}})");
    check(books.book(A)->yes_size_at(4000) == trading::CountFp{60000}, "delta seq2 applied (500+100 contracts)");

    // control seq (ok) consumes seq 3; next delta seq 4 must not read as a gap.
    t.inject_text(R"({"id":9,"type":"ok","sid":7,"seq":3,"msg":{}})");
    t.inject_text(R"({"type":"orderbook_delta","sid":7,"seq":4,"msg":{"market_ticker":"MKT-A","price_dollars":"0.4000","delta_fp":"100.00","side":"yes"}})");
    check(rz.calls == 0 && books.book(A)->valid(), "control seq did not cause a false gap");

    // true gap (expected 5, got 20)
    t.inject_text(R"({"type":"orderbook_delta","sid":7,"seq":20,"msg":{"market_ticker":"MKT-A","price_dollars":"0.4000","delta_fp":"1.00","side":"yes"}})");
    check(rz.calls == 1 && !books.book(A)->valid(), "gap -> book invalid + resync requested");

    // error 25 (buffer overflow, I7)
    t.inject_text(R"({"id":0,"type":"error","msg":{"code":25,"msg":"overflow"}})");
    check(c.overflow_events() == 1, "error 25 counted as overflow event");

    // heartbeat liveness
    const std::int64_t before = c.last_activity_ms();
    t.inject_ping("heartbeat");
    check(c.last_activity_ms() >= before, "ping updates last-activity (liveness watchdog)");
  }

  // --- lifecycle determined -> book freed (I9) ---
  {
    MockWebSocketTransport t;
    OrderBookManager books;
    KalshiWsClient c(t, cfg(), [](std::string_view) { return std::string("s"); });
    c.set_book_manager(&books);
    c.start();
    t.inject_text(R"({"type":"orderbook_snapshot","sid":7,"seq":1,"msg":{"market_ticker":"MKT-A","yes_dollars_fp":[["0.4000","500.00"]],"no_dollars_fp":[]}})");
    check(books.book(A) && books.book(A)->valid(), "book established");
    t.inject_text(R"({"type":"market_lifecycle_v2","sid":7,"msg":{"market_ticker":"MKT-A","event_type":"determined"}})");
    check(books.book(A) == nullptr && c.lifecycle_deletes() == 1,
          "determined lifecycle frees the book");
  }

  // --- reconnect: drop -> close; reopen -> epoch bump + resubscribe ---
  {
    MockWebSocketTransport t;
    KalshiWsClient c(t, cfg(), [](std::string_view) { return std::string("s"); });
    c.want_orderbook({"MKT-A"});
    c.start();
    check(c.epoch() == 1 && c.reconnects() == 0, "first open: epoch 1, no reconnects");
    t.drop();                 // unexpected disconnect
    t.clear_sent();
    t.start();                // transport reconnects
    check(c.epoch() == 2 && c.reconnects() == 1, "reconnect: epoch bumped to 2");
    check(!t.sent().empty() && has(t.last_sent(), R"("cmd":"subscribe")"),
          "resubscribe sent after reconnect");
  }

  // --- reconnect RE-SIGNS auth: every reconnect attempt carries a fresh
  //     signature timestamp, not the stale startup one (2026-07-07 401-lockout
  //     capture gap: ixwebsocket replayed a since-expired signature and Kalshi
  //     401'd every reconnect until the hourly process restart). ---
  {
    MockWebSocketTransport t;
    std::int64_t fake_ms = 1'700'000'000'000LL;
    // Signer echoes the signed message so a replayed (stale) signature is
    // detectable, not just the timestamp header.
    KalshiWsClient c(t, cfg(), [](std::string_view m) -> std::optional<std::string> {
      return std::string("sig:") + std::string(m);
    });
    c.set_clock([&] { return fake_ms; });
    c.want_orderbook({"MKT-A"});
    c.start();
    const std::string ts0 = header_val(t.headers(), "KALSHI-ACCESS-TIMESTAMP");
    const std::string sig0 = header_val(t.headers(), "KALSHI-ACCESS-SIGNATURE");
    check(ts0 == "1700000000000", "initial handshake signed at clock T0");

    // Open -> Close (dropped after open): re-sign before the transport reconnects.
    fake_ms += 5'000;  // 5s later
    t.drop();
    const std::string ts1 = header_val(t.headers(), "KALSHI-ACCESS-TIMESTAMP");
    check(!ts0.empty() && !ts1.empty() && std::stoll(ts1) > std::stoll(ts0),
          "on close: next reconnect handshake carries a NEWER signature timestamp");
    check(header_val(t.headers(), "KALSHI-ACCESS-SIGNATURE") != sig0,
          "on close: signature is actually re-signed, not replayed");

    // Replay the REAL 401 handshake rejection: Error, NO open/close, then retry.
    // Without re-signing here the stale signature repeats forever (the lockout).
    fake_ms += 5'000;
    t.inject_error("Expecting status 101 (Switching Protocol), got 401", 401);
    const std::string ts2 = header_val(t.headers(), "KALSHI-ACCESS-TIMESTAMP");
    check(!ts2.empty() && std::stoll(ts2) > std::stoll(ts1),
          "on 401 handshake error: next attempt re-signed with a NEWER timestamp");

    // Recovery: the retried (now-fresh) handshake succeeds -> resubscribe.
    t.clear_sent();
    t.start();
    check(t.is_open() && !t.sent().empty() && has(t.last_sent(), R"("cmd":"subscribe")"),
          "after re-signed reconnect the stream recovers and resubscribes");
  }

  // --- W-C1 force-reconnect watchdog: a silently-wedged (half-open) socket
  //     delivers no Close/Error, so ixwebsocket's auto-reconnect never fires and
  //     capture stays dead until the hourly respawn (the 2026-07-07 top-of-hour
  //     gap). The ws_shadow watchdog detects inbound silence and FORCES a
  //     stop()+start() carrying a fresh signature; the stream then resumes.
  //     RED-FIRST: with force_reconnect() reduced to a no-op (no stop()+start())
  //     these asserts fail — silence never recovers. ---
  {
    MockWebSocketTransport t;
    std::int64_t fake_ms = 1'700'000'000'000LL;
    KalshiWsClient c(t, cfg(), [](std::string_view m) -> std::optional<std::string> {
      return std::string("sig:") + std::string(m);
    });
    c.set_clock([&] { return fake_ms; });
    c.want_orderbook({"MKT-A"});
    c.start();
    const std::string ts0 = header_val(t.headers(), "KALSHI-ACCESS-TIMESTAMP");

    // The stream is alive: an inbound frame stamps last-activity.
    t.inject_text(R"({"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":7}})");
    const std::int64_t alive_ms = c.last_activity_ms();
    check(alive_ms != 0, "inbound frame recorded last-activity");

    // The socket wedges: no further frames. The watchdog liveness check trips
    // past the force-reconnect timeout, and nothing has recovered on its own.
    check(c.ping_silent(alive_ms + 25'000, 20'000),
          "20s+ inbound silence detected as a wedged socket");
    check(c.reconnects() == 0 && c.forced_reconnects() == 0,
          "before recovery nothing has reconnected (ixwebsocket is inert on a wedge)");

    // FORCE recovery — exactly what the ws_shadow watchdog calls on silence.
    fake_ms += 25'000;  // wall clock advanced while the socket was dead
    t.clear_sent();
    c.force_reconnect();

    const std::string ts1 = header_val(t.headers(), "KALSHI-ACCESS-TIMESTAMP");
    check(!ts0.empty() && !ts1.empty() && std::stoll(ts1) > std::stoll(ts0),
          "forced reconnect handshake carries a NEWER signature timestamp");
    check(t.is_open(), "forced reconnect reopened the transport");
    check(c.forced_reconnects() == 1 && c.reconnects() == 1 && c.epoch() == 2,
          "forced reconnect counts a reconnect + bumps the stream epoch");
    check(!t.sent().empty() && has(t.last_sent(), R"("cmd":"subscribe")"),
          "forced reconnect resubscribes");

    // The stream RESUMES: a post-reconnect frame flows and refreshes liveness.
    t.inject_text(R"({"id":2,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":8}})");
    check(!c.ping_silent(c.last_activity_ms(), 20'000),
          "after the forced reconnect the stream is live again (silence cleared)");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
