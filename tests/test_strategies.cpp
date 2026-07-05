// Strategy roster + once_probe tests (PLAN_LIVE_VALIDATION P3). Pure/offline:
// no network, no real orders. Exercises env-driven roster selection and the
// one-shot once_probe emit-once-then-silent behavior through a fake ExecSink.
//
// usage: test_strategies

#include "kalshi/strategy.hpp"

#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

using namespace kalshi;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

// Records everything a strategy submits.
struct RecordingSink : ExecSink {
  struct Rec {
    std::uint8_t strategy_id;
    wire::ExecPayload p;
  };
  std::vector<Rec> submitted;
  void submit(std::uint8_t strategy_id, wire::ExecPayload p) override {
    submitted.push_back({strategy_id, p});
  }
};

wire::MarketEvent event_for(std::string_view ticker) {
  wire::MarketEvent ev;
  ev.set_ticker(ticker);
  return ev;
}

void set_env(const char* k, const char* v) {
  if (v) ::setenv(k, v, 1);
  else ::unsetenv(k);
}

// Clear every env var this test touches so cases don't leak into each other.
void reset_env() {
  set_env("STRATEGIES", nullptr);
  set_env("PROBE_TICKER", nullptr);
  set_env("PROBE_PRICE_CENTS", nullptr);
  set_env("PROBE_COUNT", nullptr);
}

bool make_throws() {
  try {
    make_strategies();
    return false;
  } catch (const std::exception&) {
    return true;
  }
}

}  // namespace

int main() {
  // 1. Unset STRATEGIES => empty roster (production default: no orders).
  reset_env();
  check(make_strategies().empty(), "unset STRATEGIES -> empty roster");

  // 1b. Empty STRATEGIES string => also empty.
  reset_env();
  set_env("STRATEGIES", "");
  check(make_strategies().empty(), "empty STRATEGIES -> empty roster");

  // 2. once_probe loads with required ticker; correct id/name.
  reset_env();
  set_env("STRATEGIES", "once_probe");
  set_env("PROBE_TICKER", "KXPROBE-A");
  {
    auto roster = make_strategies();
    check(roster.size() == 1, "STRATEGIES=once_probe -> one strategy");
    check(roster.size() == 1 && std::string(roster[0]->name()) == "once_probe",
          "strategy name is once_probe");
    check(roster.size() == 1 && roster[0]->id() == 29, "once_probe slot id is 29");
  }

  // 3. Missing PROBE_TICKER => fail closed (throws).
  reset_env();
  set_env("STRATEGIES", "once_probe");
  check(make_throws(), "once_probe without PROBE_TICKER throws (fail closed)");

  // 4. Unknown strategy name => fail closed (throws).
  reset_env();
  set_env("STRATEGIES", "does_not_exist");
  check(make_throws(), "unknown strategy name throws (fail closed)");

  // 5. Emit-once behavior: only the matching ticker fires, exactly once.
  reset_env();
  set_env("STRATEGIES", "once_probe");
  set_env("PROBE_TICKER", "KXPROBE-A");
  set_env("PROBE_PRICE_CENTS", "7");
  set_env("PROBE_COUNT", "3");
  {
    auto roster = make_strategies();
    RecordingSink sink;
    check(roster.size() == 1, "roster built for emit test");

    // Non-matching ticker: no order.
    auto other = event_for("KXPROBE-B");
    roster[0]->on_event(other, sink);
    check(sink.submitted.empty(), "non-matching ticker -> no order");

    // Matching ticker: exactly one order with the configured fields.
    auto match = event_for("KXPROBE-A");
    roster[0]->on_event(match, sink);
    check(sink.submitted.size() == 1, "matching ticker -> exactly one order");
    if (sink.submitted.size() == 1) {
      const auto& r = sink.submitted[0];
      check(r.strategy_id == 29, "submitted with strategy_id 29");
      check(r.p.action == wire::kActionBuy, "order action = buy");
      check(r.p.side == wire::kSideYes, "order side = yes");
      check(r.p.order_type == wire::kTypeLimit, "order type = limit");
      check(r.p.count == 3, "order count from PROBE_COUNT");
      check(r.p.price_cents == 7, "order price from PROBE_PRICE_CENTS");
      check(r.p.ticker_view() == "KXPROBE-A", "order ticker = PROBE_TICKER");
    }

    // Further events (matching or not): silent forever.
    roster[0]->on_event(match, sink);
    roster[0]->on_event(match, sink);
    roster[0]->on_event(other, sink);
    check(sink.submitted.size() == 1, "once_probe stays silent after the one shot");
  }

  // 6. Defaults: PROBE_PRICE_CENTS/PROBE_COUNT default to 1 when unset.
  reset_env();
  set_env("STRATEGIES", "once_probe");
  set_env("PROBE_TICKER", "KXPROBE-A");
  {
    auto roster = make_strategies();
    RecordingSink sink;
    auto match = event_for("KXPROBE-A");
    if (!roster.empty()) roster[0]->on_event(match, sink);
    check(sink.submitted.size() == 1 && sink.submitted[0].p.count == 1,
          "default PROBE_COUNT = 1");
    check(sink.submitted.size() == 1 && sink.submitted[0].p.price_cents == 1,
          "default PROBE_PRICE_CENTS = 1");
  }

  reset_env();
  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
