// Processing layer daemon.
//
// Subscribes to the market-event channel, decodes each MarketEvent, and
// dispatches it to all 30 strategy slots on one hot thread. Strategies emit
// ExecPayloads through the sink, which stamps identity (seq, ts_ns) and
// LPUSHes them onto the execution list.
//
// Env: REDIS_HOST/REDIS_PORT (default 127.0.0.1:6379)

#include "daemon_util.hpp"
#include "kalshi/resp.hpp"
#include "kalshi/strategy.hpp"
#include "kalshi/wire.hpp"

#include <array>
#include <cinttypes>

using namespace kalshi;
using daemon::g_stop;
using daemon::logf;

namespace {

class RedisSink final : public ExecSink {
 public:
  RedisSink(const std::string& host, int port) : redis_(host, port) {}

  void submit(std::uint8_t strategy_id, wire::ExecPayload p) override {
    p.magic = wire::kExecMagic;
    p.version = wire::kWireVersion;
    p.strategy_id = strategy_id;
    p.seq = ++seq_[strategy_id];
    p.ts_ns = daemon::now_ns();

    const char* why = nullptr;
    if (!wire::decode_exec(wire::as_bytes(p), &why)) {  // reject at the source
      logf("stratd: strategy %u produced invalid payload (%s) — dropped",
           strategy_id, why);
      ++dropped_;
      return;
    }
    if (auto r = redis_.lpush(wire::kExecList, wire::as_bytes(p)); !r) {
      // The edge is perishable; drop rather than buffer stale intents.
      logf("stratd: lpush failed (%s) — order strat=%u seq=%" PRIu64 " dropped",
           r.error().message.c_str(), strategy_id, p.seq);
      ++dropped_;
      redis_.close();  // next lpush reconnects
      return;
    }
    ++submitted_;
  }

  std::uint64_t submitted() const { return submitted_; }
  std::uint64_t dropped() const { return dropped_; }

 private:
  resp::RespClient redis_;
  std::array<std::uint64_t, wire::kStrategySlots> seq_{};
  std::uint64_t submitted_ = 0;
  std::uint64_t dropped_ = 0;
};

}  // namespace

int main() {
  daemon::install_signal_handlers();
  const std::string redis_host = daemon::env_or("REDIS_HOST", "127.0.0.1");
  const int redis_port = daemon::env_int("REDIS_PORT", 6379);

  auto strategies = make_strategies();
  logf("stratd: %zu strategy slots (slot0=%s slot1=%s)", strategies.size(),
       strategies[0]->name(), strategies[1]->name());

  RedisSink sink(redis_host, redis_port);
  resp::RespClient sub(redis_host, redis_port);

  std::uint64_t events = 0;
  std::uint64_t last_report = daemon::now_ns();
  int attempt = 0;
  bool subscribed = false;

  while (!g_stop.load(std::memory_order_relaxed)) {
    if (!subscribed) {
      if (auto s = sub.subscribe(wire::kEventChannel); !s) {
        logf("stratd: subscribe failed: %s", s.error().message.c_str());
        sub.close();
        daemon::backoff_sleep(attempt++);
        continue;
      }
      logf("stratd: subscribed to %s", wire::kEventChannel);
      subscribed = true;
      attempt = 0;
    }

    auto msg = sub.next_message(200);
    if (!msg) {
      logf("stratd: subscription lost: %s", msg.error().message.c_str());
      sub.close();
      subscribed = false;
      continue;
    }
    if (*msg) {
      const wire::MarketEvent* ev = wire::decode_event(**msg);
      if (!ev) {
        logf("stratd: dropped malformed event (%zu bytes)", (*msg)->size());
      } else {
        ++events;
        for (auto& s : strategies) s->on_event(*ev, sink);
      }
    }

    const std::uint64_t now = daemon::now_ns();
    if (now - last_report > 10'000'000'000ULL) {
      logf("stratd: events=%" PRIu64 " orders=%" PRIu64 " dropped=%" PRIu64,
           events, sink.submitted(), sink.dropped());
      last_report = now;
    }
  }

  logf("stratd: stopped (events=%" PRIu64 " orders=%" PRIu64 ")", events,
       sink.submitted());
  return 0;
}
