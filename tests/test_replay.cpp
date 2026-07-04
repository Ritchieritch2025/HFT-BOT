// Replay-equivalence tests (Phase 6): marker-aware replay (a gap marker is
// replayed as an invalidation so replay mirrors the live blind spot), and
// book-checksum equivalence between a directly-applied book and the replayed
// book, including across an epoch boundary.
//
// usage: test_replay <scratch_dir>

#include "kalshi/gateway.hpp"
#include "kalshi/orderbook.hpp"
#include "trading/storage.hpp"

#include <cstdio>
#include <iostream>
#include <string>

using namespace trading;
using kalshi::OrderBook;
using kalshi::SnapshotView;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
RawRecord data(std::uint64_t seq, std::string raw) {
  RawRecord r;
  r.source = SourceId::Kalshi;
  r.source_ticker = "MKT-A";
  r.source_stream_id = 7;
  r.source_sequence = seq;
  r.stream_epoch = 1;
  r.raw = std::move(raw);
  return r;
}
RawRecord marker(std::string kind, std::uint32_t epoch = 1) {
  RawRecord r;
  r.source = SourceId::Kalshi;
  r.marker = std::move(kind);
  r.stream_epoch = epoch;
  return r;
}

// Replays events into a book; the marker handler invalidates on "gap".
struct BookReplay : EventSink {
  OrderBook book;
  void on_event(const NormalizedEvent& e) override {
    const std::uint64_t seq = e.source_sequence.value_or(0);
    if (e.kind() == Kind::BookSnapshot) {
      const auto& s = std::get<BookSnapshot>(e.payload);
      book.load_snapshot(SnapshotView{s.yes, s.no, seq});
    } else if (e.kind() == Kind::BookDelta) {
      const auto& d = std::get<BookDelta>(e.payload);
      book.apply_delta(d.side, d.price, d.delta, seq);
    }
  }
  void on_marker(const RawRecord& r) {
    if (r.marker && *r.marker == "gap") book.invalidate();  // mirror live blind spot
  }
};

const char* kSnap1 = R"({"type":"orderbook_snapshot","sid":7,"seq":1,"msg":{"market_ticker":"MKT-A","yes_dollars_fp":[["0.4000","500.00"],["0.4200","300.00"]],"no_dollars_fp":[["0.5500","100.00"]]}})";
const char* kD2 = R"({"type":"orderbook_delta","sid":7,"seq":2,"msg":{"market_ticker":"MKT-A","price_dollars":"0.4200","delta_fp":"100.00","side":"yes"}})";
const char* kD3 = R"({"type":"orderbook_delta","sid":7,"seq":3,"msg":{"market_ticker":"MKT-A","price_dollars":"0.4300","delta_fp":"50.00","side":"yes"}})";
const char* kD5 = R"({"type":"orderbook_delta","sid":7,"seq":5,"msg":{"market_ticker":"MKT-A","price_dollars":"0.4200","delta_fp":"1.00","side":"yes"}})";
const char* kSnap6 = R"({"type":"orderbook_snapshot","sid":7,"seq":6,"msg":{"market_ticker":"MKT-A","yes_dollars_fp":[["0.4100","400.00"]],"no_dollars_fp":[["0.5600","10.00"]]}})";
}  // namespace

int main(int argc, char** argv) {
  const std::string dir = argc > 1 ? argv[1] : ".";
  const std::string path = dir + "/replay6.ndjson";
  std::remove(path.c_str());

  // A recorded session with a gap (marker) between seq3 and a stale seq5, then
  // an in-stream snapshot (seq6) recovery — spanning an epoch_change marker.
  {
    RawLogWriter w(path);
    w.write(data(1, kSnap1));
    w.write(data(2, kD2));
    w.write(data(3, kD3));
    w.write(marker("gap"));
    w.write(data(5, kD5));      // stale after gap -> dropped by the book
    w.write(marker("epoch_change", 1));
    w.write(data(6, kSnap6));   // in-stream recovery
  }

  // Reference: apply directly, mirroring the same blind spot.
  OrderBook ref;
  ref.load_snapshot(SnapshotView{{{4000, 50000}, {4200, 30000}}, {{5500, 10000}}, 1});
  ref.apply_delta(Side::Yes, 4200, 10000, 2);
  ref.apply_delta(Side::Yes, 4300, 5000, 3);
  ref.invalidate();                                  // gap
  ref.apply_delta(Side::Yes, 4200, 100, 5);          // dropped (invalid)
  ref.load_snapshot(SnapshotView{{{4100, 40000}}, {{5600, 1000}}, 6});  // recover

  // Replay.
  kalshi::KalshiRawDecoder decoder;
  BookReplay sink;
  ReplaySource src(path, decoder, SourceId::Kalshi);
  src.set_sink(&sink);
  src.on_marker([&](const RawRecord& r) { sink.on_marker(r); });
  src.start();

  check(src.markers() == 2, "replay surfaced 2 marker records (gap + epoch_change)");
  check(sink.book.valid() && sink.book.last_seq() == 6, "replayed book recovered to seq 6");
  check(sink.book.checksum() == ref.checksum(),
        "replayed book checksum == directly-applied book (bit-for-bit)");
  check(sink.book.best_yes_bid() == ref.best_yes_bid(), "best bid matches");

  // Determinism: a second identical replay yields the same checksum.
  BookReplay sink2;
  ReplaySource src2(path, decoder, SourceId::Kalshi);
  src2.set_sink(&sink2);
  src2.on_marker([&](const RawRecord& r) { sink2.on_marker(r); });
  src2.start();
  check(sink2.book.checksum() == sink.book.checksum(), "two replays are checksum-identical");

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
