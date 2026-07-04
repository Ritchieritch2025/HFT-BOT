// WsRecorder tests: SPSC record -> writer thread -> RawLogWriter round-trip,
// marker records, and overflow -> drop + "loss" marker (never blocks). The
// running-writer path is a 2-thread producer/consumer (TSan target).
//
// usage: test_recorder <scratch_dir>

#include "kalshi/ws_recorder.hpp"
#include "trading/storage.hpp"

#include <cstdio>
#include <iostream>
#include <string>
#include <vector>

using namespace kalshi;
using trading::RawRecord;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
RawRecord mk(std::uint64_t seq, std::string raw) {
  RawRecord r;
  r.source = trading::SourceId::Kalshi;
  r.source_ticker = "MKT-A";
  r.source_stream_id = 7;
  r.source_sequence = seq;
  r.stream_epoch = 1;
  r.raw = std::move(raw);
  return r;
}
std::vector<RawRecord> read_all(const std::string& path) {
  std::vector<RawRecord> out;
  trading::RawLogReader r(path);
  while (auto rec = r.next()) out.push_back(*rec);
  return out;
}
}  // namespace

int main(int argc, char** argv) {
  const std::string dir = argc > 1 ? argv[1] : ".";

  // --- round-trip through the writer thread ---
  {
    const std::string path = dir + "/rec_rt.ndjson";
    std::remove(path.c_str());
    {
      WsRecorder rec(path);
      rec.start();  // writer thread running -> 2-thread producer/consumer
      for (int i = 1; i <= 500; ++i) rec.record(mk(i, R"({"seq":)" + std::to_string(i) + "}"));
      rec.mark("epoch_change", 2);
      rec.stop();  // joins writer, flushes
      check(rec.recorded() == 500 && rec.dropped() == 0, "500 recorded, 0 dropped");
    }
    auto recs = read_all(path);
    int data = 0, epoch_markers = 0;
    bool fields_ok = false;
    for (auto& r : recs) {
      if (r.marker && *r.marker == "epoch_change") ++epoch_markers;
      else {
        ++data;
        if (r.source_sequence == 1) fields_ok = r.source_stream_id == 7 && r.raw == R"({"seq":1})";
      }
    }
    check(data == 500, "all 500 data records read back");
    check(epoch_markers == 1, "epoch_change marker recorded");
    check(fields_ok, "record fields (sid/seq/raw) preserved");
  }

  // --- overflow: fill before the writer starts -> drops + loss marker ---
  {
    const std::string path = dir + "/rec_of.ndjson";
    std::remove(path.c_str());
    std::uint64_t dropped = 0;
    {
      WsRecorder rec(path, /*ring_capacity*/ 2);  // tiny ring
      for (int i = 1; i <= 10; ++i) rec.record(mk(i, "x"));  // writer not started
      dropped = rec.dropped();
      rec.start();  // now drains the 2 queued + emits a loss marker
      rec.stop();
    }
    check(dropped == 8, "8 of 10 dropped on a capacity-2 ring (never blocked)");
    auto recs = read_all(path);
    long loss = 0, data = 0;
    std::uint64_t lost_count = 0;
    for (auto& r : recs) {
      if (r.marker && *r.marker == "loss") { ++loss; lost_count = r.source_sequence.value_or(0); }
      else ++data;
    }
    check(data == 2, "2 records survived the overflow");
    check(loss == 1 && lost_count == 8, "a loss marker records the 8 dropped frames");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
