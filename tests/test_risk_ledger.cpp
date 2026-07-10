// W-K3 acceptance — the five-layer reservation ledger.
//
// The four NAMED tests (verbatim from the design doc; absence of any one is an
// audit REJECT): Eggsy replay, NHL template replay, deadlock exemption, refund
// path. Plus the property battery: multi-threaded atomicity (no "look then
// send" over-commit), reservation conservation, unknown_factor fail-closed,
// client_order_id retry-stability, and the RED-FIRST (1)(4)(5)-only proof that
// the event+factor layers are load-bearing. Pure C++, zero I/O.

#include "kalshi/risk_ledger.hpp"

#include <atomic>
#include <fstream>
#include <limits>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

using namespace kalshi::risk;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

// exposure of one contract at $0.50 in E6: CountFp(1.00)=100, PriceE4(0.50)=5000
// -> 100 * 5000 = 500000 micros = $0.50. One "line" below = this unit.
constexpr Micros kLine = 500000;  // $0.50 exposure
}  // namespace

// (a) Eggsy replay: ten correlated same-EVENT lines, same direction => one
// shared factor. Factor cap = 1 line; market & event caps generous. Exactly
// the first reserves; the other nine are rejected AT THE FACTOR LAYER.
static void test_eggsy_replay() {
  RiskLedger::Caps caps;
  caps.per_market = 100 * kLine;   // generous: isolate the factor layer
  caps.per_event = 100 * kLine;
  caps.per_factor = 1 * kLine;     // the binding constraint
  caps.total = 100 * kLine;
  caps.day_loss = kNoCap;
  RiskLedger led(caps);

  int admitted = 0, rejected_factor = 0;
  for (int i = 0; i < 10; ++i) {
    OrderReq req;
    req.market = "KXEGGSY-25DEC31-L" + std::to_string(i);  // ten distinct lines
    req.event = "KXEGGSY-25DEC31";                         // same event
    req.factor = "KXEGGSY-25DEC31:yes";                    // same underlying+dir
    req.exposure = kLine;
    Reservation r = led.reserve(req);
    if (r.ok) ++admitted;
    else if (r.rejected == Layer::Factor) ++rejected_factor;
  }
  check(admitted == 1, "eggsy: exactly one line admitted");
  check(rejected_factor == 9, "eggsy: nine rejected at the FACTOR layer");
  check(led.used_factor("KXEGGSY-25DEC31:yes") == kLine,
        "eggsy: factor bucket holds exactly one line");
}

// (b) NHL template replay: ONE leg threaded into N different combos (distinct
// events/markets) all map to the SAME factor; leg-level exposure aggregates
// across combos and caps out. Factor cap = 3 lines; 6 combos => 3 admitted,
// 3 rejected at the factor layer.
static void test_nhl_template_replay() {
  RiskLedger::Caps caps;
  caps.per_market = 100 * kLine;
  caps.per_event = 100 * kLine;
  caps.per_factor = 3 * kLine;
  caps.total = 100 * kLine;
  caps.day_loss = kNoCap;
  RiskLedger led(caps);

  int admitted = 0, rejected_factor = 0;
  for (int combo = 0; combo < 6; ++combo) {
    OrderReq req;
    req.market = "KXNHL-COMBO" + std::to_string(combo) + "-LEGX";  // distinct
    req.event = "KXNHL-COMBO" + std::to_string(combo);             // distinct
    req.factor = "NHL:TEAMX:win";     // the shared leg across all combos
    req.exposure = kLine;
    Reservation r = led.reserve(req);
    if (r.ok) ++admitted;
    else if (r.rejected == Layer::Factor) ++rejected_factor;
  }
  check(admitted == 3, "nhl: leg admitted up to the factor cap (3)");
  check(rejected_factor == 3, "nhl: further combos rejected at the FACTOR layer");
}

// (c) deadlock exemption: with the factor layer AT cap, a reduce-direction
// order is still admitted (Q8 — the exit quote is never suppressed).
static void test_deadlock_exemption() {
  RiskLedger::Caps caps;
  caps.per_market = kNoCap; caps.per_event = kNoCap;
  caps.per_factor = 1 * kLine; caps.total = kNoCap; caps.day_loss = kNoCap;
  RiskLedger led(caps);

  OrderReq open;
  open.market = "M"; open.event = "E"; open.factor = "F"; open.exposure = kLine;
  check(led.reserve(open).ok, "deadlock: factor filled to cap");

  OrderReq more = open;
  check(!led.reserve(more).ok, "deadlock: a NEW-risk order is now blocked");

  OrderReq exit = open;
  exit.reduces_risk = true;                 // reduce-direction
  Reservation r = led.reserve(exit);
  check(r.ok && r.reduce && r.amount == 0,
        "deadlock: the reduce-risk (exit) order is ADMITTED, reserves nothing");
  check(led.used_factor("F") == kLine,
        "deadlock: exit did not consume factor budget");
}

// (d) refund path: send-failure / timeout / partial fill each restore the
// account EXACTLY (integer equality), and reserved == settled + refunded.
static void test_refund_path() {
  RiskLedger::Caps caps;
  caps.per_market = 10 * kLine; caps.per_event = 10 * kLine;
  caps.per_factor = 10 * kLine; caps.total = 10 * kLine; caps.day_loss = kNoCap;
  RiskLedger led(caps);
  auto mk = [&](Micros e) {
    OrderReq q; q.market = "M"; q.event = "E"; q.factor = "F"; q.exposure = e;
    return led.reserve(q);
  };

  // send failure -> full refund -> everything back to zero, exactly
  Reservation a = mk(3 * kLine);
  check(a.ok && led.used_total() == 3 * kLine, "refund: reserved 3 lines");
  led.refund(a);
  check(led.used_total() == 0 && led.used_market("M") == 0 &&
        led.used_event("E") == 0 && led.used_factor("F") == 0,
        "refund: send-failure returns ALL layers to exactly zero");
  check(a.settled == 0 && a.settled + (a.amount - a.settled) == a.amount,
        "refund: conservation reserved == settled + refunded");

  // partial fill -> keep filled, refund the remainder, exact integers
  Reservation b = mk(5 * kLine);
  led.settle(b, 2 * kLine);           // 2 filled, 3 refunded
  check(led.used_total() == 2 * kLine, "refund: partial keeps exactly the fill");
  check(b.settled == 2 * kLine, "refund: partial settled == filled");
  const Micros refunded = b.amount - b.settled;
  check(refunded == 3 * kLine && b.settled + refunded == b.amount,
        "refund: partial conservation reserved == settled + refunded");

  // timeout after zero fill -> full refund; double-settle is a no-op
  Reservation c = mk(4 * kLine);
  led.settle(c, 0);
  check(led.used_total() == 2 * kLine, "refund: timeout (0 fill) refunds all");
  led.refund(c);                       // second close: must not double-refund
  check(led.used_total() == 2 * kLine, "refund: double-close is a no-op");
}

// property: five-layer atomicity under a concurrent hammer — no interleaving
// admits a breach ("look then send" impossible). Many threads race to reserve
// against a tight TOTAL cap; the sum of admitted exposure must never exceed it.
static void test_atomicity_hammer() {
  RiskLedger::Caps caps;
  caps.per_market = kNoCap; caps.per_event = kNoCap; caps.per_factor = kNoCap;
  caps.total = 50 * kLine; caps.day_loss = kNoCap;
  RiskLedger led(caps);

  std::atomic<int> admitted{0};
  std::vector<std::thread> ts;
  for (int t = 0; t < 8; ++t) {
    ts.emplace_back([&, t] {
      for (int i = 0; i < 100; ++i) {
        OrderReq q;
        q.market = "M" + std::to_string(t);   // distinct markets: only TOTAL binds
        q.event = "E" + std::to_string(t);
        q.factor = "F" + std::to_string(t) + "-" + std::to_string(i);
        q.exposure = kLine;
        if (led.reserve(q).ok) admitted.fetch_add(1);
      }
    });
  }
  for (auto& th : ts) th.join();
  check(led.used_total() <= caps.total,
        "hammer: total exposure never exceeds the cap");
  check(admitted.load() == 50 && led.used_total() == 50 * kLine,
        "hammer: exactly cap/line reservations admitted, no over-commit");
}

// unknown_factor fail-closed: orders with an EMPTY factor key share ONE
// bucket, so they aggregate and throttle together (never each slip through).
static void test_unknown_factor_failclosed() {
  RiskLedger::Caps caps;
  caps.per_market = kNoCap; caps.per_event = kNoCap;
  caps.per_factor = kNoCap;          // generous known-factor default...
  caps.total = kNoCap; caps.day_loss = kNoCap;
  RiskLedger led(caps);
  // ...but the unknown bucket has its OWN conservative default (N4): with no
  // operator override it is 0 => every unclassifiable order is REJECTED.
  int admitted_default = 0;
  for (int i = 0; i < 3; ++i) {
    OrderReq q; q.market = "M" + std::to_string(i);
    q.event = "E" + std::to_string(i); q.factor = ""; q.exposure = kLine;
    if (led.reserve(q).ok) ++admitted_default;
  }
  check(admitted_default == 0,
        "unknown_factor: fail-CLOSED by default (0 cap) — unclassifiable "
        "orders rejected until the operator widens the bucket");

  // when the operator DELIBERATELY widens the unknown bucket, they still
  // share ONE bucket and throttle together (aggregation shape).
  RiskLedger::Caps caps2 = caps;
  RiskLedger led2(caps2);
  led2.set_factor_cap(kUnknownFactor, 2 * kLine);
  int admitted = 0;
  for (int i = 0; i < 5; ++i) {
    OrderReq q; q.market = "M" + std::to_string(i);
    q.event = "E" + std::to_string(i); q.factor = ""; q.exposure = kLine;
    if (led2.reserve(q).ok) ++admitted;
  }
  check(admitted == 2, "unknown_factor: widened bucket still aggregates all "
        "unclassifiable orders into one cap");
  check(led2.used_factor("") == 2 * kLine,
        "unknown_factor: they land in the __unknown_factor__ bucket");
}

// N2/N3: a negative or overflow-magnitude exposure is REJECTED, never
// admitted into phantom headroom (S2 fail-closed).
static void test_bad_exposure_rejected() {
  RiskLedger::Caps caps;
  caps.per_market = 10 * kLine; caps.per_event = 10 * kLine;
  caps.per_factor = 10 * kLine; caps.total = 10 * kLine; caps.day_loss = kNoCap;
  RiskLedger led(caps);
  OrderReq neg; neg.market="M"; neg.event="E"; neg.factor="F";
  neg.exposure = -5 * kLine;
  Reservation rn = led.reserve(neg);
  check(!rn.ok && rn.rejected == Layer::Invalid,
        "bad-exposure: negative exposure rejected as Invalid");
  check(led.used_total() == 0 && led.used_market("M") == 0,
        "bad-exposure: negative order deducted NOTHING (no phantom headroom)");
  // overflow magnitude: used+e cannot wrap because headroom is checked by
  // subtraction; a near-INT64_MAX exposure simply exceeds headroom -> reject
  OrderReq huge; huge.market="M"; huge.event="E"; huge.factor="F";
  huge.exposure = std::numeric_limits<Micros>::max() - 100;
  Reservation rh = led.reserve(huge);
  check(!rh.ok && rh.rejected == Layer::Market,
        "bad-exposure: overflow-magnitude exposure rejected (no wrap-to-room)");
  check(led.used_total() == 0, "bad-exposure: huge order deducted nothing");
}

// N1: a COPIED reservation handle cannot double-release. Closing an alias
// after the original is a no-op — used_ never drifts negative.
static void test_copied_handle_no_double_release() {
  RiskLedger::Caps caps;
  caps.per_market = 10*kLine; caps.per_event=10*kLine; caps.per_factor=10*kLine;
  caps.total = 10*kLine; caps.day_loss = kNoCap;
  RiskLedger led(caps);
  OrderReq q; q.market="M"; q.event="E"; q.factor="F"; q.exposure = kLine;
  Reservation a = led.reserve(q);
  Reservation b = a;                 // alias the handle
  led.refund(a);
  led.refund(b);                     // second close via the copy: no-op
  check(led.used_total() == 0 && led.used_market("M") == 0,
        "double-release: copied-handle second refund does NOT drift below zero");
  // same for settle-then-settle-via-copy
  Reservation c = led.reserve(q);
  Reservation d = c;
  led.settle(c, kLine);              // fully filled: keeps kLine
  led.settle(d, 0);                  // alias: must not release the kept amount
  check(led.used_total() == kLine,
        "double-release: aliased settle does not release the kept fill");
}

// day-loss breaker (5): once realized loss reaches the cap, new risk refused.
static void test_day_loss_breaker() {
  RiskLedger::Caps caps;
  caps.per_market = kNoCap; caps.per_event = kNoCap; caps.per_factor = kNoCap;
  caps.total = kNoCap; caps.day_loss = 2 * kLine;
  RiskLedger led(caps);
  OrderReq q; q.market = "M"; q.event = "E"; q.factor = "F"; q.exposure = kLine;
  Reservation r = led.reserve(q);
  led.settle(r, kLine, /*realized_loss=*/2 * kLine);   // book a loss to the cap
  Reservation r2 = led.reserve(q);
  check(!r2.ok && r2.rejected == Layer::DayLoss,
        "day_loss: breaker refuses new risk once the loss cap is hit");
  OrderReq exit = q; exit.reduces_risk = true;
  check(led.reserve(exit).ok,
        "day_loss: reduce-risk still admitted with the breaker tripped (Q8)");
}

// client_order_id (contract #9): stable across retries reusing the SAME
// reservation, distinct across distinct slots.
static void test_client_order_id_stability() {
  RiskLedger led(RiskLedger::Caps{});   // all kNoCap
  OrderReq q; q.market = "M"; q.event = "E"; q.factor = "F"; q.exposure = kLine;
  Reservation a = led.reserve(q, /*ts_ns=*/1000);
  Reservation b = led.reserve(q, /*ts_ns=*/1000);
  const std::string ida1 = led.client_order_id(a, 29);
  const std::string ida2 = led.client_order_id(a, 29);        // retry, same slot
  const std::string idb = led.client_order_id(b, 29);
  check(ida1 == ida2, "coid: retry with the same reservation yields the SAME id");
  check(ida1 != idb, "coid: distinct reservations yield distinct ids");
  // N5: the id is a pure function of the slot+captured ts — a copied handle
  // (a retry path) yields the SAME id even though no ts is passed at call time
  Reservation a_copy = a;
  check(led.client_order_id(a_copy, 29) == ida1,
        "coid: an aliased handle (retry) yields the SAME id (slot-pure)");
}

// N6: the canonical factor-key derivation is implemented (not just documented)
// — same event + same direction => same key (aggregates); different direction
// or empty event => different / unknown.
static void test_factor_key_derivation() {
  check(derive_factor_key("KXEGGSY-25DEC31", true) ==
        derive_factor_key("KXEGGSY-25DEC31", true),
        "factor-derive: same event+direction -> same key");
  check(derive_factor_key("KXEGGSY-25DEC31", true) !=
        derive_factor_key("KXEGGSY-25DEC31", false),
        "factor-derive: opposite directions -> distinct keys");
  check(derive_factor_key("", true).empty(),
        "factor-derive: empty event -> empty key (routes to unknown_factor)");
  // end-to-end: derived keys drive the Eggsy aggregation
  RiskLedger::Caps caps;
  caps.per_market = 100*kLine; caps.per_event=100*kLine;
  caps.per_factor = 1*kLine; caps.total=100*kLine; caps.day_loss=kNoCap;
  RiskLedger led(caps);
  int admitted = 0;
  for (int i = 0; i < 5; ++i) {
    OrderReq q; q.market = "KXEGGSY-25DEC31-L"+std::to_string(i);
    q.event = "KXEGGSY-25DEC31";
    q.factor = derive_factor_key(q.event, /*long_yes=*/true);   // canonical
    q.exposure = kLine;
    if (led.reserve(q).ok) ++admitted;
  }
  check(admitted == 1, "factor-derive: derived keys drive factor aggregation");
}

// RED-FIRST: the roadmap's old (1)(4)(5)-only list (event+factor disabled)
// CANNOT stop the factor blowup — legacy mode admits ALL ten Eggsy lines.
// This is the proof that layers (2)(3) are load-bearing, not decorative.
static void test_redfirst_legacy_layers_fail() {
  RiskLedger::Caps caps;
  caps.per_market = 100 * kLine;
  caps.per_event = kNoCap;      // (2) disabled  -> legacy config
  caps.per_factor = kNoCap;     // (3) disabled
  caps.total = 100 * kLine;     // (4) generous
  caps.day_loss = kNoCap;       // (5) off
  RiskLedger led(caps);
  int admitted = 0;
  for (int i = 0; i < 10; ++i) {
    OrderReq req;
    req.market = "KXEGGSY-25DEC31-L" + std::to_string(i);
    req.event = "KXEGGSY-25DEC31";
    req.factor = "KXEGGSY-25DEC31:yes";
    req.exposure = kLine;
    if (led.reserve(req).ok) ++admitted;
  }
  check(admitted == 10,
        "red-first: (1)(4)(5)-only admits ALL 10 correlated lines "
        "(the blowup layers 2+3 prevent)");
}

// D5 no-float gate over the LEDGER HEADER (the money path — the test file is
// test scaffolding, like gold_load's gate scopes to the tool source). Matches
// the C++ type tokens as WHOLE WORDS in code with // comments stripped, so
// English words ("double-close") and message strings can't false-trip it.
static bool word_present(const std::string& code, const std::string& tok) {
  size_t pos = 0;
  auto is_ident = [](char ch) {
    return (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
           (ch >= '0' && ch <= '9') || ch == '_';
  };
  while ((pos = code.find(tok, pos)) != std::string::npos) {
    const bool lb = pos == 0 || !is_ident(code[pos - 1]);
    const size_t after = pos + tok.size();
    const bool rb = after >= code.size() || !is_ident(code[after]);
    if (lb && rb) return true;
    pos = after;
  }
  return false;
}

static void test_no_float_grep_gate() {
  const char* f = "include/kalshi/risk_ledger.hpp";
  std::ifstream in(f);
  bool clean = in.good();
  if (!clean) std::cout << "  cannot open " << f << " (run from repo root)\n";
  std::string line;
  while (std::getline(in, line)) {
    auto c = line.find("//");
    std::string code = c == std::string::npos ? line : line.substr(0, c);
    if (word_present(code, "float") || word_present(code, "double")) {
      std::cout << "  float/double token in " << f << ": " << line << "\n";
      clean = false;
    }
  }
  check(clean, "no-float gate: the ledger header carries no float/double (money is E6 int)");
}

int main() {
  test_eggsy_replay();
  test_nhl_template_replay();
  test_deadlock_exemption();
  test_refund_path();
  test_atomicity_hammer();
  test_unknown_factor_failclosed();
  test_bad_exposure_rejected();
  test_copied_handle_no_double_release();
  test_day_loss_breaker();
  test_client_order_id_stability();
  test_factor_key_derivation();
  test_redfirst_legacy_layers_fail();
  test_no_float_grep_gate();
  std::cout << (g_failures == 0 ? "ALL PASS\n" : "TEST FAIL\n");
  return g_failures == 0 ? 0 : 1;
}
