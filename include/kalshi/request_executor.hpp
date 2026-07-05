#pragma once
//
// RequestExecutor — the SINGLE outbound path for every authenticated REST call
// (docs/PLAN_TOKEN_RULES.md T5, hard rule 5). One send() implements the 11-step
// flow: build RequestSpec -> resolve sig path -> normalize -> pick bucket ->
// resolve cost -> RESERVE (before any signing/HTTP, hard rule 3) -> sign
// (pure-CPU) -> send -> parse -> retry policy (T4) -> telemetry.
//
// Signing/transport reuse the KalshiClient split API (sign_request/send_signed).
// The only authenticated signing allowed OUTSIDE this class is the WS handshake
// (src/ws_client.cpp), which the grep gate exempts.

#include "kalshi/api_error.hpp"
#include "kalshi/backoff.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/limits.hpp"
#include "kalshi/request_spec.hpp"
#include "kalshi/telemetry.hpp"
#include "kalshi/token_bucket.hpp"
#include "trading/timestamp.hpp"

#include <cstdint>
#include <expected>
#include <string>
#include <string_view>

namespace kalshi {

struct RetryPolicy;  // defined in rest_api.hpp

struct SendOpts {
  std::int64_t deadline_ns = -1;   // <0 => now + kDefaultReserveBudgetMs
  std::uint64_t trace_id = 0;      // correlation id for telemetry
};

// Retry/backoff knobs (kept here so the executor does not depend on rest_api.hpp).
struct ExecPolicy {
  int max_attempts = 4;
  long base_ms = 200;
  long max_backoff_ms = 5000;
  long reserve_budget_ms = 30000;  // default reservation deadline budget
};

// Free error mappers (shared with RestApi).
ApiError map_transport_error(const Error&);
ApiError parse_kalshi_error(const Response&);

class RequestExecutor {
 public:
  RequestExecutor(KalshiClient& client, Runtime rt, ExecPolicy policy = {},
                  TelemetrySink* telemetry = nullptr)
      : c_(client), rt_(std::move(rt)), policy_(policy),
        read_bucket_(conservative_limits().read.refill_rate,
                     conservative_limits().read.bucket_capacity),
        write_bucket_(conservative_limits().write.refill_rate,
                      conservative_limits().write.bucket_capacity),
        backoff_(static_cast<std::uint64_t>(trading::mono_ns()) | 1ULL, policy.base_ms,
                 policy.max_backoff_ms),
        usage_tier_(conservative_limits().usage_tier),
        tel_(telemetry ? telemetry : &null_sink_) {}

  // Adopt server-provided limits + costs (T1/F7: capacity used directly).
  void configure_limits(const AccountLimits& lim) {
    read_bucket_.configure(lim.read.refill_rate, lim.read.bucket_capacity);
    write_bucket_.configure(lim.write.refill_rate, lim.write.bucket_capacity);
    usage_tier_ = lim.usage_tier;
  }
  void set_cost_table(const EndpointCostTable& costs) { costs_ = costs; }
  void set_telemetry(TelemetrySink* t) { tel_ = t ? t : &null_sink_; }

  // The single outbound path. `path` may be prefix-less or full wire, with or
  // without a query; it is classified/costed on the query-stripped full wire
  // path and sent with the query preserved.
  std::expected<Response, ApiError> send(Method method, std::string_view path,
                                         std::string_view body = {}, const SendOpts& opts = {});

  TokenBucketI64& read_bucket() { return read_bucket_; }
  TokenBucketI64& write_bucket() { return write_bucket_; }
  const EndpointCostTable& cost_table() const { return costs_; }

 private:
  KalshiClient& c_;
  Runtime rt_;
  ExecPolicy policy_;
  TokenBucketI64 read_bucket_;
  TokenBucketI64 write_bucket_;
  Backoff backoff_;
  EndpointCostTable costs_;   // default: all default_cost until configured
  std::string usage_tier_;
  NullTelemetrySink null_sink_;
  TelemetrySink* tel_;
};

}  // namespace kalshi
