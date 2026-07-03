#pragma once
//
// Processing layer: a fixed set of 30 strategy slots (wire::kStrategySlots).
// stratd feeds every MarketEvent to every strategy on one dispatch thread;
// strategies emit ExecPayloads through ExecSink, which stamps identity fields
// and LPUSHes them onto the Redis execution list for execd.

#include "kalshi/wire.hpp"

#include <memory>
#include <vector>

namespace kalshi {

class ExecSink {
 public:
  virtual ~ExecSink() = default;
  // `p` must carry action/side/order_type/count/price_cents/ticker (+ ttl_ns
  // if the edge is perishable). strategy_id, seq, and ts_ns are stamped here.
  virtual void submit(std::uint8_t strategy_id, wire::ExecPayload p) = 0;
};

class IStrategy {
 public:
  virtual ~IStrategy() = default;
  virtual std::uint8_t id() const = 0;
  virtual const char* name() const = 0;
  // Runs on the single dispatch thread for every event. Must not block —
  // no I/O and no waiting; heavy models belong on their own thread feeding
  // conclusions back through shared state the on_event body reads.
  virtual void on_event(const wire::MarketEvent& event, ExecSink& sink) = 0;
};

// The fixed 30-slot roster: demo strategies in the first slots, idle
// placeholders (safe no-ops) in the rest. Replace slots with real strategies
// as they are written.
std::vector<std::unique_ptr<IStrategy>> make_strategies();

}  // namespace kalshi
