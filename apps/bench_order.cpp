// Order-execution speed test: N rounds of place-and-cancel over one dedicated
// warm lane, timing every stage (RSA sign, place, cancel). Orders are
// post-only 1c YES bids — by construction they rest or are rejected, never
// fill — and every order is canceled in the same iteration.
//
//   bench_order [n=5] [ticker] [--prod-ok]
//
// Auto-picks an open market when no ticker is given. Refuses production
// without --prod-ok. Run the same command on every candidate deployment box
// to compare (results are RTT-dominated).
//
// Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_BASE_URL

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/wire.hpp"
#include "simdjson.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using namespace kalshi;
using daemon::steady_now_ns;

namespace {

double pct(std::vector<long long> v, double p) {
  std::sort(v.begin(), v.end());
  if (v.empty()) return 0;
  return static_cast<double>(
             v[std::min(v.size() - 1, static_cast<size_t>(p * static_cast<double>(v.size())))]) /
         1000.0;
}

std::string pick_ticker(KalshiClient& client, simdjson::ondemand::parser& parser) {
  auto resp = client.request(Method::Get, "/markets?status=open&limit=200");
  if (!resp || !resp->ok()) return {};
  try {
    simdjson::padded_string j(resp->body);
    auto doc = parser.iterate(j);
    for (auto m : doc["markets"].get_array()) {
      std::string_view t;
      if (m["ticker"].get(t) == simdjson::SUCCESS && !t.empty() && t.size() <= 41)
        return std::string(t);
    }
  } catch (...) {}
  return {};
}

}  // namespace

int main(int argc, char** argv) {
  int n = 5;
  std::string ticker;
  for (int i = 1; i < argc; ++i) {
    if (std::strcmp(argv[i], "--prod-ok") == 0) continue;  // superseded by env safety
    else if (std::atoi(argv[i]) > 0 && ticker.empty() && std::string(argv[i]).find_first_not_of("0123456789") == std::string::npos)
      n = std::atoi(argv[i]);
    else ticker = argv[i];
  }

  const char* key_id = std::getenv("KALSHI_API_KEY_ID");
  const char* key_path = std::getenv("KALSHI_PRIVATE_KEY_PATH");
  if (!key_id || !key_path) {
    std::fprintf(stderr, "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n");
    return 2;
  }

  // Environment + base_url resolved and cross-validated by the safety layer.
  kalshi::Runtime rt;
  try {
    rt = kalshi::resolve_runtime();
    // bench_order transmits real (post-only) orders -> require live explicitly.
    kalshi::require_orders_allowed(rt);
  } catch (const kalshi::SafetyViolation& e) {
    std::fprintf(stderr, "bench_order refused: %s\n", e.what());
    return 2;
  }

  Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = rt.rest_base_url;
  cfg.pool_size = 1;
  KalshiClient client(std::move(cfg));
  simdjson::ondemand::parser parser;

  if (ticker.empty()) ticker = pick_ticker(client, parser);
  if (ticker.empty()) {
    std::fprintf(stderr, "no suitable market found; pass a ticker\n");
    return 1;
  }
  std::printf("target: %s  market: %s  rounds: %d\n",
              client.config().base_url.c_str(), ticker.c_str(), n);

  auto lane = client.make_lane();
  auto warm = lane.ping();
  std::printf("lane: %s\n", (warm && warm->ok()) ? "warm" : "warmup failed");

  std::vector<long long> sign_us, place_us, cancel_us;
  int placed_n = 0, canceled_n = 0, rejected_n = 0;

  for (int i = 0; i < n; ++i) {
    wire::ExecPayload p;
    p.action = wire::kActionBuy;
    p.side = wire::kSideYes;
    p.order_type = wire::kTypeLimit;
    p.count = 1;
    p.price_cents = 1;
    p.strategy_id = 0;
    p.seq = static_cast<std::uint64_t>(i) + 1;
    p.ts_ns = daemon::now_ns();
    p.set_ticker(ticker);
    const std::string body = wire::order_json(p, /*post_only=*/true);

    const std::uint64_t t0 = steady_now_ns();
    auto req = client.sign_request(Method::Post, wire::kCreateOrderPath, body);
    const std::uint64_t t1 = steady_now_ns();
    if (!req) {
      std::printf("round %d: sign failed: %s\n", i, req.error().message.c_str());
      continue;
    }
    auto placed = lane.send(*req);
    const std::uint64_t t2 = steady_now_ns();
    if (!placed) {
      std::printf("round %d: transport: %s\n", i, placed.error().message.c_str());
      continue;
    }
    if (placed->status != 201 && placed->status != 200) {
      ++rejected_n;
      std::printf("round %d: HTTP %ld: %.200s\n", i, placed->status,
                  placed->body.c_str());
      continue;
    }
    std::string order_id;
    try {
      simdjson::padded_string j(placed->body);
      auto doc = parser.iterate(j);
      std::string_view id;
      if (doc["order_id"].get(id) == simdjson::SUCCESS) order_id = std::string(id);
      else if (doc["order"]["order_id"].get(id) == simdjson::SUCCESS) order_id = std::string(id);
    } catch (...) {}
    ++placed_n;
    sign_us.push_back(static_cast<long long>((t1 - t0) / 1000));
    place_us.push_back(static_cast<long long>((t2 - t1) / 1000));

    if (!order_id.empty()) {
      const std::uint64_t t3 = steady_now_ns();
      auto creq = client.sign_request(
          Method::Delete, std::string(wire::kCreateOrderPath) + "/" + order_id);
      auto cancel = creq ? lane.send(*creq)
                         : std::expected<Response, Error>(std::unexpected(creq.error()));
      const std::uint64_t t4 = steady_now_ns();
      if (cancel && cancel->ok()) {
        ++canceled_n;
        cancel_us.push_back(static_cast<long long>((t4 - t3) / 1000));
      } else {
        std::printf("round %d: CANCEL FAILED for %s: %s\n", i, order_id.c_str(),
                    cancel ? ("HTTP " + std::to_string(cancel->status) + " " + cancel->body).c_str()
                           : cancel.error().message.c_str());
      }
    }
  }

  std::printf("\nplaced=%d canceled=%d rejected=%d of %d rounds\n", placed_n,
              canceled_n, rejected_n, n);
  if (!sign_us.empty()) {
    std::printf("RSA-PSS sign:        p50=%.1fms min=%.1fms max=%.1fms\n",
                pct(sign_us, .5), pct(sign_us, 0), pct(sign_us, .999));
    std::printf("place (wire->ack):   p50=%.1fms min=%.1fms max=%.1fms\n",
                pct(place_us, .5), pct(place_us, 0), pct(place_us, .999));
    std::printf("cancel (wire->ack):  p50=%.1fms min=%.1fms max=%.1fms\n",
                pct(cancel_us, .5), pct(cancel_us, 0), pct(cancel_us, .999));
    std::printf("decision->order-ack: p50=%.1fms  (sign + place on a warm lane)\n",
                pct(sign_us, .5) + pct(place_us, .5));
  }
  const bool ok = placed_n > 0 && canceled_n == placed_n;
  std::printf("%s\n", ok ? "ORDER EXECUTION PASS" : "ORDER EXECUTION FAIL");
  return ok ? 0 : 1;
}
