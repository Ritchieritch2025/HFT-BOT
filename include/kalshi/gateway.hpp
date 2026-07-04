#pragma once
//
// Kalshi gateway: the ONLY place Kalshi-specific wire formats are understood.
// Everything it produces is a source-agnostic trading::NormalizedEvent — no
// Kalshi field names cross into include/trading/.
//
// This header declares the raw-payload decoder (Kalshi JSON -> NormalizedEvent)
// used by replay and by the live data source. The live KalshiDataSource and the
// execution engines are added on top (see gateway.cpp / P7).

#include "kalshi/env.hpp"
#include "trading/bus.hpp"
#include "trading/storage.hpp"

#include <cstdint>
#include <optional>
#include <string>

namespace kalshi {

// Decodes a stored Kalshi raw payload (orderbook_snapshot / orderbook_delta /
// ticker) into a NormalizedEvent. Prices are converted to trading::PriceE4
// probability space here, at the gateway boundary. Returns nullopt for message
// types not modeled.
class KalshiRawDecoder : public trading::RawDecoder {
 public:
  std::optional<trading::NormalizedEvent> decode(const trading::RawRecord&) override;
};

// Mode-aware execution engine. Every OrderIntent terminates here and the
// decision is returned (so tests assert on values, not logs). This pass wires
// NO real transmission — the live path fails closed.
//   DataCollect -> Rejected (loud; the pipeline should never produce intents here)
//   Shadow      -> Logged (would-be-order NDJSON record; never transmits)
//   Live        -> require_orders_allowed() (throws unless live enabled), then
//                  the transmit skeleton is NOT wired -> throws (fail closed)
class KalshiExecutionEngine : public trading::ExecutionEngine {
 public:
  explicit KalshiExecutionEngine(Runtime rt, std::string would_be_order_log = "")
      : rt_(std::move(rt)), log_path_(std::move(would_be_order_log)) {}

  trading::ExecDecision submit(const trading::OrderIntent&) override;

  std::uint64_t rejected() const { return rejected_; }
  std::uint64_t logged() const { return logged_; }
  std::uint64_t transmitted() const { return transmitted_; }  // always 0 this pass

 private:
  void write_would_be(const trading::OrderIntent&);

  Runtime rt_;
  std::string log_path_;
  std::uint64_t rejected_ = 0;
  std::uint64_t logged_ = 0;
  std::uint64_t transmitted_ = 0;
};

}  // namespace kalshi
