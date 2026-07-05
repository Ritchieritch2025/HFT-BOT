#pragma once
//
// Structured REST error (shared by RestApi + RequestExecutor). Extracted to its
// own header so the executor and the typed endpoint layer can both use it without
// a circular include.

#include <cstdint>
#include <string>

namespace kalshi {

struct ApiError {
  enum class Kind : std::uint8_t { Transport, Http, Kalshi };
  Kind kind = Kind::Transport;
  long http_status = 0;     // HTTP status for Http/Kalshi kinds
  int transport_code = 0;   // CURLcode for Transport kind
  std::string kalshi_code;  // e.g. "authentication_error" from the error body; or
                            // "reconcile_required" when an ambiguous write must be
                            // reconciled rather than resent (T4/T5)
  std::string message;
};

}  // namespace kalshi
