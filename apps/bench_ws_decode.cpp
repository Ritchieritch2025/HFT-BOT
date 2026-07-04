// WS hot-path microbenchmark: full frame JSON -> KalshiRawDecoder -> book apply,
// reporting ns per delta (p50/max). Contrasts with bench_orderbook (apply-only)
// to show how much of the WS path is JSON decode vs book math — the input to
// the "flat-array book only if numbers justify" decision (Phase 7).

#include "kalshi/gateway.hpp"
#include "kalshi/orderbook.hpp"
#include "trading/storage.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

int main(int argc, char** argv) {
  const int n = argc > 1 ? std::atoi(argv[1]) : 500000;

  kalshi::KalshiRawDecoder dec;
  kalshi::OrderBookManager books;
  const trading::EntityId A = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-A");

  // Seed via a snapshot record.
  {
    trading::RawRecord r;
    r.source = trading::SourceId::Kalshi;
    r.source_ticker = "MKT-A";
    r.source_stream_id = 7;
    r.source_sequence = 0;
    r.raw = R"({"type":"orderbook_snapshot","sid":7,"seq":0,"msg":{"market_ticker":"MKT-A","yes_dollars_fp":[["0.5000","1000000.00"]],"no_dollars_fp":[]}})";
    auto ev = dec.decode(r);
    const auto& s = std::get<trading::BookSnapshot>(ev->payload);
    books.bind(7, 1, A);
    books.on_snapshot(7, 1, A, kalshi::SnapshotView{s.yes, s.no, 0}, 0);
  }

  // Pre-build a pool of delta message strings (alternating +/- at 0.5000).
  std::vector<std::string> msgs;
  msgs.reserve(64);
  for (int i = 0; i < 64; ++i) {
    msgs.push_back(std::string(R"({"type":"orderbook_delta","sid":7,"seq":SEQ,"msg":{"market_ticker":"MKT-A","price_dollars":"0.5000","delta_fp":")") +
                   ((i & 1) ? "10.00" : "-10.00") + R"(","side":"yes"}})");
  }

  std::uint64_t seq = 0, ok = 0;
  const auto t0 = std::chrono::steady_clock::now();
  for (int i = 0; i < n; ++i) {
    ++seq;
    // Substitute the running seq into the template.
    std::string m = msgs[i & 63];
    const auto pos = m.find("SEQ");
    m.replace(pos, 3, std::to_string(seq));

    trading::RawRecord r;
    r.source = trading::SourceId::Kalshi;
    r.source_ticker = "MKT-A";
    r.source_stream_id = 7;
    r.source_sequence = seq;
    r.raw = std::move(m);
    if (auto ev = dec.decode(r); ev && ev->kind() == trading::Kind::BookDelta) {
      const auto& d = std::get<trading::BookDelta>(ev->payload);
      if (books.on_delta(7, 1, A, d.side, d.price, d.delta, seq) == kalshi::ApplyResult::Ok) ++ok;
    }
  }
  const auto t1 = std::chrono::steady_clock::now();
  const double ns = std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count();

  std::printf("ws decode+apply: %d msgs, %llu applied, %.1f ns/msg (%.2f M/s)\n", n,
              static_cast<unsigned long long>(ok), ns / n, n / (ns / 1e9) / 1e6);
  std::printf("(compare bench_orderbook = apply-only; the delta is JSON decode)\n");
  return 0;
}
