// Shadow MM engine acceptance tests (ACCEPTANCE_影子做市引擎 items 1-9).
// Pure: no sockets, no files. Prints "ALL PASS" on success.

#include "trading/shadow/engine.hpp"

#include <cstdio>
#include <cstdlib>
#include <string>

using namespace trading::shadow;

static int g_fail = 0;
#define CHECK(cond, msg)                                        \
  do {                                                          \
    if (!(cond)) {                                              \
      std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, msg);  \
      ++g_fail;                                                 \
    }                                                           \
  } while (0)

static FeeSchedule test_fees() {
  FeeSchedule f;
  // Injected TEST schedule (never a ratified number): 0.0175 -> rate_e4=175.
  f.add(FeeEntry{"KA-", 175, 700, "unit-test", "n/a"});
  return f;
}

// --- FSM legality -----------------------------------------------------------
static void test_fsm() {
  CHECK(legal_transition(OrdState::Free, OrdState::PendingNew), "free->pnew");
  CHECK(legal_transition(OrdState::PendingNew, OrdState::Open), "pnew->open");
  CHECK(legal_transition(OrdState::Open, OrdState::PendingCancel), "open->pc");
  CHECK(legal_transition(OrdState::PendingCancel, OrdState::Filled),
        "cancel race: fill can win");
  CHECK(!legal_transition(OrdState::Filled, OrdState::Open), "terminal");
  CHECK(!legal_transition(OrdState::Free, OrdState::Open), "no skip");
}

// --- queue conservation (item 1/2): no fill until queue ahead is eaten ------
static void test_queue_conservation() {
  FeeSchedule fees = test_fees();
  ShadowEngine eng(fees, Track::Queue, SpeedParams{0, 0});
  const auto m = eng.market("KA-Q", "test");
  // Resting buy YES @40 with 100 contracts displayed ahead of us.
  eng.place(m, 40, 10, /*buy_yes=*/true, 1'000, /*displayed=*/100);
  // At-level sell prints totalling 100 must drain the queue, filling nothing.
  eng.on_trade(m, 40, 60, /*taker_yes=*/false, 2'000);
  eng.on_trade(m, 40, 40, false, 3'000);
  CHECK(eng.ledger().fills() == 0, "queue ahead must fill first");
  // The next at-level print reaches us.
  eng.on_trade(m, 40, 25, false, 4'000);
  CHECK(eng.ledger().fills() == 1, "queue drained -> we fill");
  eng.on_settle(m, /*yes=*/false, 5'000);
  // Bought 10 YES @40, settle NO: gross = -400c; fee(175,10,40) = ceil(4.2)=5.
  const auto rep = eng.ledger().report();
  CHECK(rep.at("test").gross_pnl_cents == -400, "gross settle math");
  CHECK(rep.at("test").net_pnl_cents == -405, "net = gross - real fee");
}

// --- strict track: at-level prints never fill, strictly-through does --------
static void test_strict_through() {
  FeeSchedule fees = test_fees();
  ShadowEngine eng(fees, Track::Strict, SpeedParams{0, 0});
  const auto m = eng.market("KA-S", "test");
  eng.place(m, 40, 10, true, 1'000);
  eng.on_trade(m, 40, 500, false, 2'000);  // at-level: must NOT fill
  CHECK(eng.ledger().fills() == 0, "strict ignores at-level prints");
  eng.on_trade(m, 39, 10, false, 3'000);   // strictly through: fills
  CHECK(eng.ledger().fills() == 1, "strict fills on trade-through");
}

// --- timing (item 4): activation latency + cancel window --------------------
static void test_latency_windows() {
  FeeSchedule fees = test_fees();
  ShadowEngine eng(fees, Track::Strict,
                   SpeedParams{/*place*/ 1'000, /*cancel*/ 1'000});
  const auto m = eng.market("KA-L", "test");
  const auto cid = eng.place(m, 40, 10, true, 1'000);  // active at 2'000
  eng.on_trade(m, 39, 10, false, 1'500);
  CHECK(eng.ledger().fills() == 0, "no fill before activation");
  eng.on_trade(m, 39, 3, false, 2'500);
  CHECK(eng.ledger().fills() == 1, "fills once active");
  eng.cancel(cid, 3'000);  // dead at 4'000
  eng.on_trade(m, 39, 3, false, 3'500);
  CHECK(eng.ledger().fills() == 2, "cancel window still fills (exposure)");
  eng.on_trade(m, 39, 3, false, 4'500);
  CHECK(eng.ledger().fills() == 2, "post-cancel-effective never fills");
}

// --- fee fail-closed (item 5 / WO-E) ----------------------------------------
static void test_fee_fail_closed() {
  FeeSchedule fees = test_fees();  // only KA- is known
  ShadowEngine eng(fees, Track::Strict, SpeedParams{0, 0});
  const auto m = eng.market("UNKNOWN-SERIES", "mystery");
  eng.place(m, 40, 10, true, 1'000);
  eng.on_trade(m, 39, 10, false, 2'000);
  eng.on_settle(m, true, 3'000);
  const auto rep = eng.ledger().report();
  CHECK(rep.at("mystery").net_pnl_cents == 0,
        "unknown fee contributes no net-PnL claim");
  CHECK(rep.at("mystery").excluded_fee_unknown_markets == 1,
        "exclusion is counted, not silent");
  CHECK(rep.at("mystery").gross_pnl_cents == 600, "gross still reported");
}

// --- known-answer (item 8): mechanical 99c convergence must lose ------------
static std::int64_t run_convergence(Track track) {
  FeeSchedule fees = test_fees();
  ShadowEngine eng(fees, track, SpeedParams{1'000'000, 1'000'000});
  SimpleQuoter quoter(eng, /*off=*/2, /*size=*/5, /*cap=*/200,
                      /*quote_buy_side=*/false);
  const auto m = eng.market("KA-CONV99", "known_answer");
  // Mechanical convergence: relentless taker YES buying grinds 90 -> 99.
  std::uint64_t ts = 1'000'000;
  for (int px = 90; px <= 99; ++px) {
    for (int k = 0; k < 30; ++k) {
      ts += 2'000'000;
      // Buy pressure prints at px, then sweeps THROUGH the maker's ask level
      // (px+3 > our px+2 ask) — the flow that eats an ask ladder on the way up.
      eng.on_trade(m, px, 20, true, ts);
      quoter.on_trade(m, px, ts);
      ts += 2'000'000;
      eng.on_trade(m, px + 3 <= 99 ? px + 3 : 99, 20, true, ts);
    }
  }
  eng.on_settle(m, /*yes=*/true, ts + 1'000);
  return eng.ledger().report().at("known_answer").net_pnl_cents;
}

static void test_known_answer() {
  const std::int64_t strict = run_convergence(Track::Strict);
  const std::int64_t optimistic = run_convergence(Track::Optimistic);
  CHECK(strict < 0, "KNOWN-ANSWER: strict track must LOSE on 99c convergence");
  CHECK(optimistic <= strict,
        "optimistic must look at least as bad here (sells more into the rise)"
        " — if optimistic ever looks BETTER while strict profits, fills are"
        " being invented");
  // The engine-is-broken tripwire: a maker fading mechanical convergence and
  // showing a PROFIT on any track = fabricated fills. Fix the engine first.
  CHECK(!(strict > 0), "engine shows profit where loss is certain -> broken");
}

// --- determinism (D3-D01 spirit): identical runs, identical ledger ----------
static void test_determinism() {
  const std::int64_t a = run_convergence(Track::Strict);
  const std::int64_t b = run_convergence(Track::Strict);
  CHECK(a == b, "same tape + params => same PnL, bit for bit");
}

// --- exposure gauge: O(1) risk reads track open orders ----------------------
static void test_exposure_gauge() {
  FeeSchedule fees = test_fees();
  ShadowEngine eng(fees, Track::Strict, SpeedParams{0, 0});
  const auto m = eng.market("KA-E", "test");
  eng.place(m, 40, 10, true, 1'000);       // risk = 10 * 40 = 400c
  eng.on_trade(m, 50, 1, true, 1'001);     // tick to activate (no cross)
  CHECK(eng.registry().gauge().market_at_risk_cents(m) == 400,
        "gauge tracks open buy risk");
  CHECK(eng.registry().gauge().within(m, 400, 400), "within cap");
  CHECK(!eng.registry().gauge().within(m, 399, 4000), "over per-market cap");
  eng.on_trade(m, 39, 10, false, 2'000);   // full fill
  CHECK(eng.registry().gauge().market_at_risk_cents(m) == 0,
        "fill releases exposure");
}

int main() {
  test_fsm();
  test_queue_conservation();
  test_strict_through();
  test_latency_windows();
  test_fee_fail_closed();
  test_known_answer();
  test_determinism();
  test_exposure_gauge();
  if (g_fail) {
    std::printf("%d FAILURES\n", g_fail);
    return 1;
  }
  std::printf("ALL PASS\n");
  return 0;
}
