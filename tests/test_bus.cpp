// Trading-bus tests: EntityRegistry round-trip + collision handling, trace_id
// propagation through the full NormalizedEvent -> FeatureVector -> Signal ->
// OrderIntent chain via test doubles, kind()<->variant consistency, and the
// mock execution engine. Pure C++, no deps.

#include "trading/bus.hpp"
#include "trading/test_doubles.hpp"

#include <iostream>
#include <string>

using namespace trading;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
}  // namespace

int main() {
  // --- EntityRegistry ---
  EntityRegistry reg;
  const EntityId e1 = reg.register_entity(SourceId::Kalshi, "KXBTC-25-T100");
  const EntityId e1b = reg.register_entity(SourceId::Kalshi, "KXBTC-25-T100");
  check(e1 == e1b, "registering same key is idempotent");
  check(e1 == make_entity_id(SourceId::Kalshi, "KXBTC-25-T100"),
        "registry id matches deterministic hash");
  check(reg.lookup(SourceId::Kalshi, "KXBTC-25-T100").has_value(), "lookup finds registered");
  check(!reg.lookup(SourceId::Kalshi, "NOPE").has_value(), "lookup misses unregistered");
  auto rev = reg.reverse(e1);
  check(rev && rev->source == SourceId::Kalshi && rev->ticker == "KXBTC-25-T100",
        "reverse recovers source+ticker");
  bool unknown_rejected = false;
  try { reg.register_entity(SourceId::Unknown, "x"); }
  catch (const std::invalid_argument&) { unknown_rejected = true; }
  check(unknown_rejected, "registry rejects SourceId::Unknown");

  // --- kind() derived from the active variant alternative ---
  {
    NormalizedEvent ev;
    ev.payload = BookSnapshot{};
    check(ev.kind() == Kind::BookSnapshot, "kind() == BookSnapshot");
    ev.payload = BookDelta{Side::No, 4200, -300};
    check(ev.kind() == Kind::BookDelta, "kind() == BookDelta");
    ev.payload = Ticker{};
    check(ev.kind() == Kind::Ticker, "kind() == Ticker");
    ev.payload = Trade{};
    check(ev.kind() == Kind::Trade, "kind() == Trade");
    ev.payload = Lifecycle{};
    check(ev.kind() == Kind::Lifecycle, "kind() == Lifecycle");
  }

  // --- trace_id propagates end to end through the doubles ---
  {
    NormalizedEvent ev;
    ev.trace_id = next_trace_id();
    ev.source = SourceId::Kalshi;
    ev.entity_id = e1;
    ev.payload = BookDelta{Side::Yes, 5000, 100};
    const auto ts = stamp_receive();
    ev.local_receive_mono_ns = ts.local_receive_mono_ns;

    RecordingSink sink;
    sink.on_event(ev);
    check(sink.count == 1 && sink.last_trace == ev.trace_id, "sink records event + trace");
    check(sink.last_kind == Kind::BookDelta, "sink sees derived kind");

    EchoFeatureBuilder fb;
    auto fv = fb.on_event(ev);
    check(fv && fv->trace_id == ev.trace_id && fv->entity_id == ev.entity_id,
          "FeatureVector carries the event's trace + entity");

    NullModel model;
    check(!model.on_features(*fv).has_value(), "NullModel emits no signal");

    // A signal + intent minted for the same trace keep it (copy-and-stamp).
    Signal sig;
    sig.entity_id = fv->entity_id;
    sig.trace_id = fv->trace_id;
    sig.decided_mono_ns = mono_ns();
    sig.side = Side::Yes;

    OrderIntent oi;
    oi.entity_id = sig.entity_id;
    oi.trace_id = sig.trace_id;
    oi.created_mono_ns = mono_ns();
    oi.side = sig.side;
    oi.limit_price = 5000;
    oi.size = 100;
    check(oi.trace_id == ev.trace_id, "OrderIntent trace == original event trace (end to end)");

    MockExecutionEngine eng(ExecDecision::Logged);
    check(eng.submit(oi) == ExecDecision::Logged && eng.submits == 1,
          "execution engine receives the intent and returns its decision");
    check(eng.last.trace_id == ev.trace_id, "engine sees the propagated trace");
  }

  // dump_state present + non-empty.
  {
    EchoFeatureBuilder fb; NullModel m;
    check(!fb.dump_state().empty() && !m.dump_state().empty(), "dump_state non-empty");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
