// Backoff (deterministic seeded jitter, capped exponential) + the F9 retry
// matrix. Pure; the end-to-end "POST timeout => at most one order reaches the
// server" is exercised against the mock in T5 (executor), but the decision that
// GUARANTEES it — CreateOrder + ambiguous => needs_reconcile, never retry — is
// asserted here.

#include "kalshi/backoff.hpp"

#include <iostream>
#include <string>
#include <vector>

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
}  // namespace

int main() {
  using namespace kalshi;

  // --- deterministic jitter: same seed => identical sequence ---
  {
    Backoff a(12345, 40, 5000), b(12345, 40, 5000);
    std::vector<long> sa, sb;
    for (int i = 0; i < 8; ++i) { sa.push_back(a.delay_ms(i)); sb.push_back(b.delay_ms(i)); }
    check(sa == sb, "same seed => identical backoff sequence (deterministic)");
    Backoff c(999, 40, 5000);
    bool differs = false;
    for (int i = 0; i < 8; ++i) if (c.delay_ms(i) != sa[i]) differs = true;
    check(differs, "different seed => different sequence");
  }

  // --- bounds: jitter in [exp/2, exp], capped exponential, always > 0 ---
  {
    Backoff a(7, 40, 500);
    for (int i = 0; i < 20; ++i) {
      long exp = i < 31 ? (40L << i) : 500;
      if (exp > 500) exp = 500;
      long d = a.delay_ms(i);
      if (!(d >= exp / 2 && d <= exp && d > 0)) {
        check(false, "delay within [exp/2, exp] and > 0 at attempt " + std::to_string(i));
        break;
      }
      if (i == 19) check(true, "all delays within [exp/2, exp], capped, > 0");
    }
    check(a.delay_ms(60) <= 500, "large attempt clamps to cap_ms");
  }

  // --- retry matrix (F9) ---
  const int kMax = 4;
  // GET: retry on 429 / 5xx / transport, until attempts exhausted.
  check(decide_retry(Method::Get, MutationKind::None, 429, false, 0, kMax).retry,
        "GET 429 => retry");
  check(decide_retry(Method::Get, MutationKind::None, 503, false, 0, kMax).retry,
        "GET 503 => retry");
  check(decide_retry(Method::Get, MutationKind::None, 0, true, 0, kMax).retry,
        "GET transport error => retry (idempotent)");
  check(!decide_retry(Method::Get, MutationKind::None, 429, false, 3, kMax).retry,
        "GET 429 on last attempt => no retry");
  check(!decide_retry(Method::Get, MutationKind::None, 404, false, 0, kMax).retry,
        "GET 404 => non-retryable");

  // 429 flags accounting drift (bucket should have prevented it) -> telemetry.
  check(decide_retry(Method::Get, MutationKind::None, 429, false, 0, kMax).accounting_drift,
        "429 flags accounting_drift (token_accounting_drift telemetry)");
  check(!decide_retry(Method::Get, MutationKind::None, 503, false, 0, kMax).accounting_drift,
        "503 is not accounting drift");

  // CreateOrder: ambiguous outcome => reconcile, NEVER blind retry (no duplicate).
  {
    auto to = decide_retry(Method::Post, MutationKind::CreateOrder, 0, true, 0, kMax);
    check(!to.retry && to.needs_reconcile, "POST order + transport timeout => reconcile, no retry");
    auto g5 = decide_retry(Method::Post, MutationKind::CreateOrder, 504, false, 0, kMax);
    check(!g5.retry && g5.needs_reconcile, "POST order + 504 gateway => reconcile, no retry (ambiguous)");
    // But a 429 is an unambiguous rejection: safe to retry after wait.
    auto r429 = decide_retry(Method::Post, MutationKind::CreateOrder, 429, false, 0, kMax);
    check(r429.retry && !r429.needs_reconcile, "POST order + 429 (not accepted) => retry after wait");
  }

  // Amend / order-group / quote are also non-idempotent writes.
  check(decide_retry(Method::Post, MutationKind::AmendOrder, 0, true, 0, kMax).needs_reconcile,
        "amend + ambiguous => reconcile");
  check(decide_retry(Method::Post, MutationKind::OrderGroup, 503, false, 0, kMax).needs_reconcile,
        "order group + 5xx => reconcile");
  // Cancel-by-id IS idempotent: ambiguous cancel may retry.
  {
    auto d = decide_retry(Method::Delete, MutationKind::CancelOrder, 0, true, 0, kMax);
    check(d.retry && !d.needs_reconcile, "cancel + ambiguous => retry (idempotent)");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
