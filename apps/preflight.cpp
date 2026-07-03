// Preflight: validate credentials, market-data logic, and order execution
// end-to-end before letting strategies trade.
//
//   ./build/preflight                      read-only checks (safe anywhere)
//   ./build/preflight --order TICKER       + place 1 contract YES @ 1c and
//                                            immediately cancel it
//
// The order check uses the exact code path tradingd uses (ExecPayload →
// wire::order_json → sign_request → send). It refuses to run against
// production unless --prod-ok is also given; point KALSHI_BASE_URL at the
// demo environment (https://demo-api.kalshi.co) for risk-free validation.
//
// Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH  (required)
//      KALSHI_BASE_URL   (default https://api.elections.kalshi.com)

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/wire.hpp"
#include "simdjson.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

using namespace kalshi;

namespace {

int g_failures = 0;

void result(bool ok, const std::string& what, const std::string& detail = {}) {
  std::printf("%s  %s%s%s\n", ok ? "PASS" : "FAIL", what.c_str(),
              detail.empty() ? "" : " — ", detail.c_str());
  if (!ok) ++g_failures;
}

long long now_ms() {
  return static_cast<long long>(daemon::now_ns() / 1'000'000ULL);
}

}  // namespace

int main(int argc, char** argv) {
  const char* key_id = std::getenv("KALSHI_API_KEY_ID");
  const char* key_path = std::getenv("KALSHI_PRIVATE_KEY_PATH");
  if (!key_id || !key_path) {
    std::fprintf(stderr,
                 "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH first\n"
                 "(key file: the RSA private key .pem Kalshi gave you when you "
                 "created the API key)\n");
    return 2;
  }
  std::string order_ticker;
  bool prod_ok = false;
  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "--order") == 0 && i + 1 < argc) order_ticker = argv[++i];
    else if (std::strcmp(argv[i], "--prod-ok") == 0) prod_ok = true;
  }

  // 1. Key parses and the client constructs.
  Config cfg;
  cfg.api_key_id = key_id;
  cfg.base_url = daemon::env_or("KALSHI_BASE_URL", "https://api.elections.kalshi.com");
  cfg.pool_size = 2;
  try {
    cfg.private_key_pem = read_file(key_path);
    // constructed below; separate try so each failure reads clearly
  } catch (const std::exception& e) {
    result(false, "read private key file", e.what());
    return 1;
  }
  std::printf("target: %s\n", cfg.base_url.c_str());
  const bool is_demo = cfg.base_url.find("demo") != std::string::npos;

  KalshiClient* client_ptr = nullptr;
  try {
    static KalshiClient client(std::move(cfg));
    client_ptr = &client;
    result(true, "RSA private key loaded, client constructed");
  } catch (const std::exception& e) {
    result(false, "construct client (bad key?)", e.what());
    return 1;
  }
  KalshiClient& client = *client_ptr;
  simdjson::ondemand::parser parser;

  // 2. Exchange reachable, signed headers accepted on a public endpoint.
  auto status = client.request(Method::Get, "/exchange/status");
  if (!status) {
    result(false, "GET /exchange/status", status.error().message);
    return 1;
  }
  result(status->ok(), "GET /exchange/status",
         "HTTP " + std::to_string(status->status) + " in " +
             std::to_string(status->total_time_us / 1000) + "ms");

  // 3. Clock skew (the classic silent 401 cause).
  if (status->server_date_ms > 0) {
    const long long skew = now_ms() - status->server_date_ms;
    result(skew > -2000 && skew < 2000, "clock skew vs exchange",
           std::to_string(skew) + "ms (must stay within a few seconds; run NTP)");
  }

  // 4. THE auth check: balance requires a valid key + signature.
  auto balance = client.request(Method::Get, "/portfolio/balance");
  if (!balance) {
    result(false, "GET /portfolio/balance", balance.error().message);
  } else if (balance->ok()) {
    std::string detail = balance->body;
    try {
      simdjson::padded_string j(balance->body);
      auto doc = parser.iterate(j);
      detail = std::to_string(int64_t(doc["balance"].get_int64())) + " cents available";
    } catch (...) {}
    result(true, "API key authenticated (GET /portfolio/balance)", detail);
  } else {
    result(false, "API key REJECTED (GET /portfolio/balance)",
           "HTTP " + std::to_string(balance->status) + ": " + balance->body +
               (balance->status == 401
                    ? "  [check: key id matches the .pem, key created for this "
                      "environment (prod keys don't work on demo), clock skew]"
                    : ""));
  }

  // 5. Market-data logic: fetch and parse live top-of-book.
  auto markets = client.request(Method::Get, "/markets?status=open&limit=3");
  if (markets && markets->ok()) {
    std::string seen;
    int n = 0;
    try {
      simdjson::padded_string j(markets->body);
      auto doc = parser.iterate(j);
      for (auto m : doc["markets"].get_array()) {
        std::string_view t;
        if (m["ticker"].get(t) != simdjson::SUCCESS) continue;
        std::int64_t bid = -1, ask = -1;
        (void)m["yes_bid"].get(bid);
        (void)m["yes_ask"].get(ask);
        seen += std::string(t) + " (" + std::to_string(bid) + "/" +
                std::to_string(ask) + "c) ";
        ++n;
      }
    } catch (const simdjson::simdjson_error& e) {
      result(false, "parse /markets", e.what());
      n = -1;
    }
    if (n >= 0)
      result(n > 0, "market data parses (GET /markets)", seen);
  } else {
    result(false, "GET /markets",
           markets ? "HTTP " + std::to_string(markets->status)
                   : markets.error().message);
  }

  // 6. Optional: real order round trip (place @1c, then cancel).
  if (!order_ticker.empty()) {
    if (!is_demo && !prod_ok) {
      result(false, "order check refused",
             "target is production; use the demo env or pass --prod-ok "
             "(max exposure: 1 contract at 1c)");
    } else {
      wire::ExecPayload p;  // the same struct + JSON path tradingd uses
      p.action = wire::kActionBuy;
      p.side = wire::kSideYes;
      p.order_type = wire::kTypeLimit;
      p.count = 1;
      p.price_cents = 1;
      p.seq = 1;
      p.ts_ns = daemon::now_ns();
      p.strategy_id = 0;
      p.set_ticker(order_ticker);
      const char* why = nullptr;
      if (!wire::decode_exec(wire::as_bytes(p), &why)) {
        result(false, "compose order payload", why);
      } else {
        auto placed = client.request(Method::Post, "/portfolio/orders",
                                     wire::order_json(p));
        if (!placed) {
          result(false, "POST /portfolio/orders", placed.error().message);
        } else if (placed->status != 201 && placed->status != 200) {
          result(false, "POST /portfolio/orders",
                 "HTTP " + std::to_string(placed->status) + ": " + placed->body);
        } else {
          std::string order_id;
          std::string order_status;
          try {
            simdjson::padded_string j(placed->body);
            auto doc = parser.iterate(j);
            order_id = std::string(std::string_view(doc["order"]["order_id"].get_string()));
            order_status = std::string(std::string_view(doc["order"]["status"].get_string()));
          } catch (...) {}
          result(!order_id.empty(), "order placed (1 YES @ 1c on " + order_ticker + ")",
                 "id=" + order_id + " status=" + order_status + " in " +
                     std::to_string(placed->total_time_us / 1000) + "ms");
          if (!order_id.empty() && order_status != "executed") {
            auto cancel = client.request(Method::Delete,
                                         "/portfolio/orders/" + order_id);
            result(cancel && cancel->ok(), "order canceled",
                   cancel ? "HTTP " + std::to_string(cancel->status)
                          : cancel.error().message);
          } else if (order_status == "executed") {
            std::printf("note: order filled immediately (cost <= 1c) — no cancel needed\n");
          }
        }
      }
    }
  } else {
    std::printf("(order execution not tested — rerun with --order TICKER "
                "against the demo env for a free place+cancel round trip)\n");
  }

  std::printf("%s\n", g_failures == 0 ? "PREFLIGHT PASS" : "PREFLIGHT FAIL");
  return g_failures == 0 ? 0 : 1;
}
