#pragma once
//
// Streaming (operator<<) for the Kalshi-side types (env, errors, orderbook,
// apply results). Opt-in <ostream> weight — include only where you print.

#include "kalshi/env.hpp"
#include "kalshi/orderbook.hpp"
#include "kalshi/rest_api.hpp"

#include <ostream>

namespace kalshi {

inline std::ostream& operator<<(std::ostream& os, Env e) { return os << to_string(e); }
inline std::ostream& operator<<(std::ostream& os, Mode m) { return os << to_string(m); }
inline std::ostream& operator<<(std::ostream& os, ApplyResult r) { return os << to_string(r); }

inline const char* to_string(ApiError::Kind k) {
  switch (k) {
    case ApiError::Kind::Transport: return "Transport";
    case ApiError::Kind::Http: return "Http";
    case ApiError::Kind::Kalshi: return "Kalshi";
  }
  return "?";
}

inline std::ostream& operator<<(std::ostream& os, const ApiError& e) {
  os << "ApiError{" << to_string(e.kind);
  if (e.http_status) os << " http=" << e.http_status;
  if (e.transport_code) os << " curl=" << e.transport_code;
  if (!e.kalshi_code.empty()) os << " code=" << e.kalshi_code;
  os << " msg=\"" << e.message << "\"}";
  return os;
}

inline std::ostream& operator<<(std::ostream& os, const OrderBook& b) {
  return os << b.dump_state();  // honors valid(); invalid prints INVALID
}

inline std::ostream& operator<<(std::ostream& os, const Runtime& rt) {
  return os << "Runtime{" << describe(rt) << "}";
}

}  // namespace kalshi
