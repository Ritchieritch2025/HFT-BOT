#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>

namespace latency_probe {

enum class Event : std::size_t {
  DataReceived = 0,
  DataParsed,
  DecisionStart,
  DecisionDone,
  RiskDone,
  SignStart,
  SignDone,
  WriteStart,
  WriteDone,
  ResponseFirstByte,
  ResponseDone,
  OrderAccepted,
  Count
};

inline const char* event_name(Event event) {
  switch (event) {
    case Event::DataReceived: return "data_received";
    case Event::DataParsed: return "data_parsed";
    case Event::DecisionStart: return "decision_start";
    case Event::DecisionDone: return "decision_done";
    case Event::RiskDone: return "risk_done";
    case Event::SignStart: return "sign_start";
    case Event::SignDone: return "sign_done";
    case Event::WriteStart: return "write_start";
    case Event::WriteDone: return "write_done";
    case Event::ResponseFirstByte: return "response_first_byte";
    case Event::ResponseDone: return "response_done";
    case Event::OrderAccepted: return "order_accepted";
    case Event::Count: return "count";
  }
  return "unknown";
}

inline std::uint64_t now_ns() {
  const auto now = std::chrono::steady_clock::now().time_since_epoch();
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(now).count());
}

struct Trace {
  std::uint64_t id = 0;
  std::array<std::uint64_t, static_cast<std::size_t>(Event::Count)> t{};

  void reset(std::uint64_t trace_id) {
    id = trace_id;
    t.fill(0);
  }

  void mark(Event event) {
    t[static_cast<std::size_t>(event)] = now_ns();
  }

  std::uint64_t at(Event event) const {
    return t[static_cast<std::size_t>(event)];
  }
};

inline void write_csv_header(std::FILE* file) {
  std::fputs(
      "trace_id,"
      "data_to_parse_us,"
      "decision_us,"
      "risk_to_sign_start_us,"
      "sign_us,"
      "write_us,"
      "wire_plus_exchange_to_first_byte_us,"
      "response_parse_us,"
      "full_to_response_us,"
      "full_to_accepted_us\n",
      file);
}

inline double us_between(std::uint64_t start_ns, std::uint64_t end_ns) {
  if (start_ns == 0 || end_ns == 0 || end_ns < start_ns) {
    return -1.0;
  }
  return static_cast<double>(end_ns - start_ns) / 1000.0;
}

inline void write_csv_row(std::FILE* file, const Trace& trace) {
  const auto d0 = trace.at(Event::DataReceived);
  const auto d1 = trace.at(Event::DataParsed);
  const auto s0 = trace.at(Event::DecisionStart);
  const auto s1 = trace.at(Event::DecisionDone);
  const auto r1 = trace.at(Event::RiskDone);
  const auto g0 = trace.at(Event::SignStart);
  const auto g1 = trace.at(Event::SignDone);
  const auto w0 = trace.at(Event::WriteStart);
  const auto w1 = trace.at(Event::WriteDone);
  const auto b1 = trace.at(Event::ResponseFirstByte);
  const auto p1 = trace.at(Event::ResponseDone);
  const auto a1 = trace.at(Event::OrderAccepted);

  std::fprintf(
      file,
      "%llu,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f,%.3f\n",
      static_cast<unsigned long long>(trace.id),
      us_between(d0, d1),
      us_between(s0, s1),
      us_between(r1, g0),
      us_between(g0, g1),
      us_between(w0, w1),
      us_between(w1, b1),
      us_between(b1, p1),
      us_between(d0, p1),
      us_between(d0, a1));
}

}  // namespace latency_probe
