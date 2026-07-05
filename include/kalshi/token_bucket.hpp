#pragma once
//
// Integer token bucket (docs/PLAN_TOKEN_RULES.md T3, hard rule 4, fact F7).
// Tokens are held as milli-tokens (×10^3) in int64 against a nanosecond clock —
// NO doubles anywhere on the accounting path. Refill uses exact carry
// accumulation so there is ZERO drift over unbounded cycles (the rounding
// remainder is preserved, never discarded).
//
// The clock is caller-injected: every method takes now_ns, so tests drive a fake
// clock and the executor passes trading::mono_ns(). Rate + capacity come DIRECTLY
// from the server BucketLimit (refill_rate / bucket_capacity) — no local burst
// derivation (F7).
//
// Threading: a single mutex guards all state. Per the plan, the RequestExecutor
// runs on control/lane threads (NOT the market-data hot path), so a plain mutex
// is the documented, deliberate choice.

#include <cstdint>
#include <mutex>

namespace kalshi {

// One reservation's outcome + telemetry (bucket/before/after/wait_ns/cost).
struct Reservation {
  bool granted = false;
  std::int64_t wait_ns = 0;        // if granted, caller sleeps this long before using it
  std::int64_t before_milli = 0;   // milli-tokens before the reservation
  std::int64_t after_milli = 0;    // milli-tokens after (may be negative = reserved debt)
  std::int64_t cost_milli = 0;
};

class TokenBucketI64 {
 public:
  static constexpr std::int64_t kScale = 1000;      // milli-tokens per token
  static constexpr std::int64_t kAcc = 1'000'000;   // carry sub-units per milli-token

  // Starts FULL at capacity. rate = tokens/sec, capacity = max tokens (F7).
  TokenBucketI64(std::int64_t refill_rate, std::int64_t capacity)
      : rate_(refill_rate), cap_(capacity * kScale), tokens_(capacity * kScale) {}

  // Reconfigure from a fresh server pull (T1 hourly refresh). Clamps to new cap.
  void configure(std::int64_t refill_rate, std::int64_t capacity) {
    std::lock_guard lk(m_);
    rate_ = refill_rate;
    cap_ = capacity * kScale;
    if (tokens_ > cap_) tokens_ = cap_;
  }

  // Non-blocking: deduct cost if available now, else refuse (no debt).
  bool try_reserve(std::int64_t cost_tokens, std::int64_t now_ns) {
    std::lock_guard lk(m_);
    refill(now_ns);
    const std::int64_t need = cost_tokens * kScale;
    if (tokens_ >= need) { tokens_ -= need; return true; }
    return false;
  }

  // Reserve now if possible; otherwise, if the wait to accrue enough tokens fits
  // within the deadline, reserve into debt and return the wait the caller must
  // sleep before using the grant. If the wait exceeds the deadline (or rate<=0),
  // refuse without deducting.
  Reservation reserve_or_wait(std::int64_t cost_tokens, std::int64_t now_ns,
                              std::int64_t deadline_ns) {
    std::lock_guard lk(m_);
    refill(now_ns);
    Reservation r;
    r.cost_milli = cost_tokens * kScale;
    r.before_milli = tokens_;
    if (tokens_ >= r.cost_milli) {
      tokens_ -= r.cost_milli;
      r.after_milli = tokens_;
      r.granted = true;
      return r;
    }
    const std::int64_t deficit = r.cost_milli - tokens_;  // > 0
    r.wait_ns = wait_for(deficit);
    if (rate_ <= 0 || now_ns + r.wait_ns > deadline_ns || r.wait_ns < 0) {
      r.after_milli = tokens_;  // not deducted
      return r;                 // granted stays false
    }
    tokens_ -= r.cost_milli;    // reserve the future (bucket goes into debt)
    r.after_milli = tokens_;
    r.granted = true;
    return r;
  }

  // Refill-and-read helpers (also used by telemetry/tests).
  std::int64_t available_milli(std::int64_t now_ns) {
    std::lock_guard lk(m_);
    refill(now_ns);
    return tokens_;
  }
  std::int64_t available_tokens(std::int64_t now_ns) { return available_milli(now_ns) / kScale; }
  std::int64_t refill_rate() const { std::lock_guard lk(m_); return rate_; }
  std::int64_t capacity_tokens() const { std::lock_guard lk(m_); return cap_ / kScale; }

 private:
  // Exact carry-based refill. `acc_` holds the sub-milli-token remainder in units
  // of (milli-token / kAcc) and is always in [0, kAcc), so nothing is ever lost.
  void refill(std::int64_t now_ns) {
    if (now_ns <= last_ns_) return;  // non-monotonic / same tick: keep the max anchor
    const std::int64_t dt = now_ns - last_ns_;
    last_ns_ = now_ns;
    if (rate_ <= 0) return;
    const std::int64_t headroom = (cap_ - tokens_) * kAcc - acc_;  // acc units to reach cap
    if (headroom <= 0) { acc_ = 0; return; }                       // already at/over cap
    // If dt alone fills the bucket, saturate (bounds rate_*dt below to avoid overflow).
    if (dt >= headroom / rate_ + 1) { tokens_ = cap_; acc_ = 0; return; }
    acc_ += rate_ * dt;               // safe: < headroom + rate_
    const std::int64_t whole = acc_ / kAcc;
    acc_ -= whole * kAcc;
    tokens_ += whole;
    if (tokens_ > cap_) { tokens_ = cap_; acc_ = 0; }
  }

  // ns until `deficit_milli` milli-tokens accrue at the current rate (ceil),
  // crediting the carry already banked in acc_.
  std::int64_t wait_for(std::int64_t deficit_milli) const {
    if (rate_ <= 0) return -1;
    const std::int64_t acc_needed = deficit_milli * kAcc - acc_;
    if (acc_needed <= 0) return 0;
    return (acc_needed + rate_ - 1) / rate_;  // ceil divide (acc units / (tokens/sec) = ns)
  }

  std::int64_t rate_;         // tokens per second
  std::int64_t cap_;          // milli-tokens capacity
  std::int64_t tokens_;       // milli-tokens available (may be negative = debt)
  std::int64_t acc_ = 0;      // carry, in [0, kAcc)
  std::int64_t last_ns_ = 0;
  mutable std::mutex m_;
};

}  // namespace kalshi
