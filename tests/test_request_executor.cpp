// RequestExecutor against tests/mock_rest.py: reserve-before-send (a locally
// refused reservation emits NO HTTP), granted send hits the server exactly once,
// telemetry fields populated, 429 retry, and malformed-body pass-through.
//
// usage: test_request_executor <base_url>

#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/request_executor.hpp"
#include "trading/timestamp.hpp"

#include <openssl/bio.h>
#include <openssl/evp.h>
#include <openssl/pem.h>

#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

using namespace kalshi;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

std::string throwaway_key() {
  EVP_PKEY* k = EVP_RSA_gen(2048);
  BIO* bio = BIO_new(BIO_s_mem());
  PEM_write_bio_PrivateKey(bio, k, nullptr, nullptr, 0, nullptr, nullptr);
  char* data = nullptr;
  long len = BIO_get_mem_data(bio, &data);
  std::string pem(data, static_cast<size_t>(len));
  BIO_free(bio);
  EVP_PKEY_free(k);
  return pem;
}

// Reads the mock's served-request counter via a RAW client call (NOT the
// executor) so it doesn't perturb the executor's token buckets.
long server_count(KalshiClient& client) {
  auto r = client.request(Method::Get, "/debug/requests");
  if (!r || !r->ok()) return -1;
  const auto pos = r->body.find("\"count\":");
  return pos == std::string::npos ? -1 : std::atol(r->body.c_str() + pos + 8);
}

struct CapturingSink : TelemetrySink {
  std::vector<RequestTelemetry> records;
  void on_request(const RequestTelemetry& t) override { records.push_back(t); }
};
}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) { std::cerr << "usage: test_request_executor <base_url>\n"; return 2; }

  Config cfg;
  cfg.api_key_id = "test";
  cfg.private_key_pem = throwaway_key();
  cfg.base_url = argv[1];
  cfg.pool_size = 2;
  KalshiClient client(std::move(cfg));

  Runtime rt;  // local_mock defaults
  rt.rest_base_url = argv[1];
  CapturingSink sink;
  ExecPolicy pol;
  pol.base_ms = 40;
  pol.max_attempts = 4;
  pol.reserve_budget_ms = 2000;
  RequestExecutor exec(client, rt, pol, &sink);

  // 1. granted send returns 200 and hits the server exactly once.
  {
    const long c0 = server_count(client);
    auto r = exec.send(Method::Get, "/exchange/status");
    const long c1 = server_count(client);
    check(r.has_value() && r->ok(), "granted send returns 200");
    check(c1 == c0 + 1, "granted send emitted exactly one HTTP request");
  }

  // 2. telemetry fields populated for the request above.
  {
    check(!sink.records.empty(), "telemetry emitted");
    const RequestTelemetry& t = sink.records.front();
    check(std::string(t.method) == "GET" && t.normalized_path == "/trade-api/v2/exchange/status",
          "telemetry: method + normalized full-wire path");
    check(std::string(t.bucket) == "read" && std::string(t.outcome) == "ok",
          "telemetry: read bucket, outcome ok");
    check(t.cost_tokens == 10 && t.endpoint_cost_source == "default",
          "telemetry: default cost (no cost table) + source=default");
    check(t.usage_tier == "basic", "telemetry: conservative usage_tier when unconfigured");
  }

  // 3. 429 (RETRY_NONE: 429 x2 then 200) retried through the executor.
  {
    auto r = exec.send(Method::Get, "/markets?limit=100&series_ticker=RETRY_NONE");
    check(r.has_value() && r->ok(), "429 retried to success via executor backoff");
  }

  // 4. malformed 200 body is passed through as a Response (executor is body-agnostic;
  //    JSON parsing + its errors live in the typed endpoint layer).
  {
    auto r = exec.send(Method::Get, "/debug/malformed");
    check(r.has_value() && r->ok() && r->body.find("not json") != std::string::npos,
          "malformed 200 body returned raw (no crash)");
  }

  // 5. reserve-before-send: drain the read bucket, then a send whose reservation
  //    cannot be met by the deadline must be REFUSED and emit NO HTTP.
  {
    while (exec.read_bucket().try_reserve(1, trading::mono_ns())) { /* drain to ~0 */ }
    const long before = server_count(client);
    SendOpts opts;
    opts.deadline_ns = trading::mono_ns();  // now => any required wait exceeds it
    auto r = exec.send(Method::Get, "/exchange/status", "", opts);
    const long after = server_count(client);
    check(!r.has_value() && r.error().kalshi_code == "rate_limited",
          "reservation unmet by deadline => refused");
    check(after == before, "refused send emitted NO HTTP (reserve-before-send)");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
