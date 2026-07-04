#pragma once
// Shared daemon plumbing: stop signal, wall-clock helpers, env config, and
// timestamped stderr logging.

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <thread>

namespace kalshi::daemon {

inline std::atomic<bool> g_stop{false};

inline void install_signal_handlers() {
  std::signal(SIGINT, [](int) { g_stop.store(true); });
  std::signal(SIGTERM, [](int) { g_stop.store(true); });
  std::signal(SIGPIPE, SIG_IGN);
}

// Wall clock: for anything the outside world sees (signature timestamps,
// ExecPayload::ts_ns / client_order_id determinism).
inline std::uint64_t now_ns() {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::system_clock::now().time_since_epoch())
          .count());
}

// Monotonic clock: for every latency span and staleness gate — immune to NTP
// steps that could spuriously expire (or immortalize) queued orders.
inline std::uint64_t steady_now_ns() {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

inline std::string env_or(const char* key, const char* fallback) {
  const char* v = std::getenv(key);
  return v ? v : fallback;
}

inline int env_int(const char* key, int fallback) {
  const char* v = std::getenv(key);
  return v ? std::atoi(v) : fallback;
}

__attribute__((format(printf, 1, 2)))
inline void logf(const char* fmt, ...) {
  char line[1024];
  va_list ap;
  va_start(ap, fmt);
  std::vsnprintf(line, sizeof(line), fmt, ap);
  va_end(ap);
  std::fprintf(stderr, "[%llu] %s\n",
               static_cast<unsigned long long>(now_ns() / 1'000'000), line);
}

inline void backoff_sleep(int attempt) {
  const int ms = attempt < 6 ? (50 << attempt) : 3200;  // 50ms .. 3.2s
  std::this_thread::sleep_for(std::chrono::milliseconds(ms));
}

}  // namespace kalshi::daemon
