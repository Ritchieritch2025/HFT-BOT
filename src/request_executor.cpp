#include "kalshi/request_executor.hpp"

#include <curl/curl.h>

#include "simdjson.h"

#include <chrono>
#include <limits>
#include <thread>

namespace kalshi {

namespace {

bool transient_curl(int code) {
  switch (code) {
    case CURLE_COULDNT_CONNECT:
    case CURLE_COULDNT_RESOLVE_HOST:
    case CURLE_OPERATION_TIMEDOUT:
    case CURLE_SEND_ERROR:
    case CURLE_RECV_ERROR:
    case CURLE_GOT_NOTHING:
    case CURLE_PARTIAL_FILE:
      return true;
    default:
      return false;
  }
}
bool transient_http(long status) { return status == 502 || status == 503 || status == 504; }

// Prefix-less path (query preserved) for the signing client, which re-adds
// cfg_.api_prefix. If the caller already passed a prefix-less path, unchanged.
std::string client_path(std::string_view raw) {
  if (raw.rfind(kWirePrefix, 0) == 0) return std::string(raw.substr(kWirePrefix.size()));
  return std::string(raw);
}

}  // namespace

ApiError map_transport_error(const Error& err) {
  ApiError e;
  e.kind = ApiError::Kind::Transport;
  e.transport_code = err.code;
  e.message = err.message;
  return e;
}

ApiError parse_kalshi_error(const Response& resp) {
  ApiError e;
  e.kind = ApiError::Kind::Http;
  e.http_status = resp.status;
  e.message = "HTTP " + std::to_string(resp.status);
  try {
    simdjson::padded_string json(resp.body);
    simdjson::ondemand::parser parser;
    simdjson::ondemand::document doc;
    if (parser.iterate(json).get(doc) != simdjson::SUCCESS) return e;
    simdjson::ondemand::object root;
    if (doc.get_object().get(root) != simdjson::SUCCESS) return e;
    simdjson::ondemand::object err;
    if (root["error"].get(err) == simdjson::SUCCESS) {
      e.kind = ApiError::Kind::Kalshi;
      std::string_view code, msg;
      if (err["code"].get(code) == simdjson::SUCCESS) e.kalshi_code = std::string(code);
      if (err["message"].get(msg) == simdjson::SUCCESS) e.message = std::string(msg);
    }
  } catch (const simdjson::simdjson_error&) {
    // Non-JSON body: keep the HTTP-status message.
  }
  return e;
}

std::expected<Response, ApiError> RequestExecutor::send(Method method, std::string_view path,
                                                        std::string_view body,
                                                        const SendOpts& opts) {
  const RequestSpec spec = make_request_spec(method, path, costs_);
  const std::string cpath = client_path(path);
  TokenBucketI64& bucket = spec.bucket == BucketKind::Write ? write_bucket_ : read_bucket_;

  RequestTelemetry tel;
  tel.ts_wall_ns = trading::wall_ns();
  tel.method = method_name(method);
  tel.normalized_path = spec.normalized_path;
  tel.bucket = to_string(spec.bucket);
  tel.mutation = to_string(spec.mutation);
  tel.cost_tokens = spec.cost_tokens;
  tel.endpoint_cost_source = costs_.from_server ? "server" : "default";
  tel.usage_tier = usage_tier_;
  tel.trace_id = opts.trace_id;

  // Step 5: RESERVE before any signing or HTTP (hard rule 3).
  const std::int64_t now = trading::mono_ns();
  const std::int64_t deadline =
      opts.deadline_ns >= 0 ? opts.deadline_ns
                            : now + static_cast<std::int64_t>(policy_.reserve_budget_ms) * 1'000'000;
  const Reservation res = bucket.reserve_or_wait(spec.cost_tokens, now, deadline);
  tel.tokens_before_milli = res.before_milli;
  tel.tokens_after_milli = res.after_milli;
  tel.wait_ns = res.wait_ns;
  if (!res.granted) {
    tel.outcome = "rate_limited";
    tel_->on_request(tel);
    return std::unexpected(ApiError{ApiError::Kind::Http, 429, 0, "rate_limited",
                                    "local reservation exceeded deadline"});
  }
  if (res.wait_ns > 0) std::this_thread::sleep_for(std::chrono::nanoseconds(res.wait_ns));

  ApiError last{ApiError::Kind::Transport, 0, 0, "", "no attempt made"};
  for (int attempt = 0; attempt < policy_.max_attempts; ++attempt) {
    tel.attempts = attempt + 1;

    // Step 6: sign (pure-CPU) with a fresh timestamp each attempt.
    auto signed_req = c_.sign_request(method, cpath, body);
    if (!signed_req) {
      last = map_transport_error(signed_req.error());  // crypto/signing failure: not retried
      last.message = "signing failed: " + last.message;
      break;
    }

    // Step 7: send.
    auto resp = c_.send_signed(*signed_req);
    if (!resp) {  // transport-level failure (ambiguous outcome)
      last = map_transport_error(resp.error());
      const RetryDecision d =
          decide_retry(method, spec.mutation, 0, true, attempt, policy_.max_attempts);
      if (d.needs_reconcile) {
        last.kalshi_code = "reconcile_required";
        tel.outcome = "reconcile";
        break;
      }
      if (d.retry && transient_curl(resp.error().code)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(backoff_.delay_ms(attempt)));
        continue;
      }
      tel.outcome = "transport_error";
      break;
    }

    const long status = resp->status;
    tel.http_status = status;
    if (status == 429 || transient_http(status)) {
      last = parse_kalshi_error(*resp);
      const RetryDecision d =
          decide_retry(method, spec.mutation, status, false, attempt, policy_.max_attempts);
      if (status == 429 && d.accounting_drift)
        std::fprintf(stderr, "[exec] %s: unexpected 429 on %s %s — refresh endpoint costs\n",
                     kEvTokenAccountingDrift, tel.method, spec.normalized_path.c_str());
      if (resp->retry_after_ms >= 0)  // F9: telemetry only, never honored
        std::fprintf(stderr, "[exec] Retry-After=%ldms observed (telemetry only)\n",
                     resp->retry_after_ms);
      if (d.needs_reconcile) {
        last.kalshi_code = "reconcile_required";
        tel.outcome = "reconcile";
        break;
      }
      if (d.retry) {
        std::this_thread::sleep_for(std::chrono::milliseconds(backoff_.delay_ms(attempt)));
        continue;
      }
      tel.outcome = "http_error";
      break;
    }

    // Success (2xx) or a non-retryable 4xx: return the Response for the caller to parse.
    tel.outcome = resp->ok() ? "ok" : "http_error";
    tel_->on_request(tel);
    return *resp;
  }

  if (tel.outcome[0] == '\0') tel.outcome = "error";
  tel_->on_request(tel);
  return std::unexpected(last);
}

}  // namespace kalshi
