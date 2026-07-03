// Execution layer daemon.
//
// BRPOPs ExecPayloads from the Redis execution list (exec:orders), validates
// and staleness-gates each one, then submits the order to Kalshi through the
// signed connection pool. Outcomes go to exec:results (capped) and the log.
//
// Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH  (required)
//      KALSHI_BASE_URL   (default https://api.elections.kalshi.com;
//                         point at the mock server in tests)
//      REDIS_HOST/REDIS_PORT (default 127.0.0.1:6379)
//      EXEC_WORKERS      (parallel consumers, default 2)
//      EXEC_POOL         (KalshiClient connections, default 4)
//      EXEC_SKIP_WARMUP  (set to skip the startup warmup round trips)

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/resp.hpp"
#include "kalshi/wire.hpp"

#include <memory>
#include <string>
#include <thread>
#include <vector>

using namespace kalshi;
using daemon::g_stop;
using daemon::logf;
using daemon::now_ns;

namespace {

std::string order_json(const wire::ExecPayload& p) {
  // Fields are pre-validated by decode_exec (ticker charset included), so
  // plain concatenation cannot produce malformed JSON.
  std::string j;
  j.reserve(192);
  j += R"({"ticker":")";
  j += p.ticker_view();
  j += R"(","action":")";
  j += (p.action == wire::kActionBuy) ? "buy" : "sell";
  j += R"(","side":")";
  j += (p.side == wire::kSideYes) ? "yes" : "no";
  j += R"(","count":)";
  j += std::to_string(p.count);
  j += R"(,"type":")";
  j += (p.order_type == wire::kTypeLimit) ? "limit" : "market";
  j += R"(","client_order_id":")";
  j += wire::client_order_id(p);
  j += '"';
  if (p.order_type == wire::kTypeLimit) {
    j += (p.side == wire::kSideYes) ? R"(,"yes_price":)" : R"(,"no_price":)";
    j += std::to_string(p.price_cents);
  }
  j += '}';
  return j;
}

void push_result(resp::RespClient& redis, const std::string& record) {
  // Best-effort monitoring trail; never let it stall execution.
  if (auto r = redis.lpush(wire::kResultList, record); r) {
    if (*r > 20000) (void)redis.ltrim(wire::kResultList, 0, 9999);
  }
}

void worker(int wid, KalshiClient& client, const std::string& redis_host,
            int redis_port) {
  resp::RespClient redis(redis_host, redis_port);
  int attempt = 0;

  while (!g_stop.load(std::memory_order_relaxed)) {
    auto popped = redis.brpop(wire::kExecList, 1);
    if (!popped) {
      logf("execd[%d]: redis error: %s", wid, popped.error().message.c_str());
      daemon::backoff_sleep(attempt++);
      continue;
    }
    attempt = 0;
    if (!*popped) continue;  // idle tick

    const char* why = nullptr;
    const wire::ExecPayload* p = wire::decode_exec(**popped, &why);
    if (!p) {
      logf("execd[%d]: dropped malformed payload (%s, %zu bytes)", wid, why,
           (*popped)->size());
      continue;
    }

    const std::uint64_t t0 = now_ns();
    const long long queue_us = static_cast<long long>(t0 - p->ts_ns) / 1000;
    const std::string cid = wire::client_order_id(*p);

    if (p->ttl_ns != 0 && t0 > p->ts_ns + p->ttl_ns) {
      logf("execd[%d]: EXPIRED strat=%u seq=%llu %.*s (queued %lldus > ttl)",
           wid, p->strategy_id, static_cast<unsigned long long>(p->seq),
           static_cast<int>(p->ticker_view().size()), p->ticker_view().data(),
           queue_us);
      push_result(redis, R"({"cid":")" + cid + R"(","outcome":"expired","queue_us":)" +
                             std::to_string(queue_us) + "}");
      continue;
    }

    const std::string body = order_json(*p);
    auto resp = client.request(Method::Post, "/portfolio/orders", body);
    const long long exec_us = static_cast<long long>(now_ns() - t0) / 1000;

    if (!resp) {
      logf("execd[%d]: TRANSPORT-FAIL strat=%u seq=%llu: %s", wid,
           p->strategy_id, static_cast<unsigned long long>(p->seq),
           resp.error().message.c_str());
      // A timed-out submit may still have reached the exchange. Do NOT
      // requeue blindly — client_order_id makes an intentional retry safe,
      // but that decision belongs to strategy/ops, not the transport.
      push_result(redis, R"({"cid":")" + cid + R"(","outcome":"transport_error","queue_us":)" +
                             std::to_string(queue_us) + "}");
      continue;
    }

    logf("execd[%d]: %s -> HTTP %ld strat=%u seq=%llu queue=%lldus exec=%lldus",
         wid, body.c_str(), resp->status, p->strategy_id,
         static_cast<unsigned long long>(p->seq), queue_us, exec_us);
    push_result(redis, R"({"cid":")" + cid + R"(","outcome":"submitted","http":)" +
                           std::to_string(resp->status) +
                           R"(,"queue_us":)" + std::to_string(queue_us) +
                           R"(,"exec_us":)" + std::to_string(exec_us) + "}");
  }
}

}  // namespace

int main() {
  daemon::install_signal_handlers();

  const std::string key_id = daemon::env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = daemon::env_or("KALSHI_PRIVATE_KEY_PATH", "");
  if (key_id.empty() || key_path.empty()) {
    logf("execd: set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH");
    return 2;
  }

  Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = daemon::env_or("KALSHI_BASE_URL", "https://api.elections.kalshi.com");
  cfg.pool_size = daemon::env_int("EXEC_POOL", 4);
  KalshiClient client(std::move(cfg));

  if (!std::getenv("EXEC_SKIP_WARMUP")) {
    logf("execd: warmed %d/%d connections", client.warmup(),
         client.config().pool_size);
  }

  const std::string redis_host = daemon::env_or("REDIS_HOST", "127.0.0.1");
  const int redis_port = daemon::env_int("REDIS_PORT", 6379);
  const int n_workers = daemon::env_int("EXEC_WORKERS", 2);

  logf("execd: consuming %s (%d workers) -> %s", wire::kExecList, n_workers,
       client.config().base_url.c_str());

  std::vector<std::thread> workers;
  workers.reserve(static_cast<size_t>(n_workers));
  for (int w = 0; w < n_workers; ++w)
    workers.emplace_back(worker, w, std::ref(client), redis_host, redis_port);
  for (auto& t : workers) t.join();

  logf("execd: stopped");
  return 0;
}
