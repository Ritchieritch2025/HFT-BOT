// Env/safety tests: fail-closed defaults, prod/live gating, host allowlist
// cross-validation (both directions), TLS rule, and the order gate. Sets env
// vars per-case with setenv and re-resolves.

#include "kalshi/env.hpp"

#include <cstdlib>
#include <iostream>
#include <string>

using namespace kalshi;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

// Clear every env var the resolver reads, then set the given ones.
void reset_env() {
  for (const char* k :
       {"KALSHI_ENV", "KALSHI_MODE", "KALSHI_ALLOW_PROD", "KALSHI_ALLOW_LIVE",
        "KALSHI_BASE_URL", "KALSHI_MOCK_PORT", "KALSHI_HOST_UNSAFE_OVERRIDE",
        "KALSHI_WS_URL", "KALSHI_WS_SIGN_PATH"})
    ::unsetenv(k);
}
void set(const char* k, const char* v) { ::setenv(k, v, 1); }

bool throws() {
  try {
    resolve_runtime();
    return false;
  } catch (const SafetyViolation&) {
    return true;
  }
}
}  // namespace

int main() {
  // Default = safest.
  reset_env();
  {
    Runtime rt = resolve_runtime();
    check(rt.env == Env::LocalMock, "default env is local_mock (not prod)");
    check(rt.mode == Mode::DataCollect, "default mode is data_collect");
    check(rt.read_only, "default is read_only");
    check(!rt.orders_enabled, "default orders blocked");
    check(!can_place_orders(rt), "can_place_orders false by default");
    check(rt.rest_base_url.rfind("http://127.0.0.1", 0) == 0, "default base is localhost");
  }

  // Prod requires explicit allow.
  reset_env(); set("KALSHI_ENV", "prod");
  check(throws(), "prod without KALSHI_ALLOW_PROD throws");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  check(!throws(), "prod + allow resolves");

  // Live requires explicit allow + non-local env.
  reset_env(); set("KALSHI_MODE", "live");
  check(throws(), "live without KALSHI_ALLOW_LIVE throws");
  reset_env(); set("KALSHI_MODE", "live"); set("KALSHI_ALLOW_LIVE", "1");
  check(throws(), "live in local_mock throws even with allow");
  reset_env(); set("KALSHI_ENV", "demo"); set("KALSHI_MODE", "live"); set("KALSHI_ALLOW_LIVE", "1");
  {
    Runtime rt = resolve_runtime();
    check(rt.orders_enabled, "demo + live + allow => orders_enabled");
    check(!rt.read_only, "live is not read_only");
    bool gate_ok = true;
    try { require_orders_allowed(rt); } catch (...) { gate_ok = false; }
    check(gate_ok, "require_orders_allowed passes in live");
  }

  // Prod + Shadow: consumes data, never transmits.
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1"); set("KALSHI_MODE", "shadow");
  {
    Runtime rt = resolve_runtime();
    check(rt.env == Env::Prod && rt.shadow, "prod+shadow resolves");
    check(rt.read_only, "prod+shadow is read_only");
    check(!rt.orders_enabled, "prod+shadow orders blocked");
    bool threw = false;
    try { require_orders_allowed(rt); } catch (const SafetyViolation&) { threw = true; }
    check(threw, "prod+shadow: require_orders_allowed throws (never transmit)");
  }

  // Unknown env/mode fail closed.
  reset_env(); set("KALSHI_ENV", "staging");
  check(throws(), "unknown env throws (fail closed)");
  reset_env(); set("KALSHI_MODE", "yolo");
  check(throws(), "unknown mode throws (fail closed)");

  // Host allowlist cross-validation (both directions).
  reset_env(); set("KALSHI_ENV", "demo"); set("KALSHI_BASE_URL", "https://api.elections.kalshi.com");
  check(throws(), "prod host under demo env throws");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "https://external-api.demo.kalshi.co");
  check(throws(), "demo host under prod env throws");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "https://external-api.kalshi.com");
  check(!throws(), "documented alternate prod host accepted");

  // TLS rule: non-https only for local_mock localhost.
  reset_env(); set("KALSHI_ENV", "demo"); set("KALSHI_BASE_URL", "http://external-api.demo.kalshi.co");
  check(throws(), "http host under demo throws (TLS rule)");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "http://api.elections.kalshi.com");
  check(throws(), "http under prod throws even for allowlisted host");
  reset_env(); set("KALSHI_BASE_URL", "http://127.0.0.1:9999");
  check(!throws(), "http localhost accepted under local_mock");

  // Unsafe override: bypasses allowlist, not TLS, refused in live.
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "https://some-new-host.kalshi.com"); set("KALSHI_HOST_UNSAFE_OVERRIDE", "1");
  check(!throws(), "override bypasses host allowlist (https)");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "http://some-new-host.kalshi.com"); set("KALSHI_HOST_UNSAFE_OVERRIDE", "1");
  check(throws(), "override does NOT bypass TLS rule");
  reset_env(); set("KALSHI_ENV", "demo"); set("KALSHI_MODE", "live"); set("KALSHI_ALLOW_LIVE", "1");
  set("KALSHI_BASE_URL", "https://some-new-host.kalshi.com"); set("KALSHI_HOST_UNSAFE_OVERRIDE", "1");
  check(throws(), "override refused in live mode");

  // Secret redaction.
  check(redact("KALSHI-ACCESS-KEY: abc123") == "***", "redact hides secrets");

  reset_env();
  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
