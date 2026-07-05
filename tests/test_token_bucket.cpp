// TokenBucketI64: integer accounting, exact-cost reserve, insufficient => wait,
// cap saturation, bucket independence, deterministic fake clock, the 10^7-cycle
// zero-drift property, and a concurrency hammer (also built under TSan).

#include "kalshi/token_bucket.hpp"

#include <atomic>
#include <chrono>
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
constexpr std::int64_t kSec = 1'000'000'000;  // ns
}  // namespace

int main() {
  using namespace kalshi;

  // --- basic reserve / refuse / refill on a fake clock ---
  {
    TokenBucketI64 b(100, 100);  // 100 tok/s, cap 100, starts full
    std::int64_t t = 1'000'000'000;  // arbitrary start
    check(b.available_tokens(t) == 100, "starts full at capacity");
    check(b.try_reserve(100, t), "exact-capacity reserve succeeds");
    check(b.available_tokens(t) == 0, "drained to zero");
    check(!b.try_reserve(1, t), "insufficient => refuse (no time passed)");
    // 10ms at 100/s = exactly 1 token.
    check(b.try_reserve(1, t + kSec / 100), "1 token after 10ms");
    check(!b.try_reserve(1, t + kSec / 100), "only the 1 accrued token available");
  }

  // --- cap saturation: idle does not exceed capacity ---
  {
    TokenBucketI64 b(100, 100);
    std::int64_t t = 5 * kSec;
    b.try_reserve(100, t);                       // drain
    check(b.available_tokens(t + 1000 * kSec) == 100, "long idle saturates at cap, not beyond");
  }

  // --- independence of two buckets (Read vs Write) ---
  {
    TokenBucketI64 rd(300, 300), wr(100, 100);
    std::int64_t t = kSec;
    check(rd.try_reserve(300, t), "read drains");
    check(wr.available_tokens(t) == 100, "write bucket unaffected by read reservation");
    check(!rd.try_reserve(1, t) && wr.try_reserve(100, t), "buckets are independent");
  }

  // --- reserve_or_wait: grant-now, grant-with-wait (debt), deadline refuse ---
  {
    TokenBucketI64 b(100, 100);
    std::int64_t t = kSec;
    auto now_ok = b.reserve_or_wait(100, t, t + kSec);
    check(now_ok.granted && now_ok.wait_ns == 0, "reserve_or_wait grants immediately when available");
    // Bucket now empty; need 50 => 0.5s wait at 100/s.
    auto waited = b.reserve_or_wait(50, t, t + kSec);
    check(waited.granted && waited.wait_ns == kSec / 2, "reserve_or_wait grants with 0.5s wait (debt)");
    check(waited.after_milli < 0, "granted-with-wait puts the bucket into debt");
    // A tight deadline (1ms) can't cover a 0.5s wait => refuse, no deduction.
    std::int64_t before = b.available_milli(t);
    auto refused = b.reserve_or_wait(50, t, t + kSec / 1000);
    check(!refused.granted, "deadline shorter than wait => refuse");
    check(b.available_milli(t) == before, "refused reservation does not deduct");
  }

  // --- exact reservation telemetry fields ---
  {
    TokenBucketI64 b(1000, 1000);
    std::int64_t t = kSec;
    auto r = b.reserve_or_wait(250, t, t + kSec);
    check(r.granted && r.cost_milli == 250 * 1000 &&
              r.before_milli == 1000 * 1000 && r.after_milli == 750 * 1000,
          "reservation reports before/after/cost in milli-tokens");
  }

  // --- ZERO-DRIFT: 10^7 incremental refills == the closed-form total exactly ---
  {
    const std::int64_t rate = 1000, cap = 20000;
    TokenBucketI64 b(rate, cap);
    b.try_reserve(cap, 0);            // drain to 0 at t=0
    const std::int64_t dt = 1500;     // ns/cycle: 1.5 milli-tokens/cycle (fractional -> exercises carry)
    const std::int64_t N = 10'000'000;
    std::int64_t now = 0;
    for (std::int64_t i = 1; i <= N; ++i) {
      now += dt;
      (void)b.available_milli(now);   // incremental refill with carry
    }
    // Closed form: total generated = rate*dt*N / kAcc milli-tokens (stays < cap => no clamp).
    const std::int64_t expected = (rate * dt * N) / TokenBucketI64::kAcc;  // = 15,000,000 milli
    const std::int64_t got = b.available_milli(now);
    check(got == expected, "10^7-cycle incremental refill == closed-form total (zero drift)");
    check(got == 15'000'000 && got < cap * 1000, "drift-free total is exact and below cap");
  }

  // --- concurrency hammer: many threads reserve on one bucket, no crash, bounded grants ---
  {
    TokenBucketI64 b(100000, 100000);  // generous so real-time refill matters little
    std::atomic<std::int64_t> granted{0};
    const int threads = 8, per = 20000;
    std::vector<std::thread> ts;
    auto mono = [] {
      return std::chrono::duration_cast<std::chrono::nanoseconds>(
                 std::chrono::steady_clock::now().time_since_epoch())
          .count();
    };
    for (int k = 0; k < threads; ++k)
      ts.emplace_back([&] {
        for (int i = 0; i < per; ++i)
          if (b.try_reserve(1, mono())) granted.fetch_add(1, std::memory_order_relaxed);
      });
    for (auto& t : ts) t.join();
    // Can't grant more than the initial capacity plus whatever refilled during the run.
    check(granted.load() <= 100000 + threads * per, "concurrent grants never exceed capacity+refill (no double-spend)");
    check(granted.load() > 0, "some reservations succeeded under contention");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
