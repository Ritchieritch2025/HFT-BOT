#pragma once
//
// Environment selection and hard safety gates. Fail-closed by construction:
//   - default is the safest runtime (local_mock + data_collect), never prod/live;
//   - prod and live each require an explicit KALSHI_ALLOW_* env var or resolution
//     throws;
//   - KALSHI_BASE_URL is cross-validated against KALSHI_ENV by a host allowlist
//     (both directions) so a "prod" collector can never silently read demo data,
//     and non-TLS is permitted only for the local mock;
//   - order transmission anywhere must pass require_orders_allowed(), which throws
//     unless live is explicitly enabled (Shadow never passes — it logs, never sends).
//
// Nothing here logs API keys, signatures, or any secret.

#include <stdexcept>
#include <string>
#include <string_view>

namespace kalshi {

enum class Env : std::uint8_t { LocalMock, Demo, Prod };
enum class Mode : std::uint8_t { DataCollect, Shadow, Live };

inline const char* to_string(Env e) {
  switch (e) {
    case Env::LocalMock: return "local_mock";
    case Env::Demo: return "demo";
    case Env::Prod: return "prod";
  }
  return "local_mock";
}
inline const char* to_string(Mode m) {
  switch (m) {
    case Mode::DataCollect: return "data_collect";
    case Mode::Shadow: return "shadow";
    case Mode::Live: return "live";
  }
  return "data_collect";
}

// Thrown by resolve_runtime() on any unsafe/ambiguous configuration, and by
// require_orders_allowed() when a transmit is attempted without live enabled.
struct SafetyViolation : std::runtime_error {
  using std::runtime_error::runtime_error;
};

struct Runtime {
  Env env = Env::LocalMock;
  Mode mode = Mode::DataCollect;
  std::string rest_base_url;   // validated against env
  std::string rest_prefix = "/trade-api/v2";
  std::string ws_url;          // resolved for the next (WS) pass; unused now
  std::string ws_sign_path = "/trade-api/ws/v2";
  bool read_only = true;       // mode != Live
  bool orders_enabled = false; // Live && allow_live && env in {Demo,Prod}
  bool shadow = false;         // mode == Shadow
};

// Reads KALSHI_ENV / KALSHI_MODE / KALSHI_ALLOW_PROD / KALSHI_ALLOW_LIVE /
// KALSHI_BASE_URL / KALSHI_MOCK_PORT / KALSHI_HOST_UNSAFE_OVERRIDE and returns a
// validated Runtime. Throws SafetyViolation on any unsafe/ambiguous config.
Runtime resolve_runtime();

// --- gates (pure, header-inline) ---

inline bool is_read_only(const Runtime& rt) { return rt.read_only; }
inline bool is_shadow(const Runtime& rt) { return rt.shadow; }
inline bool can_place_orders(const Runtime& rt) { return rt.orders_enabled; }

// The single choke point every order-transmit path must call first.
inline void require_orders_allowed(const Runtime& rt) {
  if (!rt.orders_enabled) {
    throw SafetyViolation(
        std::string("order transmission refused: env=") + to_string(rt.env) +
        " mode=" + to_string(rt.mode) +
        " (live mode + KALSHI_ALLOW_LIVE required; shadow/data_collect never transmit)");
  }
}

// Redaction for anything that might carry a secret. Never print raw keys/sigs.
inline std::string redact(std::string_view) { return "***"; }

// Human-readable, secret-free one-liner for startup logs.
std::string describe(const Runtime& rt);

}  // namespace kalshi
