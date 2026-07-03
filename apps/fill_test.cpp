// One explicit live-fill test: buy 1 YES contract at the given limit price,
// immediate-or-cancel (fills now or cancels — never rests), then read back
// the resulting position and balance. The position is NOT exited — that is
// the operator's manual step.
//
//   fill_test TICKER PRICE_CENTS [--prod-ok]
//
// Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_BASE_URL

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/wire.hpp"
#include "simdjson.h"

#include <cstdio>
#include <cstring>
#include <string>

using namespace kalshi;
using daemon::steady_now_ns;

int main(int argc, char** argv) {
  std::string ticker;
  int price_cents = 0;
  bool prod_ok = false;
  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "--prod-ok") == 0) prod_ok = true;
    else if (ticker.empty()) ticker = argv[i];
    else price_cents = std::atoi(argv[i]);
  }
  if (ticker.empty() || price_cents < 1 || price_cents > 99) {
    std::fprintf(stderr, "usage: fill_test TICKER PRICE_CENTS [--prod-ok]\n");
    return 2;
  }
  const char* key_id = std::getenv("KALSHI_API_KEY_ID");
  const char* key_path = std::getenv("KALSHI_PRIVATE_KEY_PATH");
  if (!key_id || !key_path) {
    std::fprintf(stderr, "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n");
    return 2;
  }

  Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = daemon::env_or("KALSHI_BASE_URL", "https://api.elections.kalshi.com");
  cfg.pool_size = 1;
  if (cfg.base_url.find("demo") == std::string::npos && !prod_ok) {
    std::fprintf(stderr, "target is production — pass --prod-ok to spend up to %dc\n",
                 price_cents);
    return 2;
  }
  KalshiClient client(std::move(cfg));
  simdjson::ondemand::parser parser;
  auto lane = client.make_lane();
  (void)lane.ping();

  // Identity for a deterministic client_order_id.
  wire::ExecPayload p;
  p.action = wire::kActionBuy;
  p.side = wire::kSideYes;
  p.order_type = wire::kTypeLimit;
  p.count = 1;
  p.price_cents = price_cents;
  p.strategy_id = 0;
  p.seq = 1;
  p.ts_ns = daemon::now_ns();
  p.set_ticker(ticker);

  char price[16];
  std::snprintf(price, sizeof(price), "0.%02d00", price_cents);
  std::string body;
  body.reserve(256);
  body += R"({"ticker":")";
  body += ticker;
  body += R"(","side":"bid","count":"1","price":")";
  body += price;
  body += R"(","time_in_force":"immediate_or_cancel","self_trade_prevention_type":"taker_at_cross","client_order_id":")";
  body += wire::client_order_id(p);
  body += R"("})";
  std::printf("placing: buy 1 YES %s @ %dc (IOC)\n", ticker.c_str(), price_cents);

  const std::uint64_t t0 = steady_now_ns();
  auto req = client.sign_request(Method::Post, wire::kCreateOrderPath, body);
  if (!req) {
    std::printf("sign failed: %s\n", req.error().message.c_str());
    return 1;
  }
  const std::uint64_t t1 = steady_now_ns();
  auto placed = lane.send(*req);
  const std::uint64_t t2 = steady_now_ns();
  if (!placed) {
    std::printf("transport: %s\n", placed.error().message.c_str());
    return 1;
  }
  std::printf("HTTP %ld in %.1fms (sign %.1fms): %s\n", placed->status,
              static_cast<double>(t2 - t1) / 1e6,
              static_cast<double>(t1 - t0) / 1e6, placed->body.c_str());
  if (placed->status != 201 && placed->status != 200) return 1;

  std::string fill_count = "?";
  try {
    simdjson::padded_string j(placed->body);
    auto doc = parser.iterate(j);
    std::string_view v;
    if (doc["fill_count"].get(v) == simdjson::SUCCESS) fill_count = std::string(v);
    else if (doc["order"]["fill_count"].get(v) == simdjson::SUCCESS) fill_count = std::string(v);
  } catch (...) {}
  std::printf("fill_count: %s\n", fill_count.c_str());

  auto pos = client.request(Method::Get, "/portfolio/positions?ticker=" + ticker);
  if (pos && pos->ok())
    std::printf("position:  %s\n", pos->body.c_str());
  auto bal = client.request(Method::Get, "/portfolio/balance");
  if (bal && bal->ok())
    std::printf("balance:   %s\n", bal->body.c_str());

  std::printf("NOTE: any filled contracts are now a live position — exit manually.\n");
  return 0;
}
