// Ring<T> unit + concurrency test. Run under TSan via `make tsan`.

#include "kalshi/ring.hpp"

#include <atomic>
#include <cstdint>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

namespace {

int g_failures = 0;

void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

}  // namespace

int main() {
  // Capacity validation.
  bool threw = false;
  try {
    kalshi::Ring<int> bad(24);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check(threw, "non-power-of-two capacity rejected");

  // FIFO order, full/empty edges.
  kalshi::Ring<std::uint64_t> r(8);
  std::uint64_t v = 0;
  check(!r.try_pop(v), "pop on empty returns false");
  bool all_pushed = true;
  for (std::uint64_t i = 0; i < 8; ++i) {
    std::uint64_t x = i;
    all_pushed = all_pushed && r.try_push(std::move(x));
  }
  check(all_pushed, "push accepts exactly capacity items");
  std::uint64_t full_probe = 99;
  check(!r.try_push(std::move(full_probe)), "push on full returns false");
  bool fifo = true;
  for (std::uint64_t i = 0; i < 8; ++i) {
    fifo = fifo && r.try_pop(v) && v == i;
  }
  check(fifo, "FIFO order preserved");
  check(!r.try_pop(v), "empty again after draining");

  // Wraparound well past capacity.
  bool wrap_ok = true;
  for (std::uint64_t i = 0; i < 100; ++i) {
    std::uint64_t x = i;
    wrap_ok = wrap_ok && r.try_push(std::move(x)) && r.try_pop(v) && v == i;
  }
  check(wrap_ok, "wraparound over 12x capacity");

  // MPMC hammer: 2 producers x 500k, 2 consumers. Values encode
  // (producer_id << 32) | seq; assert per-producer monotonicity and totals.
  constexpr int kProducers = 2, kConsumers = 2;
  constexpr std::uint64_t kPerProducer = 500'000;
  kalshi::Ring<std::uint64_t> ring(1024);
  std::atomic<bool> done{false};
  std::atomic<std::uint64_t> consumed{0};
  std::atomic<std::uint64_t> checksum{0};
  std::vector<std::vector<std::uint64_t>> last_seen(
      kConsumers, std::vector<std::uint64_t>(kProducers, 0));
  std::atomic<bool> order_violation{false};

  std::vector<std::thread> consumers;
  for (int c = 0; c < kConsumers; ++c) {
    consumers.emplace_back([&, c] {
      std::uint64_t val;
      for (;;) {
        if (ring.try_pop(val)) {
          const auto pid = static_cast<size_t>(val >> 32);
          const std::uint64_t seq = val & 0xFFFFFFFFull;
          // Any single consumer must see strictly increasing seqs per producer.
          if (seq < last_seen[static_cast<size_t>(c)][pid])
            order_violation.store(true);
          last_seen[static_cast<size_t>(c)][pid] = seq;
          checksum.fetch_add(seq, std::memory_order_relaxed);
          consumed.fetch_add(1, std::memory_order_relaxed);
        } else if (done.load(std::memory_order_acquire) &&
                   consumed.load() == kProducers * kPerProducer) {
          return;
        } else {
          std::this_thread::yield();
        }
      }
    });
  }

  std::vector<std::thread> producers;
  for (int p = 0; p < kProducers; ++p) {
    producers.emplace_back([&, p] {
      for (std::uint64_t i = 1; i <= kPerProducer; ++i) {
        std::uint64_t val = (static_cast<std::uint64_t>(p) << 32) | i;
        while (!ring.try_push(std::move(val))) std::this_thread::yield();
      }
    });
  }
  for (auto& t : producers) t.join();
  done.store(true, std::memory_order_release);
  for (auto& t : consumers) t.join();

  check(consumed.load() == kProducers * kPerProducer,
        "MPMC: all items consumed exactly once (" +
            std::to_string(consumed.load()) + ")");
  const std::uint64_t expect_sum =
      kProducers * (kPerProducer * (kPerProducer + 1) / 2);
  check(checksum.load() == expect_sum, "MPMC: checksum matches");
  check(!order_violation.load(), "MPMC: per-producer FIFO per consumer");

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
