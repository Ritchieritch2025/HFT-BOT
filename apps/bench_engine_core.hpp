#pragma once
//
// W-LAT-BENCH-01 Tier 1 measurement core: times the local software order
// path — ExecPayload build, wire::order_json serialization, and
// KalshiClient::sign_request (compose + RSA-PSS sign, the exact call
// bench_order/tradingd make before transmitting) — plus the whole chain.
// Pure CPU, no network. Shared by apps/bench_engine.cpp (full n=10k run)
// and tests/test_engine_bench.cpp (small-n smoke inside `make check`).

#include "kalshi/client.hpp"
#include "kalshi/wire.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

namespace kalshi::bench {

struct Dist {
  std::string metric;
  std::size_t n = 0;
  double min_us = 0, p50_us = 0, p90_us = 0, p99_us = 0, max_us = 0;
};

inline std::uint64_t now_ns() {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

inline Dist make_dist(std::string metric, std::vector<std::uint64_t>& ns) {
  Dist d;
  d.metric = std::move(metric);
  d.n = ns.size();
  if (ns.empty()) return d;
  std::sort(ns.begin(), ns.end());
  const auto pc = [&](double p) {
    return static_cast<double>(
               ns[std::min(ns.size() - 1,
                           static_cast<std::size_t>(p * static_cast<double>(ns.size())))]) /
           1000.0;
  };
  d.min_us = static_cast<double>(ns.front()) / 1000.0;
  d.p50_us = pc(0.50);
  d.p90_us = pc(0.90);
  d.p99_us = pc(0.99);
  d.max_us = static_cast<double>(ns.back()) / 1000.0;
  return d;
}

// A representative resting maker order (post-only 1c YES bid, 42-char-max
// ticker) — same shape bench_order transmits.
inline wire::ExecPayload bench_payload(std::uint64_t seq) {
  wire::ExecPayload p;
  p.action = wire::kActionBuy;
  p.side = wire::kSideYes;
  p.order_type = wire::kTypeLimit;
  p.count = 1;
  p.price_cents = 1;
  p.strategy_id = 0;
  p.seq = seq;
  p.ts_ns = now_ns();
  p.set_ticker("KXBENCHMARK-26JUL16-T1");
  return p;
}

// Runs each stage n times. `client` only needs a parsed key (no network).
// Returns {payload_build, order_json, sign_request, chain_build_to_signed}
// or an empty vector if any signing call fails.
inline std::vector<Dist> run_engine_bench(KalshiClient& client, int n) {
  std::vector<std::uint64_t> t_payload, t_json, t_sign, t_chain;
  t_payload.reserve(static_cast<std::size_t>(n));
  t_json.reserve(static_cast<std::size_t>(n));
  t_sign.reserve(static_cast<std::size_t>(n));
  t_chain.reserve(static_cast<std::size_t>(n));
  std::size_t sink = 0;  // defeats dead-code elimination

  for (int i = 0; i < n; ++i) {
    const auto seq = static_cast<std::uint64_t>(i) + 1;

    std::uint64_t t0 = now_ns();
    wire::ExecPayload p = bench_payload(seq);
    std::uint64_t t1 = now_ns();
    t_payload.push_back(t1 - t0);
    sink += static_cast<std::size_t>(p.ticker[0]);

    t0 = now_ns();
    const std::string body = wire::order_json(p, /*post_only=*/true);
    t1 = now_ns();
    t_json.push_back(t1 - t0);
    sink += body.size();

    t0 = now_ns();
    auto req = client.sign_request(Method::Post, wire::kCreateOrderPath, body);
    t1 = now_ns();
    if (!req) return {};
    t_sign.push_back(t1 - t0);
    sink += req->sig_header.size();

    // Whole chain in one shot: order intent -> wire-ready signed request.
    t0 = now_ns();
    wire::ExecPayload p2 = bench_payload(seq);
    const std::string body2 = wire::order_json(p2, /*post_only=*/true);
    auto req2 = client.sign_request(Method::Post, wire::kCreateOrderPath, body2);
    t1 = now_ns();
    if (!req2) return {};
    t_chain.push_back(t1 - t0);
    sink += req2->body.size();
  }
  if (sink == 0) return {};  // impossible; keeps `sink` observable

  std::vector<Dist> out;
  out.push_back(make_dist("payload_build", t_payload));
  out.push_back(make_dist("order_json", t_json));
  out.push_back(make_dist("sign_request", t_sign));
  out.push_back(make_dist("chain_build_to_signed", t_chain));
  return out;
}

}  // namespace kalshi::bench
