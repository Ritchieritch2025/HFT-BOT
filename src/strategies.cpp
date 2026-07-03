#include "kalshi/strategy.hpp"

#include <string>

namespace kalshi {

namespace {

// Both demo strategies deliberately react only to TEST-* tickers so that
// running the full pipeline against the live feed can never place a real
// order until you deploy strategies of your own.

// Slot 0: lifts the ask when it is at or under a threshold.
class ThresholdBuyer final : public IStrategy {
 public:
  ThresholdBuyer(std::uint8_t id, std::string prefix, int max_ask)
      : id_(id), prefix_(std::move(prefix)), max_ask_(max_ask) {}

  std::uint8_t id() const override { return id_; }
  const char* name() const override { return "threshold-buyer"; }

  void on_event(const wire::MarketEvent& ev, ExecSink& sink) override {
    if (ev.kind != wire::kEventTicker) return;
    if (!ev.ticker_view().starts_with(prefix_)) return;
    if (ev.yes_ask <= 0 || ev.yes_ask > max_ask_) return;
    if (ev.yes_ask == last_fired_ask_) return;  // one shot per price level
    last_fired_ask_ = ev.yes_ask;

    wire::ExecPayload p;
    p.action = wire::kActionBuy;
    p.side = wire::kSideYes;
    p.order_type = wire::kTypeLimit;
    p.count = 1;
    p.price_cents = ev.yes_ask;
    p.ttl_ns = 500'000'000;  // edge is stale after 500ms
    p.set_ticker(ev.ticker_view());
    sink.submit(id_, p);
  }

 private:
  const std::uint8_t id_;
  const std::string prefix_;
  const int max_ask_;
  int last_fired_ask_ = -1;
};

// Slot 1: when the book is wide, joins one tick inside the bid.
class SpreadJoiner final : public IStrategy {
 public:
  SpreadJoiner(std::uint8_t id, std::string prefix, int min_spread)
      : id_(id), prefix_(std::move(prefix)), min_spread_(min_spread) {}

  std::uint8_t id() const override { return id_; }
  const char* name() const override { return "spread-joiner"; }

  void on_event(const wire::MarketEvent& ev, ExecSink& sink) override {
    if (ev.kind != wire::kEventTicker) return;
    if (!ev.ticker_view().starts_with(prefix_)) return;
    if (ev.yes_bid <= 0 || ev.yes_ask <= 0) return;
    const int spread = ev.yes_ask - ev.yes_bid;
    if (spread < min_spread_) return;
    const int quote = ev.yes_bid + 1;
    if (quote == last_quote_ || quote >= 99) return;
    last_quote_ = quote;

    wire::ExecPayload p;
    p.action = wire::kActionBuy;
    p.side = wire::kSideYes;
    p.order_type = wire::kTypeLimit;
    p.count = 1;
    p.price_cents = quote;
    p.ttl_ns = 2'000'000'000;
    p.set_ticker(ev.ticker_view());
    sink.submit(id_, p);
  }

 private:
  const std::uint8_t id_;
  const std::string prefix_;
  const int min_spread_;
  int last_quote_ = -1;
};

// Reserved slot awaiting a real strategy.
class IdleStrategy final : public IStrategy {
 public:
  explicit IdleStrategy(std::uint8_t id) : id_(id) {}
  std::uint8_t id() const override { return id_; }
  const char* name() const override { return "idle"; }
  void on_event(const wire::MarketEvent&, ExecSink&) override {}

 private:
  const std::uint8_t id_;
};

}  // namespace

std::vector<std::unique_ptr<IStrategy>> make_strategies() {
  std::vector<std::unique_ptr<IStrategy>> v;
  v.reserve(wire::kStrategySlots);
  v.push_back(std::make_unique<ThresholdBuyer>(0, "TEST-", 45));
  v.push_back(std::make_unique<SpreadJoiner>(1, "TEST-", 10));
  for (int i = static_cast<int>(v.size()); i < wire::kStrategySlots; ++i) {
    v.push_back(std::make_unique<IdleStrategy>(static_cast<std::uint8_t>(i)));
  }
  return v;
}

}  // namespace kalshi
