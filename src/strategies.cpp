#include "kalshi/strategy.hpp"

#include <cstdlib>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>

namespace kalshi {

namespace {

// once_probe (PLAN_LIVE_VALIDATION P3): a one-shot test strategy for exercising
// the full feed -> strategy -> ring -> submit -> ack latency chain. On the FIRST
// MarketEvent matching PROBE_TICKER it emits a single 1c YES bid, then stays
// silent forever. It is NOT in the default roster: tradingd generates no orders
// unless STRATEGIES explicitly names it. Params are read from env at
// construction ONLY (never touched in on_event -> no hot-path I/O):
//   PROBE_TICKER       (required; absent => make_strategies fails closed)
//   PROBE_PRICE_CENTS  (default 1)
//   PROBE_COUNT        (default 1)
class OnceProbe : public IStrategy {
 public:
  OnceProbe(std::string ticker, std::int32_t price_cents, std::int32_t count)
      : ticker_(std::move(ticker)), price_cents_(price_cents), count_(count) {}

  std::uint8_t id() const override { return 29; }
  const char* name() const override { return "once_probe"; }

  void on_event(const wire::MarketEvent& event, ExecSink& sink) override {
    if (fired_) return;                          // silent after the single shot
    if (event.ticker_view() != ticker_) return;  // only our probe market
    fired_ = true;
    wire::ExecPayload p;
    p.action = wire::kActionBuy;
    p.side = wire::kSideYes;
    p.order_type = wire::kTypeLimit;
    p.count = count_;
    p.price_cents = price_cents_;
    p.set_ticker(ticker_);
    sink.submit(id(), p);  // strategy_id/seq/ts_ns stamped by the sink
  }

 private:
  std::string ticker_;
  std::int32_t price_cents_;
  std::int32_t count_;
  bool fired_ = false;  // single dispatch thread => a plain bool is safe
};

std::int32_t env_int(const char* name, std::int32_t dflt) {
  const char* v = std::getenv(name);
  if (!v || !*v) return dflt;
  char* end = nullptr;
  long n = std::strtol(v, &end, 10);
  if (end == v || *end != '\0') return dflt;
  return static_cast<std::int32_t>(n);
}

}  // namespace

// Roster selection (P3): STRATEGIES names the enabled strategies (comma-separated).
// Unset/empty => empty roster (production default: tradingd generates no orders).
// An unknown name or a missing required param fails closed (throws) so a
// misconfigured launch never silently runs the wrong roster.
std::vector<std::unique_ptr<IStrategy>> make_strategies() {
  std::vector<std::unique_ptr<IStrategy>> out;
  const char* roster = std::getenv("STRATEGIES");
  if (!roster || !*roster) return out;

  std::string_view rv(roster);
  std::size_t start = 0;
  while (start <= rv.size()) {
    std::size_t comma = rv.find(',', start);
    std::string_view name =
        rv.substr(start, comma == std::string_view::npos ? std::string_view::npos
                                                         : comma - start);
    if (!name.empty()) {
      if (name == "once_probe") {
        const char* t = std::getenv("PROBE_TICKER");
        if (!t || !*t)
          throw std::runtime_error("once_probe requires PROBE_TICKER (fail closed)");
        out.push_back(std::make_unique<OnceProbe>(
            std::string(t), env_int("PROBE_PRICE_CENTS", 1), env_int("PROBE_COUNT", 1)));
      } else {
        throw std::runtime_error("unknown strategy in STRATEGIES: " + std::string(name));
      }
    }
    if (comma == std::string_view::npos) break;
    start = comma + 1;
  }
  return out;
}

}  // namespace kalshi
