// Threaded integration test against tests/mock_server.py.
//
// Usage: test_integration <base_url> <key.pem> <threads> <requests_per_thread> [pool_size]
//
// Hammers the client from many threads with mixed GET/POST traffic, asserts
// every request gets HTTP 200 and the expected body, and prints latency
// percentiles. Built with -fsanitize=thread in the harness to check the
// connection pool and signer for data races.

#include "kalshi/client.hpp"

#include <algorithm>
#include <atomic>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

int main(int argc, char** argv) {
  if (argc < 5) {
    std::cerr << "usage: test_integration <base_url> <key.pem> <threads> "
                 "<requests_per_thread> [pool_size]\n";
    return 2;
  }
  const std::string base_url = argv[1];
  const int n_threads = std::stoi(argv[3]);
  const int n_requests = std::stoi(argv[4]);
  const int pool_size = argc > 5 ? std::stoi(argv[5]) : 4;

  kalshi::Config cfg;
  cfg.api_key_id = "itest-key-id";
  cfg.private_key_pem = kalshi::read_file(argv[2]);
  cfg.base_url = base_url;
  cfg.pool_size = pool_size;
  cfg.request_timeout_ms = 10000;
  kalshi::KalshiClient client(std::move(cfg));

  std::atomic<int> ok{0}, bad_status{0}, transport_err{0}, bad_body{0};
  std::vector<std::vector<long long>> latencies(static_cast<size_t>(n_threads));

  auto worker = [&](int tid) {
    auto& lat = latencies[static_cast<size_t>(tid)];
    lat.reserve(static_cast<size_t>(n_requests));
    for (int i = 0; i < n_requests; ++i) {
      std::expected<kalshi::Response, kalshi::Error> r;
      if (i % 2 == 0) {
        r = client.request(kalshi::Method::Get, "/markets?limit=5&t=" + std::to_string(tid));
      } else {
        const std::string body =
            R"({"action":"buy","count":1,"side":"yes","ticker":"TEST-)" +
            std::to_string(tid) + "-" + std::to_string(i) + R"("})";
        r = client.request(kalshi::Method::Post, "/portfolio/orders", body);
      }
      if (!r) {
        ++transport_err;
        continue;
      }
      lat.push_back(r->total_time_us);
      if (r->status != 200) {
        ++bad_status;
      } else if (r->body.find("\"ok\":true") == std::string::npos) {
        ++bad_body;
      } else {
        ++ok;
      }
    }
  };

  std::vector<std::thread> threads;
  threads.reserve(static_cast<size_t>(n_threads));
  for (int t = 0; t < n_threads; ++t) threads.emplace_back(worker, t);
  for (auto& t : threads) t.join();

  std::vector<long long> all;
  for (auto& v : latencies) all.insert(all.end(), v.begin(), v.end());
  std::sort(all.begin(), all.end());
  auto pct = [&](double p) -> long long {
    if (all.empty()) return 0;
    return all[std::min(all.size() - 1,
                        static_cast<size_t>(p * static_cast<double>(all.size())))];
  };

  const int total = n_threads * n_requests;
  std::cout << "total=" << total << " ok=" << ok
            << " bad_status=" << bad_status << " bad_body=" << bad_body
            << " transport_err=" << transport_err << "\n";
  std::cout << "latency_us p50=" << pct(0.50) << " p90=" << pct(0.90)
            << " p99=" << pct(0.99) << " max=" << (all.empty() ? 0 : all.back())
            << "\n";

  if (ok == total) {
    std::cout << "INTEGRATION PASS\n";
    return 0;
  }
  std::cout << "INTEGRATION FAIL\n";
  return 1;
}
