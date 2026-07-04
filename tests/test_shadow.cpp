// Execution-engine safety tests: DataCollect rejects, Shadow logs (never
// transmits), Live gate throws. No network, no real orders.
//
// usage: test_shadow <scratch_dir>

#include "kalshi/env.hpp"
#include "kalshi/gateway.hpp"

#include <cstdio>
#include <fstream>
#include <iostream>
#include <string>

using namespace kalshi;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

trading::OrderIntent an_order() {
  trading::OrderIntent oi;
  oi.entity_id = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-1");
  oi.trace_id = trading::next_trace_id();
  oi.side = trading::Side::Yes;
  oi.limit_price = 5500;
  oi.size = 100;
  return oi;
}

long line_count(const std::string& path) {
  std::ifstream in(path);
  long n = 0;
  std::string l;
  while (std::getline(in, l)) if (!l.empty()) ++n;
  return n;
}

Runtime make_rt(Env env, Mode mode, bool orders_enabled) {
  Runtime rt;
  rt.env = env;
  rt.mode = mode;
  rt.read_only = mode != Mode::Live;
  rt.shadow = mode == Mode::Shadow;
  rt.orders_enabled = orders_enabled;
  return rt;
}
}  // namespace

int main(int argc, char** argv) {
  const std::string dir = argc > 1 ? argv[1] : ".";

  // --- Prod + Shadow: logs a would-be order, never transmits ---
  {
    const std::string log = dir + "/wouldbe.ndjson";
    std::remove(log.c_str());
    KalshiExecutionEngine eng(make_rt(Env::Prod, Mode::Shadow, false), log);
    const auto d = eng.submit(an_order());
    check(d == trading::ExecDecision::Logged, "Prod+Shadow submit -> Logged");
    check(eng.logged() == 1 && eng.transmitted() == 0, "shadow logged, zero transmit");
    check(line_count(log) == 1, "one would-be-order record written");
  }

  // --- DataCollect: rejects loudly, writes nothing to the order log ---
  {
    const std::string log = dir + "/wouldbe_dc.ndjson";
    std::remove(log.c_str());
    KalshiExecutionEngine eng(make_rt(Env::Prod, Mode::DataCollect, false), log);
    const auto d = eng.submit(an_order());
    check(d == trading::ExecDecision::Rejected, "DataCollect submit -> Rejected");
    check(eng.rejected() == 1 && eng.transmitted() == 0, "rejected counter incremented");
    check(line_count(log) == 0, "no would-be-order record written in data_collect");
  }

  // --- Live gate: not-enabled runtime -> require_orders_allowed throws ---
  {
    KalshiExecutionEngine eng(make_rt(Env::Demo, Mode::Live, /*orders_enabled*/ false));
    bool threw = false;
    try { eng.submit(an_order()); } catch (const SafetyViolation&) { threw = true; }
    check(threw, "Live w/o orders_enabled: submit throws (gate fires)");
    check(eng.transmitted() == 0, "nothing transmitted");
  }

  // --- Live enabled: gate passes but transmission is not wired -> fail closed ---
  {
    KalshiExecutionEngine eng(make_rt(Env::Demo, Mode::Live, /*orders_enabled*/ true));
    bool threw = false;
    try { eng.submit(an_order()); } catch (const SafetyViolation&) { threw = true; }
    check(threw, "Live enabled: transmit skeleton not wired -> throws (fail closed)");
    check(eng.transmitted() == 0, "still zero transmit (never sends this pass)");
  }

  // --- invalid/stale book execution gate (belt-and-braces, read-only) ---
  {
    OrderBookManager books;
    const trading::EntityId e = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-1");
    // Shadow engine that would normally Log; wire the book manager.
    KalshiExecutionEngine eng(make_rt(Env::Prod, Mode::Shadow, false));
    eng.set_book_manager(&books);

    trading::OrderIntent oi = an_order();
    // No book yet -> not tradeable -> rejected before shadow logging.
    check(eng.submit(oi) == trading::ExecDecision::Rejected, "no book -> rejected");
    check(eng.stale_book_rejected() == 1, "stale-book rejection counted");

    // Establish a valid book -> now the shadow path runs (Logged).
    books.bind(1, 1, e);
    books.on_snapshot(1, 1, e, kalshi::SnapshotView{{{4000, 500}}, {}, 1}, 1);
    check(eng.submit(oi) == trading::ExecDecision::Logged, "valid book -> shadow logs");

    // Drive the book invalid -> rejected again.
    books.on_delta(1, 1, e, trading::Side::Yes, 4000, -1000, 2);  // negative -> invalid
    check(eng.submit(oi) == trading::ExecDecision::Rejected, "invalid book -> rejected");
    check(eng.stale_book_rejected() == 2, "second stale-book rejection counted");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
