// tradingd — the single-process trading engine.
//
// Hot thread:      feed (REST poll | WS later) → MarketEvent →
//                  configured strategies inline → InlineExecutor stamps/validates the
//                  80-byte ExecPayload and pushes it onto a lock-free ring
//                  (~100ns). No Redis, no process hop, no blocking on I/O.
// Submit workers:  each owns a dedicated KalshiClient::Lane — an exclusive
//                  warm HTTP/2 TLS connection (no shared socket, no pool
//                  mutex; workers cannot block each other). pop → staleness/
//                  rate gates → order_json → RSA-PSS sign → lane.send().
//                  Idle workers ping their own lane so a real order never
//                  pays a TLS handshake.
// Telemetry:       cold path only. Workers push fixed-size records onto a
//                  second ring; one thread drains it to Redis exec:results
//                  (drops if Redis is down — trading is never affected).
//
// Usage: tradingd --poll [interval_ms]
// Env:   KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH   (required)
//        KALSHI_BASE_URL                              (default production)
//        REDIS_HOST/REDIS_PORT                        (telemetry, optional)
//        TRADINGD_WORKERS=2      submit lanes (one dedicated connection each)
//        TRADINGD_RING=1024      order ring capacity (power of two)
//        TRADINGD_SPIN=0         1 = busy-poll workers (lowest handoff latency)
//        TRADINGD_MAX_QUEUE_AGE_MS=2000  hard staleness cap (closes ttl=0 hole)
//        TRADINGD_MAX_ORDERS_PER_SEC=0   token bucket, 0 = off
//        TRADINGD_KEEPALIVE_S=15 per-lane idle ping interval
//        TRADINGD_DRAIN_MS=2000  shutdown drain deadline for queued orders

#include "daemon_util.hpp"
#include "feed.hpp"
#include "kalshi/client.hpp"
#include "kalshi/full_chain_latency_probe.hpp"
#include "kalshi/resp.hpp"
#include "kalshi/ring.hpp"
#include "kalshi/strategy.hpp"
#include "kalshi/wire.hpp"

#include <algorithm>
#include <atomic>
#include <cinttypes>
#include <cstring>
#include <condition_variable>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using namespace kalshi;
using daemon::g_stop;
using daemon::logf;
using daemon::steady_now_ns;

namespace {

// ---------------------------------------------------------------- messages

struct OrderMsg {
  wire::ExecPayload intent{};
  std::uint64_t decision_steady_ns = 0;  // stamped at submit() entry
  std::uint64_t enqueue_steady_ns = 0;   // stamped just before try_push
  latency_probe::Trace trace{};          // full-chain marks (steady clock)
};

void set_mark(latency_probe::Trace& tr, latency_probe::Event e, std::uint64_t ns) {
  tr.t[static_cast<std::size_t>(e)] = ns;
}

struct TelemetryRecord {
  enum class Kind : std::uint8_t {
    Submitted, Expired, RateLimited, TransportError, SignError,
    DroppedFull, RejectedInvalid,
  };
  Kind kind = Kind::Submitted;
  std::uint8_t strategy_id = 0;
  std::uint8_t side = 0;      // wire::kSideYes/kSideNo
  std::uint8_t action = 0;    // wire::kActionBuy/kActionSell
  std::int32_t price_cents = 0;
  std::int32_t count = 0;
  std::uint16_t http_status = 0;
  std::uint32_t sign_us = 0, queue_us = 0, send_us = 0;
  std::uint64_t seq = 0, ts_ns = 0;
  char ticker[42] = {};
};

const char* kind_name(TelemetryRecord::Kind k) {
  switch (k) {
    case TelemetryRecord::Kind::Submitted: return "submitted";
    case TelemetryRecord::Kind::Expired: return "expired";
    case TelemetryRecord::Kind::RateLimited: return "rate_limited";
    case TelemetryRecord::Kind::TransportError: return "transport_error";
    case TelemetryRecord::Kind::SignError: return "sign_error";
    case TelemetryRecord::Kind::DroppedFull: return "dropped_full";
    case TelemetryRecord::Kind::RejectedInvalid: return "rejected_invalid";
  }
  return "unknown";
}

// ---------------------------------------------------------------- doorbell

// Spin-then-park wakeup between the hot thread and the submit workers.
// ring() costs one relaxed-ish atomic load when nobody is parked. The 500us
// wait bound is the hard cap on any residual wakeup race.
class Doorbell {
 public:
  void ring() noexcept {
    if (parked_.load(std::memory_order_seq_cst) > 0) {
      std::lock_guard lk(m_);
      cv_.notify_all();
    }
  }

  template <typename HasWork>
  void wait(HasWork&& has_work) {
    parked_.fetch_add(1, std::memory_order_seq_cst);
    {
      std::unique_lock lk(m_);
      if (!has_work())
        cv_.wait_for(lk, std::chrono::microseconds(500));
    }
    parked_.fetch_sub(1, std::memory_order_seq_cst);
  }

 private:
  std::atomic<int> parked_{0};
  std::mutex m_;
  std::condition_variable cv_;
};

// ------------------------------------------------------------ token bucket

class TokenBucket {
 public:
  explicit TokenBucket(double per_sec)
      : rate_(per_sec), tokens_(per_sec), last_(steady_now_ns()) {}

  bool try_take() {
    if (rate_ <= 0) return true;  // disabled
    std::lock_guard lk(m_);
    const std::uint64_t now = steady_now_ns();
    tokens_ = std::min(rate_, tokens_ + rate_ * 1e-9 *
                                  static_cast<double>(now - last_));
    last_ = now;
    if (tokens_ >= 1.0) {
      tokens_ -= 1.0;
      return true;
    }
    return false;
  }

 private:
  const double rate_;
  double tokens_;
  std::uint64_t last_;
  std::mutex m_;
};

// ---------------------------------------------------------------- engine

struct Engine {
  Ring<OrderMsg> orders;
  Ring<TelemetryRecord> telemetry;
  Ring<latency_probe::Trace> probe{4096};  // completed traces -> CSV, cold path
  Doorbell bell;
  TokenBucket bucket;
  KalshiClient& client;
  std::uint64_t max_queue_age_ns;
  bool spin;
  bool probe_enabled = false;  // set when TRADINGD_LATENCY_CSV names a file

  std::atomic<std::uint64_t> drain_deadline_steady{0};  // set at shutdown
  std::atomic<std::uint64_t> submitted{0}, expired{0}, rate_limited{0},
      transport_err{0}, sign_err{0}, dropped_full{0}, rejected{0},
      telemetry_dropped{0}, shutdown_dropped{0};

  Engine(std::size_t ring_cap, double orders_per_sec, KalshiClient& c,
         std::uint64_t max_age_ns, bool spin_mode)
      : orders(ring_cap), telemetry(8192), bucket(orders_per_sec), client(c),
        max_queue_age_ns(max_age_ns), spin(spin_mode) {}

  void emit(TelemetryRecord&& rec) {
    if (!telemetry.try_push(std::move(rec)))
      telemetry_dropped.fetch_add(1, std::memory_order_relaxed);
  }
};

TelemetryRecord make_record(TelemetryRecord::Kind kind,
                            const wire::ExecPayload& p, std::uint32_t queue_us) {
  TelemetryRecord r;
  r.kind = kind;
  r.strategy_id = p.strategy_id;
  r.side = p.side;
  r.action = p.action;
  r.price_cents = p.price_cents;
  r.count = p.count;
  r.seq = p.seq;
  r.ts_ns = p.ts_ns;
  r.queue_us = queue_us;
  std::memcpy(r.ticker, p.ticker, sizeof(r.ticker));
  return r;
}

// The ExecSink strategies fire into: stamp, validate, enqueue. Hot thread
// only — the per-slot seq counters are plain integers by design.
class InlineExecutor final : public ExecSink {
 public:
  explicit InlineExecutor(Engine& e) : e_(e) {}

  // Called by the dispatch loop before the strategies see each event.
  void begin_event(const feed::EventTiming& timing) {
    event_timing_ = timing;
    dispatch_start_ns_ = steady_now_ns();
  }

  void submit(std::uint8_t strategy_id, wire::ExecPayload p) override {
    const std::uint64_t t_decision = steady_now_ns();
    p.magic = wire::kExecMagic;
    p.version = wire::kWireVersion;
    p.strategy_id = strategy_id;
    p.seq = ++seq_[strategy_id];
    p.ts_ns = daemon::now_ns();

    const char* why = nullptr;
    if (!wire::decode_exec(wire::as_bytes(p), &why)) {
      logf("tradingd: strategy %u produced invalid payload (%s) — rejected",
           strategy_id, why);
      e_.rejected.fetch_add(1, std::memory_order_relaxed);
      e_.emit(make_record(TelemetryRecord::Kind::RejectedInvalid, p, 0));
      return;
    }

    OrderMsg m{p, t_decision, steady_now_ns()};
    if (e_.probe_enabled) {
      using latency_probe::Event;
      m.trace.reset((static_cast<std::uint64_t>(strategy_id) << 56) | p.seq);
      set_mark(m.trace, Event::DataReceived, event_timing_.received_steady_ns);
      set_mark(m.trace, Event::DataParsed, event_timing_.parsed_steady_ns);
      set_mark(m.trace, Event::DecisionStart, dispatch_start_ns_);
      set_mark(m.trace, Event::DecisionDone, t_decision);
      set_mark(m.trace, Event::RiskDone, m.enqueue_steady_ns);  // decode_exec passed
    }
    if (!e_.orders.try_push(std::move(m))) {
      // Full ring means the submit path is catastrophically behind; these
      // intents are already dead. Drop loudly, never block market data.
      logf("tradingd: order ring FULL — dropped strat=%u seq=%" PRIu64,
           strategy_id, p.seq);
      e_.dropped_full.fetch_add(1, std::memory_order_relaxed);
      e_.emit(make_record(TelemetryRecord::Kind::DroppedFull, p, 0));
      return;
    }
    e_.bell.ring();
  }

 private:
  Engine& e_;
  std::array<std::uint64_t, wire::kStrategySlots> seq_{};
  feed::EventTiming event_timing_{};
  std::uint64_t dispatch_start_ns_ = 0;
};

// ------------------------------------------------------------- submit lane

void process_order(Engine& e, KalshiClient::Lane& lane, OrderMsg& m) {
  const std::uint64_t t_pop = steady_now_ns();
  const auto queue_us =
      static_cast<std::uint32_t>((t_pop - m.enqueue_steady_ns) / 1000);
  const std::uint64_t age_ns = t_pop - m.decision_steady_ns;

  if ((m.intent.ttl_ns != 0 && age_ns > m.intent.ttl_ns) ||
      age_ns > e.max_queue_age_ns) {
    e.expired.fetch_add(1, std::memory_order_relaxed);
    e.emit(make_record(TelemetryRecord::Kind::Expired, m.intent, queue_us));
    return;
  }
  if (!e.bucket.try_take()) {
    e.rate_limited.fetch_add(1, std::memory_order_relaxed);
    e.emit(make_record(TelemetryRecord::Kind::RateLimited, m.intent, queue_us));
    return;
  }

  using latency_probe::Event;
  const std::uint64_t t_sign = steady_now_ns();
  set_mark(m.trace, Event::SignStart, t_sign);
  auto req = e.client.sign_request(Method::Post, wire::kCreateOrderPath,
                                   wire::order_json(m.intent));
  if (!req) {
    logf("tradingd: sign failed: %s", req.error().message.c_str());
    e.sign_err.fetch_add(1, std::memory_order_relaxed);
    e.emit(make_record(TelemetryRecord::Kind::SignError, m.intent, queue_us));
    return;
  }
  const std::uint64_t t_send = steady_now_ns();
  set_mark(m.trace, Event::SignDone, t_send);
  set_mark(m.trace, Event::WriteStart, t_send);
  auto resp = lane.send(*req);
  const std::uint64_t t_done = steady_now_ns();
  if (e.probe_enabled) {
    set_mark(m.trace, Event::ResponseDone, t_done);
    if (resp) {
      // curl offsets from transfer start: PRETRANSFER ~ request about to be
      // written (WriteDone approximation on a warm keep-alive connection),
      // STARTTRANSFER = first response byte.
      if (resp->pretransfer_us > 0)
        set_mark(m.trace, Event::WriteDone,
                 t_send + static_cast<std::uint64_t>(resp->pretransfer_us) * 1000);
      if (resp->starttransfer_us > 0)
        set_mark(m.trace, Event::ResponseFirstByte,
                 t_send + static_cast<std::uint64_t>(resp->starttransfer_us) * 1000);
      if (resp->ok()) set_mark(m.trace, Event::OrderAccepted, steady_now_ns());
    }
    if (!e.probe.try_push(std::move(m.trace)))
      e.telemetry_dropped.fetch_add(1, std::memory_order_relaxed);
  }

  TelemetryRecord rec = make_record(TelemetryRecord::Kind::Submitted, m.intent,
                                    queue_us);
  rec.sign_us = static_cast<std::uint32_t>((t_send - t_sign) / 1000);
  rec.send_us = static_cast<std::uint32_t>((t_done - t_send) / 1000);

  if (!resp) {
    // A timed-out submit may still have reached the exchange. No blind
    // retry — the deterministic client_order_id makes an intentional
    // ops/strategy retry idempotent.
    logf("tradingd: TRANSPORT-FAIL strat=%u seq=%" PRIu64 ": %s",
         m.intent.strategy_id, m.intent.seq, resp.error().message.c_str());
    rec.kind = TelemetryRecord::Kind::TransportError;
    e.transport_err.fetch_add(1, std::memory_order_relaxed);
  } else {
    rec.http_status = static_cast<std::uint16_t>(resp->status);
    e.submitted.fetch_add(1, std::memory_order_relaxed);
    logf("tradingd: order strat=%u seq=%" PRIu64 " %.*s -> HTTP %ld "
         "(queue=%uus sign=%uus send=%uus)",
         m.intent.strategy_id, m.intent.seq,
         static_cast<int>(m.intent.ticker_view().size()),
         m.intent.ticker_view().data(), resp->status, queue_us, rec.sign_us,
         rec.send_us);
  }
  e.emit(std::move(rec));
}

void submit_worker(Engine& e, int wid, int keepalive_s) {
  KalshiClient::Lane lane = e.client.make_lane();
  {
    auto warm = lane.ping();
    logf("tradingd: lane %d %s", wid,
         (warm && warm->ok()) ? "warm (dedicated connection up)"
                              : "warmup failed (will connect on first use)");
  }
  // Stagger keepalives so lanes never ping in lockstep.
  const std::uint64_t keepalive_ns =
      static_cast<std::uint64_t>(keepalive_s) * 1'000'000'000ULL +
      static_cast<std::uint64_t>(wid) * 2'000'000'000ULL;
  std::uint64_t last_activity = steady_now_ns();

  OrderMsg m;
  for (;;) {
    if (e.orders.try_pop(m)) {
      process_order(e, lane, m);
      last_activity = steady_now_ns();
      continue;
    }
    if (g_stop.load(std::memory_order_relaxed)) {
      // Drain phase: ring empty (checked above) or deadline passed => done.
      const std::uint64_t deadline =
          e.drain_deadline_steady.load(std::memory_order_relaxed);
      if (deadline == 0 || steady_now_ns() > deadline) break;
    }
    if (e.spin) {
      std::this_thread::yield();
    } else {
      e.bell.wait([&] { return e.orders.size_approx() > 0 ||
                               g_stop.load(std::memory_order_relaxed); });
    }
    if (steady_now_ns() - last_activity > keepalive_ns &&
        !g_stop.load(std::memory_order_relaxed)) {
      (void)lane.ping();  // keep this lane's TLS session hot through idle
      last_activity = steady_now_ns();
    }
  }
  logf("tradingd: lane %d stopped", wid);
}

// --------------------------------------------------------------- telemetry

// NDJSON line for the ops dashboard (dashboard_server.py). Cold path only.
std::string ndjson_order(const TelemetryRecord& r, const char* mode) {
  wire::ExecPayload id{};
  id.strategy_id = r.strategy_id;
  id.seq = r.seq;
  id.ts_ns = r.ts_ns;
  const char* status = "sent";
  const char* reason = "";
  switch (r.kind) {
    case TelemetryRecord::Kind::Submitted:
      status = (r.http_status >= 400) ? "rejected" : "sent"; break;
    case TelemetryRecord::Kind::Expired: status = "dropped"; reason = "expired"; break;
    case TelemetryRecord::Kind::RateLimited: status = "dropped"; reason = "rate_limited"; break;
    case TelemetryRecord::Kind::TransportError: status = "error"; reason = "transport_error"; break;
    case TelemetryRecord::Kind::SignError: status = "error"; reason = "sign_error"; break;
    case TelemetryRecord::Kind::DroppedFull: status = "dropped"; reason = "queue_full"; break;
    case TelemetryRecord::Kind::RejectedInvalid: status = "rejected"; reason = "invalid"; break;
  }
  const char* side =
      (r.action == wire::kActionBuy)
          ? (r.side == wire::kSideYes ? "buy_yes" : "buy_no")
          : (r.side == wire::kSideYes ? "sell_yes" : "sell_no");
  char buf[512];
  std::snprintf(
      buf, sizeof(buf),
      "{\"type\":\"order\",\"ts_ms\":%llu,\"strategy\":\"slot_%u\",\"ticker\":\"%.*s\","
      "\"side\":\"%s\",\"price\":%d,\"size\":%d,\"mode\":\"%s\",\"status\":\"%s\","
      "\"http_status\":%u,\"sign_us\":%u,\"submit_to_ack_ms\":%.3f,"
      "\"signal_to_ack_ms\":%.3f,\"reason\":\"%s\"}",
      static_cast<unsigned long long>(r.ts_ns / 1'000'000ULL), r.strategy_id,
      static_cast<int>(::strnlen(r.ticker, sizeof(r.ticker))), r.ticker, side,
      r.price_cents, r.count, mode, status, r.http_status, r.sign_us,
      r.send_us / 1000.0, (r.queue_us + r.sign_us + r.send_us) / 1000.0, reason);
  return buf;
}

std::string ndjson_system(const char* mode, const char* status, const char* msg,
                          const char* env) {
  char buf[320];
  std::snprintf(buf, sizeof(buf),
                "{\"type\":\"system\",\"ts_ms\":%llu,\"mode\":\"%s\",\"component\":"
                "\"tradingd\",\"status\":\"%s\",\"message\":\"%s\",\"env\":\"%s\"}",
                static_cast<unsigned long long>(daemon::now_ns() / 1'000'000ULL),
                mode, status, msg, env);
  return buf;
}

std::string telemetry_json(const TelemetryRecord& r) {
  wire::ExecPayload id{};
  id.strategy_id = r.strategy_id;
  id.seq = r.seq;
  id.ts_ns = r.ts_ns;
  std::string j;
  j.reserve(224);
  j += R"({"kind":")";
  j += kind_name(r.kind);
  j += R"(","cid":")";
  j += wire::client_order_id(id);
  j += R"(","strategy":)";
  j += std::to_string(r.strategy_id);
  j += R"(,"seq":)";
  j += std::to_string(r.seq);
  j += R"(,"ticker":")";
  j.append(r.ticker, ::strnlen(r.ticker, sizeof(r.ticker)));
  j += R"(","http":)";
  j += std::to_string(r.http_status);
  j += R"(,"queue_us":)";
  j += std::to_string(r.queue_us);
  j += R"(,"sign_us":)";
  j += std::to_string(r.sign_us);
  j += R"(,"send_us":)";
  j += std::to_string(r.send_us);
  j += '}';
  return j;
}

void telemetry_worker(Engine& e, std::atomic<bool>& stop, const char* mode,
                      const char* env) {
  resp::RespClient redis(daemon::env_or("REDIS_HOST", "127.0.0.1"),
                         daemon::env_int("REDIS_PORT", 6379));
  const std::string csv_path = daemon::env_or("TRADINGD_LATENCY_CSV", "");
  std::FILE* csv = nullptr;
  if (!csv_path.empty()) {
    csv = std::fopen(csv_path.c_str(), "w");
    if (csv) latency_probe::write_csv_header(csv);
    else logf("tradingd: cannot open %s for latency CSV", csv_path.c_str());
  }

  // NDJSON sink for the ops dashboard (append-only; the dashboard tails it).
  const std::string ndjson_path = daemon::env_or("TRADINGD_NDJSON", "");
  std::FILE* nd = nullptr;
  if (!ndjson_path.empty()) {
    nd = std::fopen(ndjson_path.c_str(), "a");
    if (nd) {
      std::fprintf(nd, "%s\n", ndjson_system(mode, "started", "engine up", env).c_str());
      std::fflush(nd);
    } else {
      logf("tradingd: cannot open %s for NDJSON", ndjson_path.c_str());
    }
  }
  std::uint64_t last_heartbeat_ns = daemon::now_ns();

  TelemetryRecord rec;
  latency_probe::Trace trace;
  std::uint64_t pushed = 0;
  for (;;) {
    bool worked = false;
    if (e.telemetry.try_pop(rec)) {
      worked = true;
      if (auto r = redis.lpush(wire::kResultList, telemetry_json(rec)); !r) {
        e.telemetry_dropped.fetch_add(1, std::memory_order_relaxed);
        redis.close();  // reconnect on next record; never block trading
      } else if (++pushed % 4096 == 0 && *r > 20000) {
        (void)redis.ltrim(wire::kResultList, 0, 9999);
      }
      if (nd) {
        std::fprintf(nd, "%s\n", ndjson_order(rec, mode).c_str());
        std::fflush(nd);
      }
    }
    if (csv && e.probe.try_pop(trace)) {
      worked = true;
      latency_probe::write_csv_row(csv, trace);
      std::fflush(csv);
    }
    if (nd && daemon::now_ns() - last_heartbeat_ns > 2'000'000'000ULL) {
      std::fprintf(nd, "%s\n", ndjson_system(mode, "ok", "heartbeat", env).c_str());
      std::fflush(nd);
      last_heartbeat_ns = daemon::now_ns();
    }
    if (worked) continue;
    if (stop.load(std::memory_order_relaxed)) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  }
  if (nd) {
    std::fprintf(nd, "%s\n", ndjson_system(mode, "stopped", "engine down", env).c_str());
    std::fclose(nd);
  }
  if (csv) std::fclose(csv);
}

}  // namespace

int main(int argc, char** argv) {
  daemon::install_signal_handlers();

  const std::string key_id = daemon::env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = daemon::env_or("KALSHI_PRIVATE_KEY_PATH", "");
  if (key_id.empty() || key_path.empty()) {
    logf("tradingd: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH");
    return 2;
  }

  Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = daemon::env_or("KALSHI_BASE_URL", "https://api.elections.kalshi.com");
  cfg.pool_size = 1;  // order traffic runs on dedicated lanes, not the pool
  KalshiClient client(std::move(cfg));

  const int n_workers = std::max(1, daemon::env_int("TRADINGD_WORKERS", 2));
  const auto ring_cap =
      static_cast<std::size_t>(daemon::env_int("TRADINGD_RING", 1024));
  const double max_rate = std::atof(
      daemon::env_or("TRADINGD_MAX_ORDERS_PER_SEC", "0").c_str());
  const auto max_age_ns = static_cast<std::uint64_t>(daemon::env_int(
                              "TRADINGD_MAX_QUEUE_AGE_MS", 2000)) * 1'000'000ULL;
  const bool spin = daemon::env_int("TRADINGD_SPIN", 0) != 0;
  const int keepalive_s = daemon::env_int("TRADINGD_KEEPALIVE_S", 15);

  Engine engine(ring_cap, max_rate, client, max_age_ns, spin);
  engine.probe_enabled = std::getenv("TRADINGD_LATENCY_CSV") != nullptr;
  InlineExecutor executor(engine);
  auto strategies = make_strategies();
  logf("tradingd: %zu configured strategies, %d lanes, ring=%zu, %s wakeup",
       strategies.size(), n_workers, ring_cap, spin ? "spin" : "park");

  std::vector<std::thread> workers;
  workers.reserve(static_cast<size_t>(n_workers));
  for (int w = 0; w < n_workers; ++w)
    workers.emplace_back(submit_worker, std::ref(engine), w, keepalive_s);

  // Mode label for telemetry/dashboard; env is prod unless base_url says demo.
  static const std::string mode_s = daemon::env_or("TRADINGD_MODE", "live");
  static const std::string env_s =
      client.config().base_url.find("demo") != std::string::npos ? "demo" : "prod";
  std::atomic<bool> telemetry_stop{false};
  std::thread telemetry(telemetry_worker, std::ref(engine),
                        std::ref(telemetry_stop), mode_s.c_str(), env_s.c_str());

  // Per-slot quarantine: one bad strategy must not take down execution.
  std::array<int, wire::kStrategySlots> consecutive_throws{};
  const feed::EventHandler dispatch = [&](wire::MarketEvent& ev,
                                          const feed::EventTiming& timing) {
    executor.begin_event(timing);
    for (size_t i = 0; i < strategies.size(); ++i) {
      if (consecutive_throws[i] >= 3) continue;  // quarantined
      try {
        strategies[i]->on_event(ev, executor);
        consecutive_throws[i] = 0;
      } catch (const std::exception& ex) {
        if (++consecutive_throws[i] == 3)
          logf("tradingd: strategy slot %zu (%s) QUARANTINED: %s", i,
               strategies[i]->name(), ex.what());
      } catch (...) {
        if (++consecutive_throws[i] == 3)
          logf("tradingd: strategy slot %zu (%s) QUARANTINED", i,
               strategies[i]->name());
      }
    }
  };

  const std::string mode = argc > 1 ? argv[1] : "";
  int rc = 0;
  if (mode == "--poll") {
    const int interval = argc > 2 ? std::atoi(argv[2]) : 1000;
    Config feed_cfg = client.config();  // same creds/base_url
    feed_cfg.pool_size = 1;  // dedicated market-data connection
    KalshiClient feed_client(std::move(feed_cfg));
    rc = feed::run_poll(feed_client, interval, dispatch);
  } else {
    std::fprintf(stderr, "usage: tradingd --poll [interval_ms]\n");
    rc = 2;
  }

  // Shutdown: drain queued orders (gates still apply) up to the deadline.
  const auto drain_ms =
      static_cast<std::uint64_t>(daemon::env_int("TRADINGD_DRAIN_MS", 2000));
  engine.drain_deadline_steady.store(steady_now_ns() + drain_ms * 1'000'000ULL);
  g_stop.store(true);
  engine.bell.ring();
  for (auto& t : workers) t.join();
  engine.shutdown_dropped.store(engine.orders.size_approx());

  // Telemetry: best-effort final drain.
  std::this_thread::sleep_for(std::chrono::milliseconds(50));
  telemetry_stop.store(true);
  telemetry.join();

  logf("tradingd: stopped. submitted=%" PRIu64 " expired=%" PRIu64
       " rate_limited=%" PRIu64 " transport_err=%" PRIu64 " sign_err=%" PRIu64
       " rejected=%" PRIu64 " dropped_full=%" PRIu64 " undrained=%" PRIu64
       " telemetry_dropped=%" PRIu64,
       engine.submitted.load(), engine.expired.load(),
       engine.rate_limited.load(), engine.transport_err.load(),
       engine.sign_err.load(), engine.rejected.load(),
       engine.dropped_full.load(), engine.shutdown_dropped.load(),
       engine.telemetry_dropped.load());
  return rc;
}
