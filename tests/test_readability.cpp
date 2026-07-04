// Readability tests: enum stringification, operator<< non-empty/parseable,
// dump_state() reflects state, invalid book renders as INVALID.

#include "kalshi/format.hpp"
#include "kalshi/orderbook.hpp"
#include "trading/format.hpp"
#include "trading/test_doubles.hpp"

#include <iostream>
#include <sstream>
#include <string>

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
bool has(const std::string& hay, const std::string& needle) {
  return hay.find(needle) != std::string::npos;
}
template <class T>
std::string str(const T& v) {
  std::ostringstream os;
  os << v;
  return os.str();
}
}  // namespace

int main() {
  using namespace trading;

  // enum stringification
  check(std::string(to_string(SourceId::Kalshi)) == "Kalshi", "SourceId to_string");
  check(std::string(to_string(kalshi::Env::Prod)) == "prod", "Env to_string");
  check(std::string(to_string(kalshi::Mode::Shadow)) == "shadow", "Mode to_string");
  check(std::string(to_string(Side::No)) == "No", "Side to_string");
  check(std::string(to_string(Kind::BookDelta)) == "BookDelta", "Kind to_string");
  check(std::string(to_string(ExecDecision::Logged)) == "Logged", "ExecDecision to_string");

  // operator<< for core types is non-empty + carries key fields
  {
    NormalizedEvent ev;
    ev.source = SourceId::Kalshi;
    ev.entity_id = make_entity_id(SourceId::Kalshi, "MKT");
    ev.trace_id = TraceId{42};
    ev.source_sequence = 7;
    ev.payload = BookDelta{.side = Side::No, .price = 4200, .delta = -300};
    const std::string s = str(ev);
    check(has(s, "Kalshi") && has(s, "BookDelta") && has(s, "T#42") && has(s, "seq=7"),
          "NormalizedEvent << carries src/kind/trace/seq");
  }
  {
    OrderIntent oi;
    oi.entity_id = make_entity_id(SourceId::Kalshi, "MKT");
    oi.trace_id = TraceId{9};
    oi.side = Side::Yes;
    oi.limit_price = 5500;
    oi.size = 100;
    oi.post_only = true;
    const std::string s = str(oi);
    check(has(s, "0.5500") && has(s, "1.00") && has(s, "post_only"),
          "OrderIntent << shows price/size/post_only");
  }
  {
    kalshi::ApiError e{kalshi::ApiError::Kind::Kalshi, 401, 0, "authentication_error", "bad"};
    const std::string s = str(e);
    check(has(s, "Kalshi") && has(s, "401") && has(s, "authentication_error"),
          "ApiError << shows kind/status/code");
  }

  // OrderBook dump_state / operator<<
  {
    kalshi::OrderBook b;
    check(has(b.dump_state(), "INVALID"), "fresh book dumps INVALID");
    check(has(str(b), "INVALID"), "operator<< on invalid book prints INVALID");
    b.load_snapshot(kalshi::SnapshotView{{{4000, 500}, {4200, 300}}, {{5500, 100}}, 1});
    const std::string s = b.dump_state();
    check(has(s, "yes_bid=0.4200") && has(s, "seq=1") && !has(s, "INVALID"),
          "valid book dumps best bid + seq");
    // Drive it invalid; must not print stale numbers.
    b.apply_delta(Side::Yes, 4200, -1000, 2);
    check(has(str(b), "INVALID") && !has(str(b), "0.4200"),
          "post-invalid book prints INVALID, not stale prices");
  }

  // dump_state on the test doubles (interface coverage)
  {
    EchoFeatureBuilder fb;
    NullModel m;
    check(has(fb.dump_state(), "EchoFeatureBuilder") && has(m.dump_state(), "NullModel"),
          "FeatureBuilder/Model dump_state present");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
