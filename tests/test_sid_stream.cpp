// SidStream + OrderBookManager sequencing tests (Phase 0):
//   1. multi-market interleaved deltas on one sid, per-sid contiguous seq
//      -> zero false resyncs;
//   2. sequenced ok/unsubscribed interleaved into the stream -> no false gap;
//   3. true gap -> all sid members invalid; in-stream snapshot re-validates
//      only its own market;
//   4. invalid book: accessors nullopt, deltas dropped and counted.
// Pure C++, no deps.

#include "kalshi/orderbook.hpp"
#include "kalshi/sid_stream.hpp"

#include <iostream>
#include <string>
#include <vector>

using namespace kalshi;
using trading::EntityId;
using trading::Side;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

struct RecordingResync : ResyncHandler {
  int calls = 0;
  std::vector<EntityId> last;
  void request_resync(std::uint64_t, const std::vector<EntityId>& m) override {
    ++calls; last = m;
  }
};

SnapshotView onelevel(trading::PriceE4 yes_px, trading::CountFp sz) {
  SnapshotView s;
  s.yes = {{yes_px, sz}};
  return s;
}
}  // namespace

int main() {
  const EntityId A = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-A");
  const EntityId B = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-B");
  constexpr std::uint64_t SID = 42;
  constexpr std::uint32_t EPOCH = 1;

  // --- SidStream unit: contiguity, control seq, gap, stale ---
  {
    SidStream s(SID, EPOCH);
    check(s.observe(5) == SidStream::SeqResult::First, "first seq -> First");
    check(s.observe(6) == SidStream::SeqResult::Ok, "6 contiguous -> Ok");
    check(s.observe(7) == SidStream::SeqResult::Ok, "7 contiguous -> Ok");
    check(s.observe(6) == SidStream::SeqResult::Stale, "old seq -> Stale");
    check(s.observe(9) == SidStream::SeqResult::Gap, "jump 8->skipped -> Gap");
    check(s.observe(10) == SidStream::SeqResult::Ok, "counter re-based after gap");
    check(s.expected_next() == 11, "expected_next tracks");
  }

  // --- 1. multi-market interleaved deltas, contiguous per-sid seq, no resync ---
  {
    RecordingResync rz;
    OrderBookManager mgr(&rz);
    mgr.bind(SID, EPOCH, A);
    mgr.bind(SID, EPOCH, B);
    mgr.on_snapshot(SID, EPOCH, A, onelevel(4000, 500), 1);
    mgr.on_snapshot(SID, EPOCH, B, onelevel(6000, 500), 2);
    // Interleave A and B deltas; seq is per-sid contiguous (3,4,5,6).
    check(mgr.on_delta(SID, EPOCH, A, Side::Yes, 4000, 10, 3) == ApplyResult::Ok, "A d seq3 Ok");
    check(mgr.on_delta(SID, EPOCH, B, Side::Yes, 6000, 20, 4) == ApplyResult::Ok, "B d seq4 Ok");
    check(mgr.on_delta(SID, EPOCH, A, Side::Yes, 4000, -5, 5) == ApplyResult::Ok, "A d seq5 Ok");
    check(mgr.on_delta(SID, EPOCH, B, Side::Yes, 6000, 5, 6) == ApplyResult::Ok, "B d seq6 Ok");
    check(rz.calls == 0, "interleaved contiguous stream -> ZERO false resyncs");
    check(mgr.book(A)->valid() && mgr.book(B)->valid(), "both books valid");
  }

  // --- 2. sequenced ok/unsubscribed interleaved -> no false gap ---
  {
    RecordingResync rz;
    OrderBookManager mgr(&rz);
    mgr.bind(SID, EPOCH, A);
    mgr.on_snapshot(SID, EPOCH, A, onelevel(4000, 500), 1);
    check(mgr.on_delta(SID, EPOCH, A, Side::Yes, 4000, 10, 2) == ApplyResult::Ok, "delta seq2 Ok");
    // A sequenced control response consumes seq 3 (ok-with-seq / unsubscribed).
    check(mgr.on_control_seq(SID, EPOCH, 3) == ApplyResult::Ok, "control seq3 consumed");
    // The next delta is seq 4 — contiguous BECAUSE control consumed 3.
    check(mgr.on_delta(SID, EPOCH, A, Side::Yes, 4000, 10, 4) == ApplyResult::Ok,
          "delta seq4 Ok (control seq did not read as a gap)");
    check(rz.calls == 0, "no false gap across a sequenced control message");
  }

  // --- 3. true gap invalidates ALL sid members; snapshot re-validates one ---
  {
    RecordingResync rz;
    OrderBookManager mgr(&rz);
    mgr.bind(SID, EPOCH, A);
    mgr.bind(SID, EPOCH, B);
    mgr.on_snapshot(SID, EPOCH, A, onelevel(4000, 500), 1);
    mgr.on_snapshot(SID, EPOCH, B, onelevel(6000, 500), 2);
    mgr.on_delta(SID, EPOCH, A, Side::Yes, 4000, 10, 3);
    // Gap: expected 4, got 9 — arrives on A but must invalidate B too (I3).
    check(mgr.on_delta(SID, EPOCH, A, Side::Yes, 4000, 10, 9) == ApplyResult::NeedResync,
          "gap on sid -> NeedResync");
    check(!mgr.book(A)->valid() && !mgr.book(B)->valid(),
          "gap invalidates EVERY market on the sid (A and B)");
    check(rz.calls == 1 && rz.last.size() == 2, "one resync request covering both markets");
    // In-stream snapshot for A only (contiguous next seq 10) re-validates A, not B.
    mgr.on_snapshot(SID, EPOCH, A, onelevel(4100, 500), 10);
    check(mgr.book(A)->valid(), "A re-validated by its in-stream snapshot");
    check(!mgr.book(B)->valid(), "B still invalid until its own snapshot arrives");
  }

  // --- 4. invalid book: accessors nullopt, subsequent deltas dropped+counted ---
  {
    RecordingResync rz;
    OrderBookManager mgr(&rz);
    mgr.bind(SID, EPOCH, A);
    mgr.on_snapshot(SID, EPOCH, A, onelevel(4000, 500), 1);
    // Drive A's book negative -> Invalid (corruption), still contiguous seq.
    check(mgr.on_delta(SID, EPOCH, A, Side::Yes, 4000, -1000, 2) == ApplyResult::Invalid,
          "negative level -> Invalid");
    check(!mgr.book(A)->valid() && !mgr.book(A)->best_yes_bid().has_value(),
          "invalid book: best_yes_bid nullopt");
    // Next contiguous delta is dropped (book invalid), and counted.
    check(mgr.on_delta(SID, EPOCH, A, Side::Yes, 4000, 10, 3) == ApplyResult::NeedResync,
          "delta on invalid book -> NeedResync (dropped)");
    check(mgr.dropped_invalid_count() == 1, "dropped-on-invalid counter incremented");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
