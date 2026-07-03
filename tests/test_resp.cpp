// RESP client + wire-format unit test, run against tests/mini_redis.py
// (or a real Redis). Usage: test_resp <port>

#include "kalshi/resp.hpp"
#include "kalshi/wire.hpp"

#include <chrono>
#include <iostream>
#include <string>
#include <thread>

namespace {

int g_failures = 0;

void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    std::cerr << "usage: test_resp <port>\n";
    return 2;
  }
  const int port = std::stoi(argv[1]);
  kalshi::resp::RespClient redis("127.0.0.1", port);

  check(redis.ping().has_value(), "PING/PONG");
  (void)redis.command({"DEL", "t:list"});

  // Binary safety: payload containing NULs and CRLFs must survive verbatim.
  std::string binary("\x00\x01\r\n\xff\x00 payload", 17);
  auto n = redis.lpush("t:list", binary);
  check(n.has_value() && *n == 1, "LPUSH binary payload");
  auto popped = redis.brpop("t:list", 1);
  check(popped.has_value() && popped->has_value() && **popped == binary,
        "BRPOP returns identical binary bytes");

  // Empty-list BRPOP times out with nullopt, not an error.
  auto empty = redis.brpop("t:list", 1);
  check(empty.has_value() && !empty->has_value(), "BRPOP timeout -> nullopt");

  // ExecPayload roundtrip through the list.
  kalshi::wire::ExecPayload out;
  out.strategy_id = 7;
  out.action = kalshi::wire::kActionBuy;
  out.side = kalshi::wire::kSideYes;
  out.order_type = kalshi::wire::kTypeLimit;
  out.count = 3;
  out.price_cents = 42;
  out.seq = 12345;
  out.ts_ns = 1'700'000'000'000'000'000ULL;
  out.ttl_ns = 500'000'000;
  out.set_ticker("TEST-MKT-A");
  check(redis.lpush("t:list", kalshi::wire::as_bytes(out)).has_value(),
        "LPUSH ExecPayload");
  auto in_bytes = redis.brpop("t:list", 1);
  check(in_bytes.has_value() && in_bytes->has_value(), "BRPOP ExecPayload");
  if (in_bytes && *in_bytes) {
    const char* why = nullptr;
    const auto* in = kalshi::wire::decode_exec(**in_bytes, &why);
    check(in != nullptr, std::string("decode_exec ok") + (why ? why : ""));
    if (in) {
      check(in->strategy_id == 7 && in->seq == 12345 && in->price_cents == 42 &&
                in->ticker_view() == "TEST-MKT-A",
            "ExecPayload fields survive the queue");
    }
  }

  // Malformed frames are rejected, not crashed on.
  const char* why = nullptr;
  check(kalshi::wire::decode_exec("garbage", &why) == nullptr,
        "decode_exec rejects short frame");
  kalshi::wire::ExecPayload bad = out;
  bad.price_cents = 0;
  check(kalshi::wire::decode_exec(kalshi::wire::as_bytes(bad), &why) == nullptr,
        "decode_exec rejects limit order with price 0");

  // client_order_id: deterministic, UUID-shaped, distinct across strategies.
  const std::string cid1 = kalshi::wire::client_order_id(out);
  const std::string cid2 = kalshi::wire::client_order_id(out);
  kalshi::wire::ExecPayload other = out;
  other.strategy_id = 8;
  check(cid1 == cid2, "client_order_id deterministic");
  check(cid1.size() == 36 && cid1[8] == '-' && cid1[13] == '-' &&
            cid1[18] == '-' && cid1[23] == '-',
        "client_order_id is UUID-shaped");
  check(kalshi::wire::client_order_id(other) != cid1,
        "client_order_id distinct per strategy");

  // Pub/sub: subscriber sees exactly the published MarketEvent bytes.
  kalshi::resp::RespClient sub("127.0.0.1", port);
  check(sub.connect().has_value() && sub.subscribe("t:events").has_value(),
        "SUBSCRIBE");
  std::thread publisher([&] {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
    kalshi::resp::RespClient pub_conn("127.0.0.1", port);
    kalshi::wire::MarketEvent ev;
    ev.set_ticker("TEST-MKT-B");
    ev.yes_bid = 33;
    ev.yes_ask = 41;
    ev.ts_ns = 1;
    ev.seq = 1;
    (void)pub_conn.publish("t:events", kalshi::wire::as_bytes(ev));
  });
  auto msg = sub.next_message(3000);
  publisher.join();
  check(msg.has_value() && msg->has_value(), "subscriber receives message");
  if (msg && *msg) {
    const auto* ev = kalshi::wire::decode_event(**msg);
    check(ev && ev->ticker_view() == "TEST-MKT-B" && ev->yes_ask == 41,
          "MarketEvent fields survive pub/sub");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
