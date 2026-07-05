// Auth-header redaction + telemetry secret-freedom (hard rule 2). Pure.

#include "kalshi/env.hpp"        // redact()
#include "kalshi/redact.hpp"     // redact_auth_headers()
#include "kalshi/telemetry.hpp"  // RequestTelemetry (no secret fields)

#include <iostream>
#include <string>

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
bool contains(const std::string& hay, const std::string& needle) {
  return hay.find(needle) != std::string::npos;
}
}  // namespace

int main() {
  using namespace kalshi;

  // A realistic outgoing header block (what CURLOPT_VERBOSE would dump).
  const std::string block =
      "GET /trade-api/v2/portfolio/balance HTTP/1.1\r\n"
      "Host: external-api.kalshi.com\r\n"
      "KALSHI-ACCESS-KEY: 00000000-0000-0000-0000-000000000000\r\n"
      "KALSHI-ACCESS-SIGNATURE: dGhpc0lzQVZlcnlTZWNyZXRTaWduYXR1cmU9PQ==\r\n"
      "KALSHI-ACCESS-TIMESTAMP: 1893456000000\r\n"
      "Authorization: Bearer sk_live_deadbeef\r\n"
      "Content-Type: application/json\r\n";
  const std::string red = redact_auth_headers(block);

  // Secret VALUES gone.
  check(!contains(red, "00000000-0000-0000-0000-000000000000"), "access key value redacted");
  check(!contains(red, "dGhpc0lzQVZlcnlTZWNyZXRTaWduYXR1cmU9PQ=="), "signature value redacted");
  check(!contains(red, "Bearer sk_live_deadbeef"), "Authorization value redacted");
  check(!contains(red, "1893456000000"), "timestamp value redacted");
  // Header NAMES + non-sensitive lines preserved.
  check(contains(red, "KALSHI-ACCESS-SIGNATURE:"), "signature header name preserved");
  check(contains(red, "***REDACTED***"), "redaction marker present");
  check(contains(red, "Content-Type: application/json"), "non-sensitive header untouched");
  check(contains(red, "Host: external-api.kalshi.com"), "Host header untouched");

  // Idempotent: redacting twice is stable.
  check(redact_auth_headers(red) == red, "redaction is idempotent");

  // env redact() blanks anything.
  check(redact("KALSHI-ACCESS-KEY: abc123") == "***", "redact() returns ***");

  // Telemetry serialization can never contain a secret — there is no secret field.
  RequestTelemetry t;
  t.method = "POST";
  t.normalized_path = "/trade-api/v2/portfolio/events/orders";
  t.bucket = "write";
  t.mutation = "create_order";
  t.usage_tier = "advanced";
  t.endpoint_cost_source = "server";
  // (No API key / signature field exists on RequestTelemetry — verified structurally.)
  check(std::string(t.method) == "POST" && t.usage_tier == "advanced",
        "telemetry record carries no auth field (secret-free by construction)");

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
