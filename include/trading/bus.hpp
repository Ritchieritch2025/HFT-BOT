#pragma once
//
// Source-agnostic trading bus. Nothing here knows about Kalshi (or any specific
// venue): a source gateway translates its native wire format into these types,
// and everything downstream consumes only these. If a `kalshi_`-anything field
// ever appears in this header, the layer boundary has been breached.
//
// Pipeline:
//   DataSource -> NormalizedEvent -> MarketDataSink -> FeatureBuilder ->
//   FeatureVector -> Model -> Signal -> OrderIntent -> ExecutionEngine
//
// trace_id is minted at the NormalizedEvent (the *triggering* event) and copied
// forward into FeatureVector / Signal / OrderIntent for end-to-end correlation.
//
// THREADING CONTRACT (binding on the future WS pass): single-threaded. A
// DataSource calls its sink synchronously on the source's own thread; sink
// callees must not block. Any view a NormalizedEvent exposes (raw_payload) is
// valid only until the sink call returns. The WS pass inserts an SPSC ring
// (ring.hpp) between the socket thread and the bus thread behind this same
// contract — the interface below does not change.

#include "trading/fixedpoint.hpp"
#include "trading/ids.hpp"
#include "trading/timestamp.hpp"

#include <cstdint>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>
#include <variant>
#include <vector>

namespace trading {

// Binary prediction-market outcome side. Generic across prediction markets
// (a "will X happen" market has a Yes and a No side); NOT a venue wire term.
enum class Side : std::uint8_t { Yes, No };
inline const char* to_string(Side s) { return s == Side::Yes ? "Yes" : "No"; }

struct Level {
  PriceE4 price = 0;
  CountFp size = 0;
};

// --- NormalizedFields variant alternatives (the cross-source schema) ---
// Keep minimal and venue-neutral. A new source adds an alternative only when a
// concept is genuinely generic. Order MUST match enum Kind below.

struct BookSnapshot {
  std::vector<Level> yes;  // resting orders on the Yes side, ascending price
  std::vector<Level> no;   // resting orders on the No side, ascending price
};
struct BookDelta {
  Side side = Side::Yes;
  PriceE4 price = 0;
  CountFp delta = 0;  // signed change to the resting size at `price`
  // Passthrough only: present on deltas caused by YOUR OWN order (rare on a
  // market-data feed, so no allocation on the common path). Not interpreted.
  std::optional<std::string> client_order_id;
};
struct Ticker {
  std::optional<PriceE4> last_price;
  std::optional<PriceE4> yes_bid;
  std::optional<PriceE4> yes_ask;
  std::optional<CountFp> volume;
  std::optional<CountFp> open_interest;
};
struct Trade {
  PriceE4 price = 0;             // yes-price in probability space
  CountFp size = 0;
  Side taker_side = Side::Yes;   // taker outcome side
  std::string trade_id;          // dedupe key (trades carry no seq)
};
// Market/event lifecycle. Generic states plus an Unknown escape hatch carrying
// the raw type string — the venue's event_type set is OPEN (types added AND
// removed within 2026), so unknown types must round-trip + count, never crash.
struct Lifecycle {
  enum class State : std::uint8_t {
    Unknown, Created, Open, Paused, Closed, Determined, Settled
  };
  State state = State::Unknown;
  std::string unknown_type;                 // raw type when state == Unknown
  std::optional<PriceE4> settlement_value;  // when Determined/Settled
};

inline const char* to_string(Lifecycle::State s) {
  switch (s) {
    case Lifecycle::State::Unknown: return "Unknown";
    case Lifecycle::State::Created: return "Created";
    case Lifecycle::State::Open: return "Open";
    case Lifecycle::State::Paused: return "Paused";
    case Lifecycle::State::Closed: return "Closed";
    case Lifecycle::State::Determined: return "Determined";
    case Lifecycle::State::Settled: return "Settled";
  }
  return "Unknown";
}

using NormalizedFields =
    std::variant<BookSnapshot, BookDelta, Ticker, Trade, Lifecycle>;

enum class Kind : std::uint8_t { BookSnapshot, BookDelta, Ticker, Trade, Lifecycle };
inline const char* to_string(Kind k) {
  switch (k) {
    case Kind::BookSnapshot: return "BookSnapshot";
    case Kind::BookDelta: return "BookDelta";
    case Kind::Ticker: return "Ticker";
    case Kind::Trade: return "Trade";
    case Kind::Lifecycle: return "Lifecycle";
  }
  return "?";
}

// Non-owning view of the original source bytes, valid only until the sink
// returns (see threading contract). The durable copy is the RawLog, not this.
struct RawPayloadView {
  std::string_view bytes;
  bool empty() const { return bytes.empty(); }
};

struct NormalizedEvent {
  TraceId trace_id;                       // minted here
  SourceId source = SourceId::Unknown;    // never Unknown past the gateway
  EntityId entity_id;                     // deterministic hash id
  std::optional<std::int64_t> source_event_time_ms;
  std::int64_t local_receive_mono_ns = 0;
  std::int64_t local_receive_wall_ns = 0;
  std::int64_t publish_time_ns = 0;
  std::optional<std::uint64_t> source_sequence;   // per-sid seq (orderbook only)
  std::optional<std::uint64_t> source_stream_id;  // sid: the subscription stream
  std::uint32_t stream_epoch = 0;                 // bumps every (re)connection;
                                                  // consumers drop epoch mismatches
  RawPayloadView raw_payload;
  NormalizedFields payload;

  // Single source of truth: kind is DERIVED from the active alternative, so a
  // stray enum field can never disagree with the payload.
  Kind kind() const { return static_cast<Kind>(payload.index()); }
};

// --- downstream value types (each carries the trace forward) ---

struct FeatureVector {
  EntityId entity_id;
  TraceId trace_id;  // trace of the triggering event
  std::int64_t built_mono_ns = 0;
  std::vector<double> values;  // model inputs (features, not prices)
  // Reserved for aggregating builders that need contributing-event lineage:
  //   std::vector<TraceId> parents;  // not implemented this pass
};

struct Signal {
  EntityId entity_id;
  TraceId trace_id;
  std::int64_t decided_mono_ns = 0;
  Side side = Side::Yes;
  double strength = 0.0;  // pre-order direction/conviction, not an order yet
};

struct OrderIntent {
  EntityId entity_id;
  TraceId trace_id;
  std::int64_t created_mono_ns = 0;
  Side side = Side::Yes;
  PriceE4 limit_price = 0;
  CountFp size = 0;
  enum class TimeInForce : std::uint8_t { IOC, GTC } tif = TimeInForce::GTC;
  bool post_only = false;
};

// --- interfaces ---

struct EventSink {
  virtual ~EventSink() = default;
  virtual void on_event(const NormalizedEvent&) = 0;
};
using MarketDataSink = EventSink;  // market data flows as NormalizedEvents

struct DataSource {
  virtual ~DataSource() = default;
  virtual SourceId id() const = 0;
  virtual void set_sink(MarketDataSink* sink) = 0;
  virtual void start() = 0;
  virtual void stop() = 0;
};

struct FeatureBuilder {
  virtual ~FeatureBuilder() = default;
  virtual std::optional<FeatureVector> on_event(const NormalizedEvent&) = 0;
  virtual std::string dump_state() const = 0;
};

struct Model {
  virtual ~Model() = default;
  virtual std::optional<Signal> on_features(const FeatureVector&) = 0;
  virtual std::string dump_state() const = 0;
};

enum class ExecDecision : std::uint8_t { Rejected, Logged, Transmitted };
inline const char* to_string(ExecDecision d) {
  switch (d) {
    case ExecDecision::Rejected: return "Rejected";
    case ExecDecision::Logged: return "Logged";
    case ExecDecision::Transmitted: return "Transmitted";
  }
  return "?";
}

// Every OrderIntent, in every mode, terminates here. Concrete engines (shadow /
// data-collect / live) live in the Kalshi gateway (they depend on env/safety).
struct ExecutionEngine {
  virtual ~ExecutionEngine() = default;
  virtual ExecDecision submit(const OrderIntent&) = 0;
};

// --- EntityRegistry: (SourceId, source_ticker) <-> deterministic EntityId ---

class EntityRegistry {
 public:
  // Registers (or returns existing) EntityId for a source ticker. Rejects
  // Unknown; a hash collision between two distinct keys is a hard error.
  EntityId register_entity(SourceId src, std::string_view ticker) {
    if (src == SourceId::Unknown)
      throw std::invalid_argument("EntityRegistry: SourceId::Unknown rejected");
    const EntityId id = make_entity_id(src, ticker);
    const Key key{src, std::string(ticker)};
    auto it = by_id_.find(id.v);
    if (it != by_id_.end()) {
      if (!(it->second == key))
        throw std::runtime_error("EntityRegistry: EntityId hash collision between '" +
                                 it->second.ticker + "' and '" + key.ticker + "'");
      return id;  // idempotent
    }
    by_id_.emplace(id.v, key);
    return id;
  }

  std::optional<EntityId> lookup(SourceId src, std::string_view ticker) const {
    if (src == SourceId::Unknown) return std::nullopt;
    const EntityId id = make_entity_id(src, ticker);
    return by_id_.count(id.v) ? std::optional<EntityId>(id) : std::nullopt;
  }

  struct Ref {
    SourceId source;
    std::string ticker;
  };
  std::optional<Ref> reverse(EntityId id) const {
    auto it = by_id_.find(id.v);
    if (it == by_id_.end()) return std::nullopt;
    return Ref{it->second.source, it->second.ticker};
  }

  std::size_t size() const { return by_id_.size(); }

 private:
  struct Key {
    SourceId source;
    std::string ticker;
    bool operator==(const Key& o) const {
      return source == o.source && ticker == o.ticker;
    }
  };
  std::unordered_map<std::uint64_t, Key> by_id_;
};

}  // namespace trading
