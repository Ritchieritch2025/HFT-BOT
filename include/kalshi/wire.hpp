#pragma once
//
// Wire formats for the trading engine:
//
//   feed --MarketEvent--> 30 strategies --ExecPayload--> in-process ring --> submit lanes
//
// ExecPayload is the uniform order intent every strategy emits. Inside
// tradingd it crosses threads as a plain struct through Ring<> — the live
// order path never touches Redis. The same fixed-size packed bytes double as
// the tape/telemetry record for the cold path (Redis bulk strings are
// binary-safe): kEventChannel feeds the optional ingestd tape recorder and
// kResultList carries async execution telemetry. Consumers validate
// magic/version/size before trusting any frame that crossed a process
// boundary. Both dev (arm64 macOS) and deploy (x86-64 Linux) targets are
// little-endian; a big-endian port would need byte-order shims.

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <string_view>

namespace kalshi::wire {

// Redis keys/channels — COLD PATH ONLY (telemetry + optional tape recorder).
inline constexpr const char* kResultList = "exec:results";  // tradingd telemetry
inline constexpr const char* kEventChannel = "md:events";   // ingestd tape recorder
inline constexpr const char* kExecList = "exec:orders";     // legacy relay (unused)

inline constexpr std::uint8_t kExecMagic = 0xEB;
inline constexpr std::uint8_t kEventMagic = 0xED;
inline constexpr std::uint8_t kWireVersion = 1;

inline constexpr int kStrategySlots = 30;  // fixed strategy count, ids 0..29

enum : std::uint8_t { kActionBuy = 1, kActionSell = 2 };
enum : std::uint8_t { kSideYes = 1, kSideNo = 2 };
enum : std::uint8_t { kTypeLimit = 1, kTypeMarket = 2 };
enum : std::uint8_t { kEventTicker = 1, kEventTrade = 2 };

#pragma pack(push, 1)

// One order instruction. Exactly 80 bytes.
struct ExecPayload {
  std::uint8_t magic = kExecMagic;
  std::uint8_t version = kWireVersion;
  std::uint8_t strategy_id = 0;  // 0..29
  std::uint8_t action = 0;       // kActionBuy/kActionSell
  std::uint8_t side = 0;         // kSideYes/kSideNo
  std::uint8_t order_type = 0;   // kTypeLimit/kTypeMarket
  std::int32_t count = 0;        // contracts, > 0
  std::int32_t price_cents = 0;  // 1..99 for limit orders (price of `side`)
  std::uint64_t seq = 0;         // per-strategy monotonic; part of client_order_id
  std::uint64_t ts_ns = 0;       // decision time, epoch nanoseconds
  std::uint64_t ttl_ns = 0;      // execd drops the order if now-ts_ns > ttl_ns; 0 = never
  char ticker[42] = {};          // NUL-padded market ticker

  void set_ticker(std::string_view t) {
    std::memset(ticker, 0, sizeof(ticker));
    std::memcpy(ticker, t.data(), t.size() < sizeof(ticker) ? t.size() : sizeof(ticker) - 1);
  }
  std::string_view ticker_view() const {
    return {ticker, ::strnlen(ticker, sizeof(ticker))};
  }
};
static_assert(sizeof(ExecPayload) == 80);

// One normalized market-data update. Exactly 88 bytes.
struct MarketEvent {
  std::uint8_t magic = kEventMagic;
  std::uint8_t version = kWireVersion;
  std::uint8_t kind = kEventTicker;
  std::uint8_t pad_ = 0;
  std::int32_t yes_bid = -1;      // cents, -1 = absent
  std::int32_t yes_ask = -1;      // cents, -1 = absent
  std::int32_t last_price = -1;   // cents, -1 = absent
  std::int64_t volume = -1;
  std::int64_t open_interest = -1;
  std::uint64_t ts_ns = 0;        // ingest time, epoch nanoseconds
  std::uint64_t seq = 0;          // ingest sequence number
  char ticker[40] = {};

  void set_ticker(std::string_view t) {
    std::memset(ticker, 0, sizeof(ticker));
    std::memcpy(ticker, t.data(), t.size() < sizeof(ticker) ? t.size() : sizeof(ticker) - 1);
  }
  std::string_view ticker_view() const {
    return {ticker, ::strnlen(ticker, sizeof(ticker))};
  }
};
static_assert(sizeof(MarketEvent) == 88);

#pragma pack(pop)

inline std::string_view as_bytes(const ExecPayload& p) {
  return {reinterpret_cast<const char*>(&p), sizeof(p)};
}
inline std::string_view as_bytes(const MarketEvent& e) {
  return {reinterpret_cast<const char*>(&e), sizeof(e)};
}

// Frame validation. Returns nullptr (with a reason) rather than throwing:
// malformed frames on a shared queue must never take the consumer down.
inline const ExecPayload* decode_exec(std::string_view bytes, const char** why = nullptr) {
  const auto fail = [&](const char* r) {
    if (why) *why = r;
    return nullptr;
  };
  if (bytes.size() != sizeof(ExecPayload)) return fail("bad size");
  const auto* p = reinterpret_cast<const ExecPayload*>(bytes.data());
  if (p->magic != kExecMagic || p->version != kWireVersion) return fail("bad magic/version");
  if (p->strategy_id >= kStrategySlots) return fail("bad strategy_id");
  if (p->action != kActionBuy && p->action != kActionSell) return fail("bad action");
  if (p->side != kSideYes && p->side != kSideNo) return fail("bad side");
  if (p->order_type != kTypeLimit && p->order_type != kTypeMarket) return fail("bad type");
  if (p->count <= 0) return fail("bad count");
  if (p->order_type == kTypeLimit && (p->price_cents < 1 || p->price_cents > 99))
    return fail("bad price");
  const std::string_view t = p->ticker_view();
  if (t.empty()) return fail("empty ticker");
  for (char c : t) {
    const bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                    (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
    if (!ok) return fail("bad ticker char");
  }
  return p;
}

inline const MarketEvent* decode_event(std::string_view bytes) {
  if (bytes.size() != sizeof(MarketEvent)) return nullptr;
  const auto* e = reinterpret_cast<const MarketEvent*>(bytes.data());
  if (e->magic != kEventMagic || e->version != kWireVersion) return nullptr;
  return e;
}

// Deterministic UUID-format client_order_id from the payload identity, so a
// re-queued duplicate maps to the same id and Kalshi's idempotency rejects it.
inline std::string client_order_id(const ExecPayload& p) {
  // 128 bits: ts_ns (64) | strategy_id (8) | seq low bits (56), rendered in
  // UUID 8-4-4-4-12 form.
  const std::uint64_t a = p.ts_ns;
  const std::uint64_t b = (static_cast<std::uint64_t>(p.strategy_id) << 56) |
                          (p.seq & 0x00FF'FFFF'FFFF'FFFFULL);
  char buf[37];
  std::snprintf(buf, sizeof(buf), "%08x-%04x-%04x-%04x-%012llx",
                static_cast<std::uint32_t>(a >> 32),
                static_cast<unsigned>((a >> 16) & 0xFFFF),
                static_cast<unsigned>(a & 0xFFFF),
                static_cast<unsigned>(b >> 48),
                static_cast<unsigned long long>(b & 0xFFFF'FFFF'FFFFULL));
  return buf;
}

// Kalshi V2 order creation (the only creation endpoint since the 2026
// fixed-point migration removed POST /portfolio/orders).
inline constexpr const char* kCreateOrderPath = "/portfolio/events/orders";

// V2 order body. The V2 book is YES-normalized: buy-YES / sell-NO rest as
// bids, sell-YES / buy-NO as asks at the complementary price. Prices are
// fixed-point dollar strings; counts are fixed-point strings. Market orders
// map to marketable-limit IOC at the price extreme. Call only on a payload
// that passed decode_exec — the validated ticker charset is what makes
// plain concatenation safe.
inline std::string order_json(const ExecPayload& p, bool post_only = false) {
  const bool is_bid = (p.action == kActionBuy) == (p.side == kSideYes);
  int yes_cents;
  if (p.order_type == kTypeMarket) {
    yes_cents = is_bid ? 99 : 1;  // marketable-limit IOC
  } else {
    yes_cents = (p.side == kSideYes) ? p.price_cents : 100 - p.price_cents;
  }
  char price[16];
  std::snprintf(price, sizeof(price), "%d.%02d00", yes_cents / 100, yes_cents % 100);

  std::string j;
  j.reserve(288);
  j += R"({"ticker":")";
  j += p.ticker_view();
  j += R"(","side":")";
  j += is_bid ? "bid" : "ask";
  j += R"(","count":")";
  j += std::to_string(p.count);
  j += R"(","price":")";
  j += price;
  j += R"(","time_in_force":")";
  j += (p.order_type == kTypeMarket) ? "immediate_or_cancel" : "good_till_canceled";
  // taker_at_cross: a self-cross cancels the incoming order, never a resting
  // quote — protects queue position of anything already working.
  j += R"(","self_trade_prevention_type":"taker_at_cross")";
  if (post_only) j += R"(,"post_only":true)";
  j += R"(,"client_order_id":")";
  j += client_order_id(p);
  j += R"("})";
  return j;
}

}  // namespace kalshi::wire
