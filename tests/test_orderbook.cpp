// Orderbook tests: snapshot load, delta add/remove/zero, seq-gap -> NeedResync,
// negative -> Invalid (no clamp) + post-invalid contract, crossed detection,
// best/implied prices, manager resync recovery. Pure C++, no deps.

#include "kalshi/orderbook.hpp"

#include <iostream>
#include <optional>
#include <string>

using namespace kalshi;
using trading::Side;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
template <class T>
bool is(const std::optional<T>& o, T v) { return o.has_value() && *o == v; }

SnapshotView snap(std::uint64_t seq) {
  SnapshotView s;
  s.seq = seq;
  s.yes = {{4000, 500}, {4200, 300}};  // best yes bid 0.4200
  s.no = {{5400, 200}, {5500, 100}};   // best no bid 0.5500 -> implied yes ask 0.4500
  return s;
}
}  // namespace

int main() {
  // --- snapshot load + best/implied ---
  {
    OrderBook b;
    check(!b.valid(), "book invalid before any snapshot");
    b.load_snapshot(snap(10));
    check(b.valid(), "valid after snapshot");
    check(is(b.best_yes_bid(), 4200), "best yes bid = 4200");
    check(is(b.best_no_bid(), 5500), "best no bid = 5500");
    check(is(b.implied_yes_ask(), 4500), "implied yes ask = 10000-5500 = 4500");
    check(!b.crossed(), "not crossed (4200 < 4500)");
    check(is(b.yes_size_at(4200), trading::CountFp{300}), "size at 4200 = 300");
  }

  // --- delta add / grow / remove / zero ---
  {
    OrderBook b;
    b.load_snapshot(snap(10));
    check(b.apply_delta(Side::Yes, 4200, 100, 11) == ApplyResult::Ok, "grow level Ok");
    check(is(b.yes_size_at(4200), trading::CountFp{400}), "4200 now 400");
    check(b.apply_delta(Side::Yes, 4300, 50, 12) == ApplyResult::Ok, "add new level Ok");
    check(is(b.best_yes_bid(), 4300), "best yes bid now 4300");
    check(b.apply_delta(Side::Yes, 4300, -50, 13) == ApplyResult::Ok, "reduce to zero Ok");
    check(is(b.yes_size_at(4300), trading::CountFp{0}), "4300 removed (size 0)");
    check(is(b.best_yes_bid(), 4200), "best yes bid back to 4200");
    check(b.last_seq() == 13, "seq advanced to 13");
  }

  // Note: per-market seq-gap detection now lives in SidStream (seq is per-sid,
  // not per-market) — see tests/test_sid_stream.cpp. OrderBook::apply_delta no
  // longer gates on seq; it only guards local invariants.

  // --- negative -> Invalid, no clamp, post-invalid contract ---
  {
    OrderBook b;
    b.load_snapshot(snap(10));
    check(b.apply_delta(Side::Yes, 4200, -1000, 11) == ApplyResult::Invalid,
          "delta below zero -> Invalid");
    check(!b.valid(), "book invalid after negative delta");
    check(!b.best_yes_bid().has_value(), "best_yes_bid nullopt while invalid");
    check(!b.implied_yes_ask().has_value(), "implied ask nullopt while invalid");
    check(b.yes_depth().empty(), "depth empty while invalid");
    check(b.apply_delta(Side::Yes, 4200, 50, 12) == ApplyResult::NeedResync,
          "further deltas NeedResync while invalid");
    b.load_snapshot(snap(20));  // only a snapshot recovers
    check(b.valid() && is(b.best_yes_bid(), 4200), "load_snapshot recovers the book");
  }

  // --- crossed detection ---
  {
    OrderBook b;
    SnapshotView s; s.seq = 1;
    s.yes = {{4600, 100}};  // yes bid 0.46
    s.no = {{5500, 100}};   // no bid 0.55 -> implied yes ask 0.45
    b.load_snapshot(s);
    check(b.crossed(), "crossed: yes bid 0.46 >= implied ask 0.45");
  }

  // --- manager: gap -> non-blocking resync request (no REST reseed) ---
  {
    struct Stub : ResyncHandler {
      int calls = 0;
      std::uint64_t last_sid = 0;
      std::vector<EntityId> last_markets;
      void request_resync(std::uint64_t sid, const std::vector<EntityId>& m) override {
        ++calls; last_sid = sid; last_markets = m;
      }
    } stub;

    OrderBookManager mgr(&stub);
    const EntityId e = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-1");
    mgr.bind(/*sid*/ 7, /*epoch*/ 1, e);
    mgr.on_snapshot(7, 1, e, snap(10), 10);
    check(mgr.on_delta(7, 1, e, Side::Yes, 4200, 100, 11) == ApplyResult::Ok,
          "manager applies contiguous delta");
    // Induce a gap (expected 12, got 99):
    const ApplyResult r = mgr.on_delta(7, 1, e, Side::Yes, 4200, 100, 99);
    check(r == ApplyResult::NeedResync, "manager reports NeedResync on sid gap");
    check(stub.calls == 1 && stub.last_sid == 7, "non-blocking resync requested for the sid");
    check(!mgr.book(e)->valid(), "book invalidated on gap (no REST reseed)");
    // Recovery is an in-stream snapshot with the next contiguous seq (100).
    mgr.on_snapshot(7, 1, e, snap(100), 100);
    check(mgr.book(e)->valid() && mgr.book(e)->last_seq() == 100,
          "in-stream snapshot re-validates the market");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
