#pragma once
//
// Timestamp schema and latency accounting for the trading bus.
//
// Two clocks, always labeled so they are never mixed accidentally:
//   wall_ns()  — system_clock, epoch nanoseconds. For anything the outside
//                world sees / correlates (exchange timestamps, logs).
//   mono_ns()  — steady_clock, monotonic nanoseconds. For latency spans; immune
//                to NTP steps.
//
// `Timestamps` describes the EVENT side only. There is deliberately no
// `decision_ns` here — decisions happen downstream, and Signal/OrderIntent
// carry their own decided_ns/created_ns (copy-and-stamp; never mutate a shared
// event struct).

#include <chrono>
#include <cstdint>
#include <optional>

namespace trading {

inline std::int64_t wall_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

inline std::int64_t mono_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

struct Timestamps {
  std::optional<std::int64_t> source_event_time_ms;  // exchange ts if provided
  std::int64_t local_receive_mono_ns = 0;            // monotonic
  std::int64_t local_receive_wall_ns = 0;            // wall clock
  std::int64_t publish_time_ns = 0;                  // monotonic, when published to bus
};

// Stamp receive-time (both clocks) at the ingest boundary.
inline Timestamps stamp_receive(std::optional<std::int64_t> exch_ms = std::nullopt) {
  Timestamps t;
  t.source_event_time_ms = exch_ms;
  t.local_receive_mono_ns = mono_ns();
  t.local_receive_wall_ns = wall_ns();
  return t;
}

// Simple lock-free-friendly latency accumulator (single-thread use, or one per
// thread). Nanosecond spans in, summary out.
struct LatencyStats {
  std::uint64_t count = 0;
  std::int64_t sum_ns = 0;
  std::int64_t min_ns = 0;
  std::int64_t max_ns = 0;

  void add(std::int64_t ns) {
    if (count == 0) {
      min_ns = max_ns = ns;
    } else {
      if (ns < min_ns) min_ns = ns;
      if (ns > max_ns) max_ns = ns;
    }
    sum_ns += ns;
    ++count;
  }
  double mean_ns() const {
    return count ? static_cast<double>(sum_ns) / static_cast<double>(count) : 0.0;
  }
  void reset() { *this = LatencyStats{}; }
};

}  // namespace trading
