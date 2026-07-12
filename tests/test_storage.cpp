// Storage + replay tests: byte-exact raw (UTF-8 + binary/base64), rotation,
// truncated-final-line tolerance, and replay rebuilding a book equal to
// directly-applied deltas with trace_ids + original SourceId preserved.
//
// usage: test_storage <scratch_dir>

#include "kalshi/gateway.hpp"
#include "kalshi/orderbook.hpp"
#include "trading/storage.hpp"

#include <cstdio>
#include <iostream>
#include <string>
#include <vector>

using namespace trading;
using kalshi::OrderBook;
using kalshi::SnapshotView;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

RawRecord mk(SourceId src, std::string ticker, std::uint64_t seq, std::string raw) {
  RawRecord r;
  r.recv_mono_ns = 111 + static_cast<std::int64_t>(seq);
  r.recv_wall_ns = 222 + static_cast<std::int64_t>(seq);
  r.source = src;
  r.channel = "orderbook_delta";
  r.source_ticker = std::move(ticker);
  r.source_sequence = seq;
  r.raw = std::move(raw);
  return r;
}

// Applies replayed NormalizedEvents into an OrderBook.
struct ReplayBookSink : EventSink {
  OrderBook book;
  std::uint64_t events = 0;
  SourceId last_source = SourceId::Unknown;
  bool trace_ok = true;
  void on_event(const NormalizedEvent& e) override {
    ++events;
    last_source = e.source;
    if (e.trace_id.v == 0) trace_ok = false;
    const std::uint64_t seq = e.source_sequence.value_or(0);
    if (e.kind() == Kind::BookSnapshot) {
      const auto& s = std::get<BookSnapshot>(e.payload);
      book.load_snapshot(SnapshotView{s.yes, s.no, seq});
    } else if (e.kind() == Kind::BookDelta) {
      const auto& d = std::get<BookDelta>(e.payload);
      book.apply_delta(d.side, d.price, d.delta, seq);
    }
  }
};
}  // namespace

int main(int argc, char** argv) {
  const std::string dir = argc > 1 ? argv[1] : ".";

  // --- byte-exact raw round-trip (UTF-8 with quotes/backslash/control) ---
  {
    const std::string path = dir + "/raw_utf8.ndjson";
    std::remove(path.c_str());
    const std::string tricky = R"({"a":"has \"quote\" and \\ and )" "\t" R"( tab","u":"é"})";
    {
      RawLogWriter w(path);
      w.write(mk(SourceId::Kalshi, "MKT", 1, tricky));
    }
    RawLogReader r(path);
    auto rec = r.next();
    check(rec.has_value() && rec->raw == tricky, "UTF-8 raw preserved byte-exact");
    check(rec && rec->source == SourceId::Kalshi && rec->source_sequence == 1,
          "record fields recovered");
    check(!r.next().has_value(), "single record then EOF");
  }

  // --- sid + stream_epoch round-trip; legacy line (absent) still reads ---
  {
    const std::string path = dir + "/sid.ndjson";
    std::remove(path.c_str());
    {
      RawLogWriter w(path);
      RawRecord r = mk(SourceId::Kalshi, "MKT", 5, R"({"seq":5})");
      r.source_stream_id = 42;
      r.stream_epoch = 3;
      w.write(r);
    }
    // Append a legacy line with no sid/stream_epoch fields.
    if (std::FILE* f = std::fopen(path.c_str(), "ab")) {
      const char legacy[] =
          "{\"recv_mono_ns\":1,\"recv_wall_ns\":2,\"source\":\"Kalshi\","
          "\"source_ticker\":\"OLD\",\"source_sequence\":9,\"raw\":\"{}\"}\n";
      std::fwrite(legacy, 1, sizeof(legacy) - 1, f);
      std::fclose(f);
    }
    RawLogReader r(path);
    auto a = r.next();
    check(a && a->source_stream_id == 42 && a->stream_epoch == 3,
          "sid + stream_epoch round-trip");
    auto b = r.next();
    check(b && !b->source_stream_id.has_value() && b->stream_epoch == 0 &&
              b->source_sequence == 9,
          "legacy line (no sid/epoch) still reads: sid absent, epoch 0");
  }

  // --- binary (non-UTF-8) raw via base64 ---
  {
    const std::string path = dir + "/raw_bin.ndjson";
    std::remove(path.c_str());
    std::string bin;
    bin.push_back('\x00'); bin.push_back('\x01'); bin.push_back('\xff'); bin.push_back('\xfe');
    bin += "tail";
    {
      RawLogWriter w(path);
      w.write(mk(SourceId::Kalshi, "MKT", 1, bin));
    }
    RawLogReader r(path);
    auto rec = r.next();
    check(rec.has_value() && rec->raw == bin, "binary raw preserved byte-exact (base64)");
  }

  // --- rotation by size ---
  {
    const std::string path = dir + "/rot.ndjson";
    for (const char* s : {"", ".1", ".2", ".3"}) std::remove((path + s).c_str());
    {
      RawLogWriter w(path, /*max_bytes*/ 200);  // tiny -> forces rotation
      for (int i = 0; i < 20; ++i)
        w.write(mk(SourceId::Kalshi, "MKT", i, std::string(60, 'x')));
    }
    // At least a couple rotations should have produced path.1, path.2 ...
    RawLogReader r0(path), r1(path + ".1");
    check(r0.ok() && r1.ok(), "rotation created base + at least one .N file");

    std::size_t highest = 0;
    while (std::FILE* f = std::fopen(
               (path + "." + std::to_string(highest + 1)).c_str(), "rb")) {
      std::fclose(f);
      ++highest;
    }
    std::vector<long> prior_sizes(highest + 1, -1);
    for (std::size_t i = 0; i <= highest; ++i) {
      const std::string p = i == 0 ? path : path + "." + std::to_string(i);
      if (std::FILE* f = std::fopen(p.c_str(), "rb")) {
        std::fseek(f, 0, SEEK_END);
        prior_sizes[i] = std::ftell(f);
        std::fclose(f);
      }
    }
    {
      RawLogWriter resumed(path, /*max_bytes*/ 200);
      resumed.write(mk(SourceId::Kalshi, "MKT", 999, std::string(60, 'y')));
    }
    bool earlier_unchanged = true;
    for (std::size_t i = 0; i < highest; ++i) {
      const std::string p = i == 0 ? path : path + "." + std::to_string(i);
      if (std::FILE* f = std::fopen(p.c_str(), "rb")) {
        std::fseek(f, 0, SEEK_END);
        earlier_unchanged = earlier_unchanged && std::ftell(f) == prior_sizes[i];
        std::fclose(f);
      }
    }
    check(highest >= 2 && earlier_unchanged,
          "restart resumes highest existing rotation shard, not an older shard");
  }

  // --- receive-clock UTC hour partitioning without reconnect -----------
  {
    const std::string root = dir + "/hourly/date={UTC_DATE}/rfq_{UTC_HOUR}.ndjson";
    const std::string p23 = dir + "/hourly/date=2026-07-11/rfq_23.ndjson";
    const std::string p00 = dir + "/hourly/date=2026-07-12/rfq_00.ndjson";
    std::remove(p23.c_str()); std::remove(p00.c_str());
    RawRecord a = mk(SourceId::Kalshi, "", 1, R"({"type":"rfq_created"})");
    RawRecord b = mk(SourceId::Kalshi, "", 2, R"({"type":"rfq_deleted"})");
    a.recv_wall_ns = 1783814399000000000LL;  // 2026-07-11T23:59:59Z
    b.recv_wall_ns = 1783814400000000000LL;  // 2026-07-12T00:00:00Z
    {
      RawLogWriter w(root);
      check(w.write(a) && w.write(b) && w.flush(),
            "time-partitioned writer persists both boundary records");
    }
    RawLogReader r23(p23), r00(p00);
    auto x = r23.next(), y = r00.next();
    check(x && x->recv_wall_ns == a.recv_wall_ns && !r23.next(),
          "time template keeps pre-boundary row in rfq_23");
    check(y && y->recv_wall_ns == b.recv_wall_ns && !r00.next(),
          "time template rotates at receive-clock UTC boundary without reconnect");
  }

  // A live collector must be able to distinguish an unwritable destination
  // from a healthy empty feed.
  {
    RawRecord rec = mk(SourceId::Kalshi, "", 1, R"({"type":"rfq_created"})");
    // Replace the directory with a regular file so create_directories/open
    // deterministically fails even when the test user owns the temp root.
    {
      std::FILE* blocker = std::fopen((dir + "/blocked").c_str(), "wb");
      if (blocker) std::fclose(blocker);
    }
    RawLogWriter blocked(dir + "/blocked/file.ndjson");
    check(!blocked.write(rec) && !blocked.flush(),
          "raw writer surfaces an unwritable capture destination");
  }

  // --- truncated final line is skipped, not fatal ---
  {
    const std::string path = dir + "/trunc.ndjson";
    std::remove(path.c_str());
    {
      RawLogWriter w(path);
      w.write(mk(SourceId::Kalshi, "MKT", 1, R"({"ok":1})"));
    }
    // Append a partial line with no trailing newline (simulates a crash).
    if (std::FILE* f = std::fopen(path.c_str(), "ab")) {
      const char partial[] = R"({"recv_mono_ns":9,"raw":"unter)";
      std::fwrite(partial, 1, sizeof(partial) - 1, f);
      std::fclose(f);
    }
    RawLogReader r(path);
    auto first = r.next();
    check(first.has_value(), "first (complete) record read");
    auto second = r.next();
    check(!second.has_value() && r.skipped_truncated() == 1,
          "truncated final line skipped, counted, not fatal");
  }

  // --- replay rebuilds a book equal to directly-applied deltas ---
  {
    const std::string path = dir + "/replay.ndjson";
    std::remove(path.c_str());
    const char* snap = R"({"type":"orderbook_snapshot","sid":2,"seq":1,"msg":{"market_ticker":"MKT-1","yes_dollars_fp":[["0.4000","500.00"],["0.4200","300.00"]],"no_dollars_fp":[["0.5400","200.00"],["0.5500","100.00"]]}})";
    const char* d1 = R"({"type":"orderbook_delta","sid":2,"seq":2,"msg":{"market_ticker":"MKT-1","price_dollars":"0.4200","delta_fp":"100.00","side":"yes"}})";
    const char* d2 = R"({"type":"orderbook_delta","sid":2,"seq":3,"msg":{"market_ticker":"MKT-1","price_dollars":"0.4300","delta_fp":"50.00","side":"yes"}})";
    const char* d3 = R"({"type":"orderbook_delta","sid":2,"seq":4,"msg":{"market_ticker":"MKT-1","price_dollars":"0.5500","delta_fp":"-100.00","side":"no"}})";
    {
      RawLogWriter w(path);
      w.write(mk(SourceId::Kalshi, "MKT-1", 1, snap));
      w.write(mk(SourceId::Kalshi, "MKT-1", 2, d1));
      w.write(mk(SourceId::Kalshi, "MKT-1", 3, d2));
      w.write(mk(SourceId::Kalshi, "MKT-1", 4, d3));
    }

    // Reference: apply the same directly. Sizes are CountFp = contracts x 100,
    // matching how the decoder parses "500.00" -> 50000.
    kalshi::OrderBook ref;
    ref.load_snapshot(SnapshotView{{{4000, 50000}, {4200, 30000}},
                                   {{5400, 20000}, {5500, 10000}}, 1});
    ref.apply_delta(Side::Yes, 4200, 10000, 2);
    ref.apply_delta(Side::Yes, 4300, 5000, 3);
    ref.apply_delta(Side::No, 5500, -10000, 4);

    kalshi::KalshiRawDecoder decoder;
    ReplayBookSink sink;
    ReplaySource src(path, decoder, SourceId::Kalshi);
    src.set_sink(&sink);
    src.start();

    check(src.records() == 4 && src.emitted() == 4, "replay emitted 4 events from 4 records");
    check(sink.last_source == SourceId::Kalshi, "replay preserved original SourceId");
    check(sink.trace_ok, "every replayed event carries a nonzero trace_id");
    check(src.id() == SourceId::Kalshi, "ReplaySource reports its SourceId");
    check(sink.book.valid() && ref.valid(), "both books valid");
    check(sink.book.best_yes_bid() == ref.best_yes_bid() &&
              sink.book.best_no_bid() == ref.best_no_bid(),
          "replayed book best bid/ask == directly-applied book");
    check(sink.book.yes_size_at(4300) == ref.yes_size_at(4300) &&
              sink.book.no_size_at(5500) == ref.no_size_at(5500),
          "replayed book levels == directly-applied book");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
