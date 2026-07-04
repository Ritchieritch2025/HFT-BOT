#pragma once
//
// Source-agnostic identity types for the trading bus.
//
// SourceId    — which upstream produced an event. Unknown = 0 fails closed:
//               registries, decoders, and sinks reject it.
// EntityId    — a normalized instrument id, DETERMINISTIC across process runs:
//               FNV-1a hash of (SourceId byte, source_ticker). Normalized logs
//               are therefore unambiguous across restarts without persisting a
//               registry, and replay reproduces the same ids.
// TraceId     — a per-session monotonic id minted at NormalizedEvent and
//               threaded through FeatureVector -> Signal -> OrderIntent.

#include <atomic>
#include <cstdint>
#include <functional>
#include <string_view>

namespace trading {

enum class SourceId : std::uint8_t {
  Unknown = 0,
  Kalshi,
  Replay,       // reserved for genuinely synthetic streams (NOT replayed real data)
  SportsOdds,
  SportsStats,
  News,
  Weather,
  Custom,
};

inline const char* to_string(SourceId s) {
  switch (s) {
    case SourceId::Unknown: return "Unknown";
    case SourceId::Kalshi: return "Kalshi";
    case SourceId::Replay: return "Replay";
    case SourceId::SportsOdds: return "SportsOdds";
    case SourceId::SportsStats: return "SportsStats";
    case SourceId::News: return "News";
    case SourceId::Weather: return "Weather";
    case SourceId::Custom: return "Custom";
  }
  return "Unknown";
}

struct EntityId {
  std::uint64_t v = 0;
  friend bool operator==(EntityId a, EntityId b) { return a.v == b.v; }
  friend bool operator!=(EntityId a, EntityId b) { return a.v != b.v; }
  friend bool operator<(EntityId a, EntityId b) { return a.v < b.v; }
};

// Deterministic 64-bit FNV-1a over (source byte || ticker). Stable across runs,
// machines, and endianness (byte-wise). Never returns 0 for a valid input
// (0 is reserved for "no entity").
inline EntityId make_entity_id(SourceId src, std::string_view ticker) {
  std::uint64_t h = 1469598103934665603ULL;  // FNV offset basis
  const std::uint64_t prime = 1099511628211ULL;
  h ^= static_cast<std::uint8_t>(src);
  h *= prime;
  for (unsigned char c : ticker) {
    h ^= c;
    h *= prime;
  }
  if (h == 0) h = prime;  // never collide with the "no entity" sentinel
  return EntityId{h};
}

struct TraceId {
  std::uint64_t v = 0;
  friend bool operator==(TraceId a, TraceId b) { return a.v == b.v; }
};

// Per-session monotonic. Not derived from wall clock or randomness (both are
// unavailable in some sandboxes and would break determinism of tests).
inline TraceId next_trace_id() {
  static std::atomic<std::uint64_t> counter{1};
  return TraceId{counter.fetch_add(1, std::memory_order_relaxed)};
}

}  // namespace trading

template <>
struct std::hash<trading::EntityId> {
  std::size_t operator()(trading::EntityId e) const noexcept {
    return static_cast<std::size_t>(e.v);
  }
};
