// W1: GoldRecord layout contract test (PLAN_GOLD_DATA_CONTRACT §2.1, V11 half).
//
// Proves, at compile time and runtime:
//   - sizeof(GoldRecord) == 512 and every field offset matches the contract
//   - the platform is little-endian (the on-disk format is LE by definition)
//   - the layout matches the COMMITTED canonical JSON
//     (tests/fixtures/gold_layout.json) byte-for-byte — C++ is the writer of
//     that file (--dump), Python (tests/test_gold_dtype.py) is the second
//     reader; any drift on either side goes red.
//
// Usage: test_gold_layout [scratch_dir]        -> verify (ALL PASS / FAIL)
//        test_gold_layout --dump               -> (re)write the committed JSON
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>

#include "../include/trading/gold_record.hpp"

using trading::GoldRecord;
using trading::kDepth;

static int g_fail = 0;
static void check(const char* name, bool ok) {
  std::printf("%s: %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) ++g_fail;
}

// One canonical serialization, produced only here. Field order = declaration
// order. Format is stable and byte-exact so the committed file can be
// compared with a plain string equality.
static std::string layout_json() {
  std::ostringstream o;
  o << "{\n  \"struct\": \"GoldRecord\",\n  \"size\": " << sizeof(GoldRecord)
    << ",\n  \"endian\": \"little\",\n  \"depth\": " << kDepth
    << ",\n  \"fields\": {\n";
  const struct { const char* name; size_t off; size_t sz; } F[] = {
      {"ts_us", offsetof(GoldRecord, ts_us), sizeof(GoldRecord::ts_us)},
      {"stream_seq", offsetof(GoldRecord, stream_seq), sizeof(GoldRecord::stream_seq)},
      {"market_id", offsetof(GoldRecord, market_id), sizeof(GoldRecord::market_id)},
      {"event_type", offsetof(GoldRecord, event_type), sizeof(GoldRecord::event_type)},
      {"flags", offsetof(GoldRecord, flags), sizeof(GoldRecord::flags)},
      {"book_seq", offsetof(GoldRecord, book_seq), sizeof(GoldRecord::book_seq)},
      {"trade_yes_price_e4", offsetof(GoldRecord, trade_yes_price_e4),
       sizeof(GoldRecord::trade_yes_price_e4)},
      {"taker_side", offsetof(GoldRecord, taker_side), sizeof(GoldRecord::taker_side)},
      {"trade_qty_e4", offsetof(GoldRecord, trade_qty_e4), sizeof(GoldRecord::trade_qty_e4)},
      {"trade_id_hash", offsetof(GoldRecord, trade_id_hash), sizeof(GoldRecord::trade_id_hash)},
      {"bid_px_e4", offsetof(GoldRecord, bid_px_e4), sizeof(GoldRecord::bid_px_e4)},
      {"ask_px_e4", offsetof(GoldRecord, ask_px_e4), sizeof(GoldRecord::ask_px_e4)},
      {"bid_qty_e4", offsetof(GoldRecord, bid_qty_e4), sizeof(GoldRecord::bid_qty_e4)},
      {"ask_qty_e4", offsetof(GoldRecord, ask_qty_e4), sizeof(GoldRecord::ask_qty_e4)},
      {"bid_rest_qty_e4", offsetof(GoldRecord, bid_rest_qty_e4),
       sizeof(GoldRecord::bid_rest_qty_e4)},
      {"ask_rest_qty_e4", offsetof(GoldRecord, ask_rest_qty_e4),
       sizeof(GoldRecord::ask_rest_qty_e4)},
      {"bid_nlevels", offsetof(GoldRecord, bid_nlevels), sizeof(GoldRecord::bid_nlevels)},
      {"ask_nlevels", offsetof(GoldRecord, ask_nlevels), sizeof(GoldRecord::ask_nlevels)},
  };
  const size_t n = sizeof(F) / sizeof(F[0]);
  for (size_t i = 0; i < n; ++i) {
    o << "    \"" << F[i].name << "\": {\"offset\": " << F[i].off
      << ", \"size\": " << F[i].sz << "}" << (i + 1 < n ? "," : "") << "\n";
  }
  o << "  }\n}\n";
  return o.str();
}

static const char* kCanonical = "tests/fixtures/gold_layout.json";

int main(int argc, char** argv) {
  if (argc > 1 && std::strcmp(argv[1], "--dump") == 0) {
    std::ofstream f(kCanonical, std::ios::binary);
    f << layout_json();
    std::printf("wrote %s\n", kCanonical);
    return 0;
  }

  // R-confirmed arithmetic: 24 + 8 + 24 + 416 + 40 = 512.
  check("sizeof(GoldRecord) == 512", sizeof(GoldRecord) == 512);
  check("identity block ends at 24", offsetof(GoldRecord, book_seq) == 24);
  check("trade payload starts at 32", offsetof(GoldRecord, trade_yes_price_e4) == 32);
  check("book state starts at 56", offsetof(GoldRecord, bid_px_e4) == 56);
  check("reserved tail starts at 472", offsetof(GoldRecord, _reserved) == 472);
  check("reserved tail is 40 bytes", sizeof(GoldRecord::_reserved) == 40);

  const std::uint16_t probe = 0x0102;
  unsigned char b0;
  std::memcpy(&b0, &probe, 1);
  check("platform is little-endian", b0 == 0x02);

  std::ifstream f(kCanonical, std::ios::binary);
  check("committed layout JSON exists (tests/fixtures/gold_layout.json)",
        f.good());
  if (f.good()) {
    std::stringstream buf;
    buf << f.rdbuf();
    check("layout matches committed JSON byte-for-byte",
          buf.str() == layout_json());
  }

  std::printf(g_fail ? "TEST FAIL (%d)\n" : "ALL PASS\n", g_fail);
  return g_fail ? 1 : 0;
}
