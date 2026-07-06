// GoldRecord — the gold-standard research event record (PLAN_GOLD_DATA_CONTRACT
// §2.1, operator-approved 2026-07-06; audit G3 arithmetic R-confirmed).
//
// Source-agnostic layer: no kalshi/* types may appear here. 512-byte
// fixed-size little-endian records, zero-copy mmap-able; Python mirror in
// tools/gold_dtype.py; layout parity enforced by tests/test_gold_layout.cpp +
// tests/test_gold_dtype.py against the committed canonical JSON
// (tests/fixtures/gold_layout.json). Any layout change requires: bump
// GOLD_LAYOUT_VERSION, regenerate the canonical JSON (--dump), and rebuild —
// old gold files do not survive layout changes (they are derived data,
// rebuilt from the warehouse).
//
// Arithmetic (must stay true): ordering/identity 24 B + book_seq 8 B +
// trade payload 24 B + book state 416 B + reserved 40 B = 512 B.
#pragma once
#include <cstddef>
#include <cstdint>

namespace trading {

inline constexpr int kDepth = 16;
inline constexpr std::uint32_t kGoldLayoutVersion = 1;

enum GoldEventType : std::uint16_t {
  BOOK_SNAPSHOT = 1,
  BOOK_DELTA = 2,
  TRADE = 3,
  L1_TICKER = 4,
  HEARTBEAT = 5,
};

enum GoldFlags : std::uint16_t {
  F_BOOK_VALID = 1u << 0,
  F_BOOK_COVERED = 1u << 1,
  F_CROSSED = 1u << 2,
  F_FROM_SNAPSHOT = 1u << 3,
  F_TRADE_RACED = 1u << 4,
};

struct GoldRecord {
  // ordering / identity (24 B)
  std::uint64_t ts_us;        // capture epoch µs — the only clock
  std::uint64_t stream_seq;   // global monotonic merge order (total-order tie-break)
  std::uint32_t market_id;    // dense per-day id -> sidecar dim. DAY-SCOPED:
                              // cross-day joins on market_id alone are FORBIDDEN;
                              // cross-day access requires (date, market_id) or ticker.
  std::uint16_t event_type;   // GoldEventType
  std::uint16_t flags;        // GoldFlags
  // join key (8 B)
  std::uint64_t book_seq;     // per-market monotonic book version this record sees.
                              // JOIN KEY = (market_id, book_seq). Never join on time.
  // trade payload, zero unless TRADE (24 B)
  std::int32_t trade_yes_price_e4;
  std::uint8_t taker_side;    // 0=none 1=yes 2=no
  std::uint8_t _pad[3];
  std::int64_t trade_qty_e4;
  std::uint64_t trade_id_hash;  // FNV-1a-64(trade_id) — R decision 2026-07-06:
                                // zero-dependency in both languages; full UUID in
                                // sidecar keyed by stream_seq; V8 collision
                                // reconciliation mandatory.
  // book state as-of this record, yes-space, best-first (416 B)
  std::int32_t bid_px_e4[kDepth];   // from yes_levels (Yes bids)
  std::int32_t ask_px_e4[kDepth];   // = 10000 - no_price, from no_levels (No bids)
  std::int64_t bid_qty_e4[kDepth];
  std::int64_t ask_qty_e4[kDepth];
  std::int64_t bid_rest_qty_e4;     // tail beyond kDepth — aggregated, never dropped silently
  std::int64_t ask_rest_qty_e4;
  std::uint16_t bid_nlevels;
  std::uint16_t ask_nlevels;
  std::uint8_t _pad2[12];
  // reserved tail (40 B) — lands the struct exactly on 512
  std::uint64_t _reserved[5];
};

static_assert(sizeof(GoldRecord) == 512, "GoldRecord must be exactly 512 bytes");
static_assert(offsetof(GoldRecord, book_seq) == 24, "identity block is 24 B");
static_assert(offsetof(GoldRecord, trade_yes_price_e4) == 32, "trade payload at 32");
static_assert(offsetof(GoldRecord, bid_px_e4) == 56, "book state at 56");
static_assert(offsetof(GoldRecord, _reserved) == 472, "reserved tail at 472");
static_assert(sizeof(GoldRecord::_reserved) == 40, "reserved tail is 40 B");

// FNV-1a-64 (R decision 2026-07-06): deterministic, dependency-free, mirrored
// in tools/gold_dtype.py. Weak alone — V8 reconciles every hash against the
// sidecar UUIDs each build, so collisions are detected, not assumed away.
inline constexpr std::uint64_t fnv1a64(const char* s, std::size_t n) {
  std::uint64_t h = 1469598103934665603ull;         // FNV offset basis
  for (std::size_t i = 0; i < n; ++i) {
    h ^= static_cast<std::uint8_t>(s[i]);
    h *= 1099511628211ull;                          // FNV prime
  }
  return h;
}

}  // namespace trading
