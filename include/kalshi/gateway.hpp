#pragma once
//
// Kalshi gateway: the ONLY place Kalshi-specific wire formats are understood.
// Everything it produces is a source-agnostic trading::NormalizedEvent — no
// Kalshi field names cross into include/trading/.
//
// This header declares the raw-payload decoder (Kalshi JSON -> NormalizedEvent)
// used by replay and by the live data source. The live KalshiDataSource and the
// execution engines are added on top (see gateway.cpp / P7).

#include "trading/bus.hpp"
#include "trading/storage.hpp"

#include <optional>

namespace kalshi {

// Decodes a stored Kalshi raw payload (orderbook_snapshot / orderbook_delta /
// ticker) into a NormalizedEvent. Prices are converted to trading::PriceE4
// probability space here, at the gateway boundary. Returns nullopt for message
// types not modeled.
class KalshiRawDecoder : public trading::RawDecoder {
 public:
  std::optional<trading::NormalizedEvent> decode(const trading::RawRecord&) override;
};

}  // namespace kalshi
