#pragma once
//
// Bus test doubles — NOT strategies, NOT production. Interface scaffolding only,
// so the pipeline can be exercised end to end without any trading logic.
//   NullModel            — emits no signals
//   EchoFeatureBuilder   — passes the triggering event's identity through
//   RecordingSink        — captures received events
//   MockExecutionEngine  — returns a preset ExecDecision and counts submits
// Kept out of bus.hpp so the production interface stays lean.

#include "trading/bus.hpp"

#include <string>
#include <vector>

namespace trading {

class NullModel final : public Model {
 public:
  std::optional<Signal> on_features(const FeatureVector&) override {
    return std::nullopt;  // never trades
  }
  std::string dump_state() const override { return "NullModel{signals=0}"; }
};

// Emits a feature vector carrying the event's entity_id + trace_id (the trace
// of the triggering event) with a single passthrough feature. No modeling.
class EchoFeatureBuilder final : public FeatureBuilder {
 public:
  std::optional<FeatureVector> on_event(const NormalizedEvent& e) override {
    ++built_;
    FeatureVector fv;
    fv.entity_id = e.entity_id;
    fv.trace_id = e.trace_id;
    fv.built_mono_ns = mono_ns();
    fv.values = {1.0};  // passthrough marker
    return fv;
  }
  std::string dump_state() const override {
    return "EchoFeatureBuilder{built=" + std::to_string(built_) + "}";
  }

 private:
  std::uint64_t built_ = 0;
};

class RecordingSink final : public EventSink {
 public:
  void on_event(const NormalizedEvent& e) override {
    events.push_back(e);
    ++count;
    last_trace = e.trace_id;
    last_kind = e.kind();
  }
  std::vector<NormalizedEvent> events;
  std::uint64_t count = 0;
  TraceId last_trace;
  Kind last_kind = Kind::BookSnapshot;
};

class MockExecutionEngine final : public ExecutionEngine {
 public:
  explicit MockExecutionEngine(ExecDecision d) : decision_(d) {}
  ExecDecision submit(const OrderIntent& oi) override {
    ++submits;
    last = oi;
    return decision_;
  }
  std::uint64_t submits = 0;
  OrderIntent last;

 private:
  ExecDecision decision_;
};

}  // namespace trading
