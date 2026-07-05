#pragma once
//
// Backoff + retry-decision policy (docs/PLAN_TOKEN_RULES.md T4, fact F9).
//
// Order of defense: the token bucket's local wait comes FIRST (reserve-before-
// send); this backoff fires only on an UNEXPECTED 429 or a transient failure.
//
// Retry matrix (decide_retry, pure/deterministic):
//   * GET / idempotent ops (cancel-by-id): retry on 429 / 502 / 503 / 504 /
//     transport error.
//   * Non-idempotent writes (order create/amend, order groups, RFQ quotes): an
//     AMBIGUOUS outcome (transport timeout or 5xx gateway — the request may have
//     been processed) is NEVER blindly retried; it demands reconcile via
//     client_order_id first. A 429 is an UNAMBIGUOUS rejection (not accepted) and
//     may be retried after a wait.
//   * A 429 that local accounting did not expect is flagged accounting_drift
//     (telemetry token_accounting_drift; refresh endpoint costs — T1/§4).
//
// Retry-After (F9): the server sends none today; if one ever appears it is
// recorded as telemetry ONLY and never waited on — the computed backoff governs.

#include "kalshi/client.hpp"        // Method
#include "kalshi/request_spec.hpp"  // MutationKind

#include <cstdint>

namespace kalshi {

// Deterministic capped-exponential backoff with equal jitter. Seeded PRNG =>
// reproducible in tests, decorrelated across instances in production.
class Backoff {
 public:
  explicit Backoff(std::uint64_t seed, long base_ms = 200, long cap_ms = 5000)
      : state_(seed ? seed : 0x9E3779B97F4A7C15ULL), base_ms_(base_ms), cap_ms_(cap_ms) {}

  // Delay for a 0-based attempt: exp = min(cap, base<<attempt); jitter uniformly
  // in [exp/2, exp]. Always > 0, never exceeds cap_ms.
  long delay_ms(int attempt) {
    long exp = cap_ms_;
    if (attempt >= 0 && attempt < 31) {
      const long shifted = base_ms_ << attempt;  // base_ms_ small; no overflow for attempt<31
      exp = shifted < cap_ms_ ? shifted : cap_ms_;
    }
    if (exp <= 0) return 0;
    const long half = exp / 2;
    const long span = exp - half;             // == ceil(exp/2)
    const long jitter = static_cast<long>(next() % static_cast<std::uint64_t>(span + 1));
    return half + jitter;                     // in [half, exp]
  }

 private:
  std::uint64_t next() {  // splitmix64
    std::uint64_t z = (state_ += 0x9E3779B97F4A7C15ULL);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
  }
  std::uint64_t state_;
  long base_ms_, cap_ms_;
};

struct RetryDecision {
  bool retry = false;             // retry after a backoff wait
  bool needs_reconcile = false;   // ambiguous non-idempotent write: reconcile, do NOT resend blindly
  bool accounting_drift = false;  // an "impossible" 429 (telemetry token_accounting_drift)
  const char* reason = "";
};

// True for writes whose outcome is unsafe to assume after an ambiguous failure
// (a duplicate would be created on blind resend).
inline bool is_nonidempotent_write(MutationKind m) {
  return m == MutationKind::CreateOrder || m == MutationKind::AmendOrder ||
         m == MutationKind::OrderGroup || m == MutationKind::Quote;
}

// Pure retry decision. `transport_error` = a transport-level failure (timeout /
// reset) with an AMBIGUOUS outcome; `http_status` = 0 when transport_error.
inline RetryDecision decide_retry(Method method, MutationKind mutation, long http_status,
                                  bool transport_error, int attempt, int max_attempts) {
  (void)method;
  RetryDecision d;
  const bool have_attempt = (attempt + 1) < max_attempts;
  const bool ambiguous =
      transport_error || http_status == 502 || http_status == 503 || http_status == 504;

  if (http_status == 429) {
    // Unambiguous rejection: the request was NOT processed -> safe to retry after
    // a wait, for every method. Local accounting should have prevented it.
    d.accounting_drift = true;
    d.retry = have_attempt;
    d.reason = "429 rejected (unexpected: retry after wait, refresh costs)";
  } else if (ambiguous) {
    if (is_nonidempotent_write(mutation)) {
      d.retry = false;
      d.needs_reconcile = true;
      d.reason = "ambiguous write outcome -> reconcile via client_order_id, never blind retry";
    } else {
      d.retry = have_attempt;
      d.reason = "ambiguous but idempotent (GET/cancel) -> retry";
    }
  } else {
    d.reason = "non-retryable status";
  }
  return d;
}

}  // namespace kalshi
