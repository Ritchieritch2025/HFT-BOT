#pragma once
//
// Per-request telemetry for the RequestExecutor (docs/PLAN_TOKEN_RULES.md T5 §12).
// Cold-path NDJSON: one line per outbound request. The dashboard reads it as a
// request-chain section. NOTHING here is a secret — there is no field for a key,
// signature, or auth header, so the record can never leak one (hard rule 2).

#include <cstdint>
#include <cstdio>
#include <string>
#include <string_view>

namespace kalshi {

struct RequestTelemetry {
  std::int64_t ts_wall_ns = 0;
  const char* method = "GET";
  std::string normalized_path;         // full wire path, query stripped
  const char* bucket = "read";         // read | write
  const char* mutation = "none";       // none | create_order | ...
  int cost_tokens = 0;
  std::int64_t tokens_before_milli = 0;
  std::int64_t tokens_after_milli = 0;
  std::int64_t wait_ns = 0;            // reservation wait before sending
  int attempts = 0;
  long http_status = 0;               // 0 = not sent / transport error
  const char* outcome = "";           // ok | http_error | rate_limited | reconcile | transport_error
  std::string endpoint_cost_source;   // "server" | "default"
  std::string usage_tier;
  std::uint64_t trace_id = 0;
};

struct TelemetrySink {
  virtual ~TelemetrySink() = default;
  virtual void on_request(const RequestTelemetry&) = 0;
};

// Discards everything (default when no telemetry is wired).
struct NullTelemetrySink : TelemetrySink {
  void on_request(const RequestTelemetry&) override {}
};

// Append-only NDJSON. Path/segment values are Kalshi-safe (no escaping needed for
// the ASCII paths + enum strings we emit); numbers are integers only.
class NdjsonTelemetrySink : public TelemetrySink {
 public:
  explicit NdjsonTelemetrySink(const std::string& path) { f_ = std::fopen(path.c_str(), "ab"); }
  ~NdjsonTelemetrySink() override { if (f_) std::fclose(f_); }

  void on_request(const RequestTelemetry& t) override {
    if (!f_) return;
    std::fprintf(
        f_,
        "{\"ts_ns\":%lld,\"method\":\"%s\",\"path\":\"%s\",\"bucket\":\"%s\","
        "\"mutation\":\"%s\",\"cost\":%d,\"tokens_before_milli\":%lld,"
        "\"tokens_after_milli\":%lld,\"wait_ns\":%lld,\"attempts\":%d,"
        "\"http_status\":%ld,\"outcome\":\"%s\",\"endpoint_cost_source\":\"%s\","
        "\"usage_tier\":\"%s\",\"trace_id\":%llu}\n",
        static_cast<long long>(t.ts_wall_ns), t.method, t.normalized_path.c_str(), t.bucket,
        t.mutation, t.cost_tokens, static_cast<long long>(t.tokens_before_milli),
        static_cast<long long>(t.tokens_after_milli), static_cast<long long>(t.wait_ns),
        t.attempts, t.http_status, t.outcome, t.endpoint_cost_source.c_str(),
        t.usage_tier.c_str(), static_cast<unsigned long long>(t.trace_id));
    std::fflush(f_);
  }

 private:
  std::FILE* f_ = nullptr;
};

}  // namespace kalshi
