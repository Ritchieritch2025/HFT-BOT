#pragma once
//
// Processing layer. tradingd feeds every MarketEvent to each configured
// strategy on one dispatch thread; strategies emit ExecPayloads through
// ExecSink, which stamps identity fields and hands them to the in-process
// submit ring.

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

// Configured strategy roster. Empty until real strategies are installed.
std::vector<std::unique_ptr<IStrategy>> make_strategies();

}  // namespace kalshi
