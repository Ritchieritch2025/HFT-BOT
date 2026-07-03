// Example: connect to Kalshi, warm the connection pool, check exchange
// status, and fetch the portfolio balance.
//
//   export KALSHI_API_KEY_ID="<your key id>"
//   export KALSHI_PRIVATE_KEY_PATH="$HOME/.kalshi/private_key.pem"
//   ./build/kalshi_example

#include "kalshi/client.hpp"
#include "simdjson.h"

#include <cstdlib>
#include <iostream>

int main() {
  const char* key_id = std::getenv("KALSHI_API_KEY_ID");
  const char* key_path = std::getenv("KALSHI_PRIVATE_KEY_PATH");
  if (!key_id || !key_path) {
    std::cerr << "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n";
    return 2;
  }

  kalshi::Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = kalshi::read_file(key_path);
  cfg.pool_size = 4;
  kalshi::KalshiClient client(std::move(cfg));

  std::cout << "warmed connections: " << client.warmup() << "\n";

  simdjson::ondemand::parser parser;

  auto status = client.request(kalshi::Method::Get, "/exchange/status");
  if (!status) {
    std::cerr << "transport error: " << status.error().message << "\n";
    return 1;
  }
  std::cout << "GET /exchange/status -> " << status->status << " in "
            << status->total_time_us << "us\n";
  if (status->ok()) {
    simdjson::padded_string json(status->body);
    auto doc = parser.iterate(json);
    bool trading_active = doc["trading_active"];
    std::cout << "trading_active: " << (trading_active ? "yes" : "no") << "\n";
  }

  auto balance = client.request(kalshi::Method::Get, "/portfolio/balance");
  if (!balance) {
    std::cerr << "transport error: " << balance.error().message << "\n";
    return 1;
  }
  std::cout << "GET /portfolio/balance -> " << balance->status << " in "
            << balance->total_time_us << "us\n";
  if (balance->ok()) {
    simdjson::padded_string json(balance->body);
    auto doc = parser.iterate(json);
    int64_t cents = doc["balance"];
    std::cout << "balance: " << cents << " cents\n";
  } else {
    std::cout << "body: " << balance->body << "\n";
  }

  // Placing an order looks like this (Post is signed the same way):
  //   client.request(kalshi::Method::Post, "/portfolio/orders",
  //                  R"({"ticker":"...","action":"buy","side":"yes",
  //                      "count":1,"type":"limit","yes_price":50,
  //                      "client_order_id":"..."})");
  return 0;
}
