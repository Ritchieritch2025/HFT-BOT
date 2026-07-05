// Deterministic fuzzer for our parsing surfaces (run under ASan+UBSan):
// KalshiRawDecoder::decode and RawLogReader must never crash or exhibit UB on
// arbitrary/malformed input — they may only return nullopt / skip. (The RFC6455
// frame parser is ixwebsocket's, battle-tested; our attack surface is the JSON
// decode + the NDJSON reader.)
//
// usage: fuzz_decode [iters] [scratch_dir]

#include "kalshi/gateway.hpp"
#include "kalshi/limits.hpp"
#include "kalshi/request_spec.hpp"
#include "trading/storage.hpp"

#include <cstdio>
#include <cstdint>
#include <string>

namespace {
std::uint64_t rng = 0x243F6A8885A308D3ULL;
std::uint64_t next() { rng = rng * 6364136223846793005ULL + 1442695040888963407ULL; return rng >> 17; }

std::string random_bytes(std::size_t max_len) {
  std::string s;
  const std::size_t n = next() % (max_len + 1);
  for (std::size_t i = 0; i < n; ++i) s += static_cast<char>(next() & 0xFF);
  return s;
}

// Structurally-plausible-but-hostile messages (valid JSON, bad values).
std::string hostile() {
  static const char* templates[] = {
    R"({"type":"orderbook_delta","msg":{"price_dollars":"","delta_fp":"","side":"yes"}})",
    R"({"type":"orderbook_delta","msg":{"price_dollars":"9.9999","delta_fp":"-99999999999","side":"maybe"}})",
    R"({"type":"orderbook_snapshot","msg":{"yes_dollars_fp":[["x","y"],[],["0.1"],["0.1","1.0","extra"]]}})",
    R"({"type":"orderbook_snapshot","msg":{"yes_dollars_fp":"not-an-array"}})",
    R"({"type":"ticker","msg":{"price_dollars":123,"volume_fp":null,"ts_ms":"soon"}})",
    R"({"type":"trade","msg":{"yes_price_dollars":"1.00001","count_fp":"1e9","taker_side":42}})",
    R"({"type":"market_lifecycle_v2","msg":{"event_type":12345}})",
    R"({"type":123})",
    R"({"msg":{}})",
    R"([])",
    R"({)",
    R"(null)",
    R"({"type":"orderbook_delta"})",
    // Account-limits / endpoint-cost shapes (T1 parsers): missing required
    // fields, wrong types, absurd numbers, malformed grants/costs.
    R"({"usage_tier":"basic","read":{"refill_rate":-1},"write":{},"grants":[]})",
    R"({"usage_tier":123,"read":{"refill_rate":"x","bucket_capacity":null},"write":{"refill_rate":9999999999999},"grants":"nope"})",
    R"({"read":{"refill_rate":1,"bucket_capacity":1},"write":{"refill_rate":1,"bucket_capacity":1},"grants":[{"exchange_instance":42}]})",
    R"({"default_cost":"free","endpoint_costs":[{"method":9,"path":null,"cost":"lots"},{}]})",
    R"({"default_cost":10,"endpoint_costs":"not-an-array"})",
  };
  const std::size_t n = sizeof(templates) / sizeof(templates[0]);
  return templates[next() % n];
}
}  // namespace

int main(int argc, char** argv) {
  const int iters = argc > 1 ? std::atoi(argv[1]) : 200000;
  const std::string dir = argc > 2 ? argv[2] : ".";

  kalshi::KalshiRawDecoder dec;
  for (int i = 0; i < iters; ++i) {
    trading::RawRecord r;
    r.source = trading::SourceId::Kalshi;
    r.source_ticker = "MKT";
    const std::string raw = (next() & 1) ? random_bytes(300) : hostile();
    r.raw = raw;
    (void)dec.decode(r);  // must not crash / no UB (ASan/UBSan enforce)

    // T1 metadata parsers over the same untrusted bytes.
    if (auto lim = kalshi::parse_account_limits(raw)) {
      (void)kalshi::limits_invariant_error(*lim);
      (void)kalshi::tier_table_warnings(*lim);
    }
    auto costs = kalshi::parse_endpoint_costs(raw);
    (void)costs.cost_for(kalshi::Method::Post, "/trade-api/v2/portfolio/orders");

    // T2 path parsing over untrusted bytes-as-path.
    (void)kalshi::normalize_endpoint_path(raw);
    (void)kalshi::strip_wire_prefix(raw);
    (void)kalshi::make_request_spec(kalshi::Method::Post, raw, costs);
  }

  // Fuzz the NDJSON reader with garbage + truncated lines.
  const std::string path = dir + "/fuzz_reader.ndjson";
  if (std::FILE* f = std::fopen(path.c_str(), "wb")) {
    for (int i = 0; i < 2000; ++i) {
      std::string line = (next() & 1) ? random_bytes(120) : hostile();
      for (char& c : line) if (c == '\n') c = ' ';  // keep it one line
      std::fwrite(line.data(), 1, line.size(), f);
      if (next() & 1) std::fputc('\n', f);  // sometimes omit newline (truncation)
    }
    std::fclose(f);
  }
  trading::RawLogReader reader(path);
  while (reader.next().has_value()) { /* consume; must not crash */ }

  std::printf("fuzz_decode OK: %d decode iters + reader sweep, no crash/UB\n", iters);
  return 0;
}
