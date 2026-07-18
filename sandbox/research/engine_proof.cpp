// engine_proof.cpp — EXPLORATORY_NOT_GO_NO_GO
//
// Self-contained proof harness for the market-data engine claims made to the
// operator (2026-07-12). Four executable proofs, one file, no network, no
// production impact. Build:
//   g++ -O2 -std=c++20 -I include -o engine_proof sandbox/research/engine_proof.cpp
// Run:
//   ./engine_proof
//
// PROOF 1 — speed, reported as a DISTRIBUTION (operator ruling: no scalar
//           without its distribution): p50/p99/max ns/delta + ASCII histogram.
//           HONEST SCOPE (audit 2026-07-12): these are BATCH-MEAN samples
//           (1000 deltas/sample) — kernel-speed evidence, NOT per-message
//           tail latency. Per-message tails live in the EC2 replay harness
//           (real-corpus p50/p99/p99.9/max), not here.
// PROOF 2 — poison feed: a delta that drives a level negative must flip the
//           book to INVALID and every accessor must refuse to serve.
// PROOF 3 — sequence gap: a missed message must invalidate ALL books on the
//           subscription and request a resync (green lights may not lie).
// PROOF 4 — checksum determinism: identical operations => identical
//           fingerprint; a one-contract difference => different fingerprint.

#include "kalshi/orderbook.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <vector>

using namespace kalshi;
using trading::PriceE4;
using trading::Side;

static int g_fail = 0;
#define CHECK(cond, msg)                                        \
  do {                                                          \
    if (cond) std::printf("  PASS  %s\n", msg);                 \
    else { std::printf("  FAIL  %s\n", msg); ++g_fail; }        \
  } while (0)

static SnapshotView seed_book() {
  SnapshotView s;
  s.seq = 0;
  // Whole-cent levels 1..99¢ only (E4 = cents x 100). NOT the full E4 domain:
  // sub-penny prices exist and are exercised in the EC2 real-corpus replay.
  // Size 100000 = 1,000 contracts (CountFp = contracts x 10^2).
  for (int i = 1; i <= 99; ++i) {
    s.yes.push_back({static_cast<PriceE4>(i * 100), 100000});
    s.no.push_back({static_cast<PriceE4>(i * 100), 100000});
  }
  return s;
}

// ---------- PROOF 1: latency distribution ----------
static void proof_speed() {
  std::printf("\nPROOF 1 — delta-apply latency distribution\n");
  OrderBook book;
  book.load_snapshot(seed_book());

  std::uint64_t rng = 0x9e3779b97f4a7c15ULL;
  auto next = [&] { rng = rng * 6364136223846793005ULL + 1442695040888963407ULL; return rng >> 33; };

  // Batch timing: 1,000 deltas per sample so the clock's own ~20ns cost does
  // not drown the measurement. 2,000 samples = 2M deltas total.
  const int kBatch = 1000, kSamples = 2000;
  std::vector<double> ns_per_delta;
  ns_per_delta.reserve(kSamples);
  std::uint64_t seq = 0, not_ok = 0;
  for (int s = 0; s < kSamples; ++s) {
    const auto t0 = std::chrono::steady_clock::now();
    for (int i = 0; i < kBatch; ++i) {
      const Side side = (next() & 1) ? Side::Yes : Side::No;
      const PriceE4 price = static_cast<PriceE4>(((next() % 99) + 1) * 100);
      const trading::CountFp delta = (i & 1) ? 100 : -100;  // +/- 1 whole contract
      if (book.apply_delta(side, price, delta, ++seq) != ApplyResult::Ok) ++not_ok;
    }
    const auto t1 = std::chrono::steady_clock::now();
    ns_per_delta.push_back(
        std::chrono::duration<double, std::nano>(t1 - t0).count() / kBatch);
  }
  std::sort(ns_per_delta.begin(), ns_per_delta.end());
  const auto pct = [&](double p) {
    return ns_per_delta[static_cast<std::size_t>(p * (ns_per_delta.size() - 1))];
  };
  std::printf("  n=%d batches x %d deltas | p50=%.1f ns  p99=%.1f ns  max=%.1f ns  (batch means)\n",
              kSamples, kBatch, pct(0.50), pct(0.99), ns_per_delta.back());
  CHECK(not_ok == 0, "every benchmarked delta applied Ok (failures would fake speed)");

  // ASCII histogram, 12 buckets from min to p99.9 (tail bucket catches rest).
  const double lo = ns_per_delta.front(), hi = pct(0.999);
  if (hi <= lo) {  // degenerate: all samples identical — no histogram to draw
    std::printf("  (all samples identical at %.1f ns — histogram skipped)\n", lo);
    return;
  }
  const int kBuckets = 12;
  std::vector<int> h(kBuckets + 1, 0);
  for (double v : ns_per_delta) {
    int b = (v >= hi) ? kBuckets
                      : static_cast<int>((v - lo) / ((hi - lo) / kBuckets));
    if (b < 0) b = 0;
    if (b > kBuckets) b = kBuckets;
    ++h[b];
  }
  const int hmax = *std::max_element(h.begin(), h.end());
  for (int b = 0; b <= kBuckets; ++b) {
    const double left = (b == kBuckets) ? hi : lo + b * (hi - lo) / kBuckets;
    const int bar = hmax ? h[b] * 40 / hmax : 0;
    std::printf("  %7.1f ns |%-40.*s| %d%s\n", left, bar,
                "########################################", h[b],
                b == kBuckets ? "  (tail >= p99.9)" : "");
  }
}

// ---------- PROOF 2: poison feed ----------
static void proof_poison() {
  std::printf("\nPROOF 2 — corrupt book must refuse to serve\n");
  OrderBook book;
  book.load_snapshot(seed_book());
  CHECK(book.valid(), "book valid after snapshot");
  CHECK(book.best_yes_bid().has_value(), "best bid served while healthy");

  // Poison: level holds 100000; subtract 100001 -> negative -> book is wrong.
  const ApplyResult r =
      book.apply_delta(Side::Yes, 5000, -100001, 1);
  CHECK(r == ApplyResult::Invalid, "poison delta returns Invalid (not clamped)");
  CHECK(!book.valid(), "book flagged INVALID");
  CHECK(!book.best_yes_bid().has_value(), "best bid REFUSES to serve when invalid");
  CHECK(book.yes_depth().empty(), "depth REFUSES to serve when invalid");
  const ApplyResult r2 = book.apply_delta(Side::Yes, 5000, 10, 2);
  CHECK(r2 == ApplyResult::NeedResync, "further deltas refused until resync");
  book.load_snapshot(seed_book());
  CHECK(book.valid() && book.best_yes_bid().has_value(),
        "only a fresh snapshot revives the book");
}

// ---------- PROOF 3: sequence gap ----------
struct RecordingResync : ResyncHandler {
  int calls = 0;
  std::size_t markets = 0;
  void request_resync(std::uint64_t, const std::vector<EntityId>& mk) override {
    ++calls;
    markets = mk.size();
  }
};

static void proof_gap() {
  std::printf("\nPROOF 3 — a missed message invalidates the whole subscription\n");
  RecordingResync rec;
  OrderBookManager mgr(&rec);
  const std::uint64_t sid = 7;
  const EntityId m1{1}, m2{2};
  mgr.bind(sid, 0, m1);
  mgr.bind(sid, 0, m2);
  SnapshotView s = seed_book();
  mgr.on_snapshot(sid, 0, m1, s, 1);
  mgr.on_snapshot(sid, 0, m2, s, 2);
  CHECK(mgr.tradeable(m1) && mgr.tradeable(m2), "both books live after snapshots");

  // seq jumps 3 -> 5: message 4 was lost.
  ApplyResult r = mgr.on_delta(sid, 0, m1, Side::Yes, 5000, 10, 3);
  CHECK(r == ApplyResult::Ok, "in-sequence delta applies");
  r = mgr.on_delta(sid, 0, m1, Side::Yes, 5000, 10, 5);
  CHECK(r == ApplyResult::NeedResync, "gap detected on seq 3->5");
  CHECK(!mgr.tradeable(m1), "market 1 no longer tradeable");
  CHECK(!mgr.tradeable(m2), "market 2 ALSO not tradeable (same subscription)");
  CHECK(rec.calls == 1 && rec.markets == 2, "resync requested for all members");
}

// ---------- PROOF 4: checksum determinism ----------
static void proof_checksum() {
  std::printf("\nPROOF 4 — book fingerprint: identical ops = identical print\n");
  OrderBook a, b;
  a.load_snapshot(seed_book());
  b.load_snapshot(seed_book());
  for (int i = 0; i < 1000; ++i) {
    const PriceE4 p = static_cast<PriceE4>(((i * 37) % 99 + 1) * 100);
    a.apply_delta(Side::Yes, p, (i & 1) ? 5 : -5, i + 1);
    b.apply_delta(Side::Yes, p, (i & 1) ? 5 : -5, i + 1);
  }
  CHECK(a.checksum() == b.checksum(), "1000 identical ops -> identical checksum");
  b.apply_delta(Side::No, 5000, 1, 1001);  // one extra contract on one book
  CHECK(a.checksum() != b.checksum(),
        "a single one-contract difference -> different checksum");
}

int main() {
  std::printf("engine_proof — EXPLORATORY_NOT_GO_NO_GO (hardware-dependent numbers)\n");
  proof_speed();
  proof_poison();
  proof_gap();
  proof_checksum();
  std::printf("\n%s (failures: %d)\n", g_fail ? "== PROOF RUN FAILED ==" : "== ALL PROOFS PASS ==", g_fail);
  return g_fail ? 1 : 0;
}
