// Microbenchmark: orderbook delta application throughput (ns/delta). Pure logic,
// no network. Deterministic pseudo-random deltas (LCG) so runs are comparable.

#include "kalshi/orderbook.hpp"

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <vector>

using namespace kalshi;
using trading::Side;

int main(int argc, char** argv) {
  const int n = argc > 1 ? std::atoi(argv[1]) : 2'000'000;

  // Seed a book with 100 levels each side.
  SnapshotView s;
  s.seq = 0;
  for (int i = 1; i <= 100; ++i) {
    // Same price grid on both sides so deltas hit existing levels (the random
    // walk stays well above zero over the run).
    s.yes.push_back({static_cast<trading::PriceE4>(i * 10), 100000});
    s.no.push_back({static_cast<trading::PriceE4>(i * 10), 100000});
  }
  OrderBook book;
  book.load_snapshot(s);

  // Deterministic LCG for prices/deltas.
  std::uint64_t rng = 0x9e3779b97f4a7c15ULL;
  auto next = [&] { rng = rng * 6364136223846793005ULL + 1442695040888963407ULL; return rng >> 33; };

  std::uint64_t seq = 0;
  std::uint64_t ok = 0;
  const auto t0 = std::chrono::steady_clock::now();
  for (int i = 0; i < n; ++i) {
    const Side side = (next() & 1) ? Side::Yes : Side::No;
    const trading::PriceE4 price = static_cast<trading::PriceE4>(((next() % 100) + 1) * 10);
    // Alternate +/- but keep levels well above zero (seeded at 100000).
    const trading::CountFp delta = (i & 1) ? 10 : -10;
    if (book.apply_delta(side, price, delta, ++seq) == ApplyResult::Ok) ++ok;
  }
  const auto t1 = std::chrono::steady_clock::now();
  const double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();

  std::printf("orderbook: %d deltas, %llu applied, %.1f ns/delta (%.2f M/s)\n", n,
              static_cast<unsigned long long>(ok), ns / n, n / (ns / 1e9) / 1e6);
  return 0;
}
