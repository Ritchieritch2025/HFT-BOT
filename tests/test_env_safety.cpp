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
  {
    // Prod DEFAULT host is the canonical external-api.kalshi.com (F1); the
    // api.elections.kalshi.com compatibility host stays accepted (below).
    Runtime rt = resolve_runtime();
    check(rt.rest_base_url == "https://external-api.kalshi.com", "prod default host is external-api");
  }
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "https://api.elections.kalshi.com");
  check(!throws(), "api.elections compatibility host still accepted under prod");

  // Live requires explicit allow + non-local env.
  reset_env(); set("KALSHI_MODE", "live");
  check(throws(), "live without KALSHI_ALLOW_LIVE throws");
  reset_env(); set("KALSHI_MODE", "live"); set("KALSHI_ALLOW_LIVE", "1");
  check(throws(), "live in local_mock throws even with allow");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_MODE", "live"); set("KALSHI_ALLOW_LIVE", "1");
  {
    Runtime rt = resolve_runtime();
    check(rt.orders_enabled, "prod + live + allow => orders_enabled");
    check(!rt.read_only, "live is not read_only");
    bool gate_ok = true;
    try { require_orders_allowed(rt); } catch (...) { gate_ok = false; }
    check(gate_ok, "require_orders_allowed passes in live");
  }

  // Demo env is no longer supported: always fail-closed, even with allows set.
  reset_env(); set("KALSHI_ENV", "demo");
  check(throws(), "demo env is rejected (Kalshi demo unavailable)");
  reset_env(); set("KALSHI_ENV", "demo"); set("KALSHI_ALLOW_PROD", "1");
  check(throws(), "demo env rejected even with KALSHI_ALLOW_PROD");

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

  // Host allowlist cross-validation. A leftover demo host must be rejected under
  // prod, and a prod collector must reject a localhost/mock host.
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "https://external-api.demo.kalshi.co");
  check(throws(), "leftover demo host under prod env throws");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "https://127.0.0.1:9999");
  check(throws(), "localhost host under prod env throws");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_BASE_URL", "https://external-api.kalshi.com");
  check(!throws(), "documented prod host accepted");

  // TLS rule: non-https only for local_mock localhost.
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
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_MODE", "live"); set("KALSHI_ALLOW_LIVE", "1");
  set("KALSHI_BASE_URL", "https://some-new-host.kalshi.com"); set("KALSHI_HOST_UNSAFE_OVERRIDE", "1");
  check(throws(), "override refused in live mode");

  // WS URL: resolved per-env by default, and cross-validated like the REST host.
  reset_env();
  {
    Runtime rt = resolve_runtime();  // local_mock default
    check(rt.ws_url.rfind("ws://127.0.0.1", 0) == 0, "default ws url is localhost ws://");
  }
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  {
    Runtime rt = resolve_runtime();
    check(rt.ws_url == "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
          "prod default ws url resolved");
  }
  // Cross-validation + TLS rule for the WS host: a leftover demo WS host must be
  // rejected under prod, and non-TLS is refused off local_mock.
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_WS_URL", "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2");
  check(throws(), "leftover demo ws host under prod env throws");
  reset_env(); set("KALSHI_ENV", "prod"); set("KALSHI_ALLOW_PROD", "1");
  set("KALSHI_WS_URL", "ws://external-api-ws.kalshi.com/trade-api/ws/v2");
  check(throws(), "non-TLS ws url under prod throws (TLS rule)");
  reset_env(); set("KALSHI_WS_URL", "ws://127.0.0.1:18200/trade-api/ws/v2");
  check(!throws(), "non-TLS ws localhost accepted under local_mock");

  // Secret redaction.
  check(redact("KALSHI-ACCESS-KEY: abc123") == "***", "redact hides secrets");

  reset_env();
  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
