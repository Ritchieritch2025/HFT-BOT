// W-K4 acceptance — the defensive rule engine, driven by four hand-written
// scenario tapes with hand-computed expected decision sequences. Pure C++,
// zero I/O, transmits nothing. The day-loss tape wires the REAL RiskLedger
// layer-⑤ breaker, so the "ledger ⑤ trip => quote-stop + panic" path is
// demonstrated end-to-end, not stubbed.
//
// Scenario tapes are IN-CODE (a recorded scope choice): the four scenarios are
// compact and deterministic, and hand-asserting them in the test is equivalent
// to external fixture files while keeping the acceptance self-contained.

#include "kalshi/rule_engine.hpp"
#include "kalshi/risk_ledger.hpp"

#include <iostream>
#include <string>
#include <vector>

using namespace kalshi::rules;
namespace risk = kalshi::risk;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
int count_action(const std::vector<Decision>& ds, Action a) {
  int n = 0;
  for (auto& d : ds) if (d.action == a) ++n;
  return n;
}
constexpr std::uint64_t kMs = 1'000'000ULL;   // ns per ms
}  // namespace

// ── tape 1: disconnect mid-quote => Cancel for EVERY resting order (S6) ──
static void tape_disconnect() {
  RuleEngine eng(RuleEngine::Config{});
  eng.add_resting({"o1", 0});
  eng.add_resting({"o2", 0});
  eng.add_resting({"o3", 0});
  auto ds = eng.on_disconnect(1000);
  check(count_action(ds, Action::Cancel) == 3,
        "disconnect: every resting order gets a Cancel (S6)");
  check(eng.resting_count() == 0,
        "disconnect: resting set cleared (no silent survivors)");
  // no resting -> disconnect is a clean no-op
  check(eng.on_disconnect(2000).empty(), "disconnect: empty book -> no decisions");
}

// ── tape 2: heartbeat loss => dead-man expires ALL resting; per-order TTL
//    expires only the individually-stale one ─────────────────────────────
static void tape_heartbeat_loss() {
  RuleEngine::Config cfg;
  cfg.dead_man_window_ns = 500 * kMs;   // 500ms without a heartbeat = dead
  RuleEngine eng(cfg);
  eng.on_heartbeat(0);
  eng.add_resting({"o1", 0});
  eng.add_resting({"o2", 0});

  // still within the window: no expiry
  auto ds0 = eng.on_tick(400 * kMs, /*day_loss_tripped=*/false);
  check(count_action(ds0, Action::Expire) == 0,
        "dead_man: within the window, nothing expires");

  // past the window with no fresh heartbeat: ALL resting expire
  auto ds1 = eng.on_tick(600 * kMs, false);
  check(count_action(ds1, Action::Expire) == 2,
        "dead_man: heartbeat lost -> ALL resting orders Expire");
  check(eng.resting_count() == 0, "dead_man: expired orders forgotten");

  // per-order TTL path: only the stale order expires, the fresh one stays
  RuleEngine eng2(cfg);
  eng2.on_heartbeat(0);
  eng2.add_resting({"stale", 100 * kMs});
  eng2.add_resting({"fresh", 900 * kMs});
  auto ds2 = eng2.on_tick(200 * kMs, false);   // heartbeat still fresh here
  check(count_action(ds2, Action::Expire) == 1 && ds2[0].order_id == "stale",
        "dead_man: a single past-TTL order expires, others survive");
  check(eng2.resting_count() == 1, "dead_man: fresh order still resting");
}

// ── tape 3: day-loss breach mid-burst => QuoteStop on new quotes + a
//    one-shot PanicRecommend; breaker trips BETWEEN updates (Q8). Wired to
//    the REAL RiskLedger layer-⑤. ─────────────────────────────────────────
static void tape_day_loss_breach() {
  // ledger with a day-loss cap of $1.00 (1e6 micros)
  risk::RiskLedger::Caps caps;
  caps.per_market = risk::kNoCap; caps.per_event = risk::kNoCap;
  caps.per_factor = risk::kNoCap; caps.total = risk::kNoCap;
  caps.day_loss = 1'000'000;      // $1.00
  risk::RiskLedger led(caps);
  auto tripped = [&] { return led.day_loss() >= caps.day_loss; };

  RuleEngine eng(RuleEngine::Config{});
  eng.on_heartbeat(0);

  // before the breach: quotes admit (budget is generous)
  check(eng.on_quote_intent(1 * kMs, tripped()).action == Action::Admit,
        "day_loss: quotes admit before the breach");

  // book a loss to the cap (mid-burst) — the breaker trips on the LEDGER
  risk::OrderReq q; q.market = "M"; q.event = "E"; q.factor = "F";
  q.exposure = 500000;
  risk::Reservation r = led.reserve(q);
  led.settle(r, 500000, /*realized_loss=*/1'000'000);   // hit the day-loss cap
  check(tripped(), "day_loss: ledger layer-5 breaker is now tripped");

  // a NEW quote intent is now refused with QuoteStop
  Decision qd = eng.on_quote_intent(2 * kMs, tripped());
  check(qd.action == Action::QuoteStop,
        "day_loss: new quotes get QuoteStop after the breach");

  // Q8: a bare TICK (no requote) between book updates surfaces the breaker +
  // a one-shot PanicRecommend
  auto t1 = eng.on_tick(3 * kMs, tripped());
  check(count_action(t1, Action::QuoteStop) == 1,
        "day_loss: breaker trips on a tick, between book updates (Q8)");
  check(count_action(t1, Action::PanicRecommend) == 1,
        "day_loss: a PanicRecommend is surfaced");
  auto t2 = eng.on_tick(4 * kMs, tripped());
  check(count_action(t2, Action::PanicRecommend) == 0,
        "day_loss: PanicRecommend is ONE-SHOT (a burst doesn't spam it)");
  check(count_action(t2, Action::QuoteStop) == 1,
        "day_loss: QuoteStop keeps firing while tripped");
}

// ── tape 4: rate-limit saturation => requotes SHED, but cancels NEVER shed
//    (cancel starvation is the classic incident) ──────────────────────────
static void tape_rate_saturation() {
  RuleEngine::Config cfg;
  cfg.write_rate = 0;        // no refill within the test window
  cfg.write_capacity = 3;    // budget = 3 quote intents
  cfg.requote_cost = 1;
  RuleEngine eng(cfg);

  int admitted = 0, shed = 0;
  for (int i = 0; i < 10; ++i) {
    Decision d = eng.on_quote_intent(i * kMs, /*day_loss_tripped=*/false);
    if (d.action == Action::Admit) ++admitted;
    else if (d.action == Action::Shed) ++shed;
    // interleave a cancel intent every iteration — it must ALWAYS admit
    Decision c = eng.on_cancel_intent("c" + std::to_string(i), i * kMs);
    check(c.action == Action::Cancel,
          "rate_limit: cancel #" + std::to_string(i) +
          " admitted even under saturation");
  }
  check(admitted == 3, "rate_limit: exactly the budget (3) quotes admitted");
  check(shed == 7, "rate_limit: the rest (7) are SHED, never silently dropped");
}

int main() {
  tape_disconnect();
  tape_heartbeat_loss();
  tape_day_loss_breach();
  tape_rate_saturation();
  std::cout << (g_failures == 0 ? "ALL PASS\n" : "TEST FAIL\n");
  return g_failures == 0 ? 0 : 1;
}
