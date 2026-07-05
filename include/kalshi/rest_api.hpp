#pragma once
//
// Typed Kalshi REST endpoint layer over KalshiClient. Replaces scattered raw
// path strings with typed requests/responses, structured errors, cursor
// pagination, proactive rate limiting, and 429/transient backoff.
//
// Safety: this layer NEVER sends orders. build_order_json() constructs a V2
// order body for inspection/testing only; transmission lives behind the
// execution engines + require_orders_allowed().
//
// Prices are decoded to trading::PriceE4 (probability space, no float) at the
// boundary; Kalshi's dollar-strings / integer-cents both funnel through
// trading::fixedpoint.

#include "kalshi/api_error.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/limits.hpp"
#include "kalshi/request_executor.hpp"
#include "kalshi/telemetry.hpp"
#include "trading/bus.hpp"
#include "trading/fixedpoint.hpp"

#include <chrono>
#include <cstdint>
#include <expected>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

namespace kalshi {

using trading::CountFp;
using trading::Level;
using trading::PriceE4;

// --- typed responses (field-tolerant; absent optionals stay nullopt) ---

struct ExchangeStatus {
  bool exchange_active = false;
  bool trading_active = false;
};

struct MarketSummary {
  std::string ticker;
  std::string status;
  std::optional<PriceE4> yes_bid;
  std::optional<PriceE4> yes_ask;
  std::optional<PriceE4> last_price;
  std::optional<CountFp> volume;
  std::optional<CountFp> open_interest;
};

struct MarketsPage {
  std::vector<MarketSummary> markets;
  std::string next_cursor;  // empty => last page
};

struct OrderbookSnapshot {
  std::string ticker;
  std::vector<Level> yes;  // ascending price
  std::vector<Level> no;
};

struct MarketsQuery {
  std::string cursor;
  int limit = 100;
  std::string status;         // e.g. "open"
  std::string series_ticker;  // optional filter
};

// Typed V2 order spec for build_order_json (construction only, never sent).
struct OrderSpec {
  std::string ticker;
  bool buy = true;               // buy vs sell
  trading::Side side = trading::Side::Yes;
  PriceE4 price = 0;             // probability-space limit price
  CountFp count = 0;
  bool post_only = false;
  bool ioc = false;              // immediate-or-cancel, else GTC
  std::string client_order_id;   // optional; caller-supplied for idempotency
};

struct RetryPolicy {
  int max_attempts = 4;
  long base_ms = 200;      // exponential base
  long max_backoff_ms = 5000;
  long retry_after_cap_ms = 10000;  // clamp a hostile Retry-After
};

class RestApi {
 public:
  // Every call routes through the shared RequestExecutor (T5): reserve -> sign ->
  // send -> retry -> telemetry. Buckets start at a conservative basic tier;
  // configure_limits()/set_cost_table() adopt server values after the T1 fetch.
  explicit RestApi(KalshiClient& client, Runtime rt, RetryPolicy policy = {},
                   TelemetrySink* telemetry = nullptr)
      : rt_(std::move(rt)),
        exec_(client, rt_,
              ExecPolicy{policy.max_attempts, policy.base_ms, policy.max_backoff_ms, 30000},
              telemetry) {}

  // Adopt server-provided rate/capacity (F7: capacity used directly) + costs.
  void configure_limits(const AccountLimits& lim) { exec_.configure_limits(lim); }
  void set_cost_table(const EndpointCostTable& costs) { exec_.set_cost_table(costs); }
  RequestExecutor& executor() { return exec_; }

  std::expected<ExchangeStatus, ApiError> exchange_status();
  std::expected<MarketsPage, ApiError> markets(const MarketsQuery& q);
  std::expected<MarketSummary, ApiError> market(std::string_view ticker);
  std::expected<OrderbookSnapshot, ApiError> orderbook(std::string_view ticker, int depth = 0);
  std::expected<std::string, ApiError> fills(std::string_view cursor = {});     // raw JSON (auth-gated)
  std::expected<std::string, ApiError> positions(std::string_view cursor = {}); // raw JSON (auth-gated)

  // Batch orderbooks: GET /markets/orderbooks?tickers=A&tickers=B (form-explode,
  // 1..100). REST snapshots carry NO seq -> bootstrap / display / cross-check
  // ONLY, never fed to a WS-live book (docs/kalshi_ws_protocol.md I4). The
  // per-request token cost is an open item to measure in demo (I11).
  std::expected<std::vector<OrderbookSnapshot>, ApiError> batch_orderbook(
      const std::vector<std::string>& tickers);

  // Startup self-configuration (PLAN_TOKEN_RULES T1, F3/F5). Fetches the nested
  // /account/limits and /account/endpoint_costs. Mode policy (T1.5):
  //   - live: any fetch/parse failure OR a failed fail-closed invariant returns
  //     an ApiError so the caller fails closed;
  //   - data_collect/shadow: failures fall back conservatively (basic tier /
  //     default costs, from_server=false) with a stderr warning — never errors.
  // On a real pull the tier-table safeguard (F4) logs mismatches (warn-only).
  std::expected<AccountLimits, ApiError> account_limits();
  std::expected<EndpointCostTable, ApiError> endpoint_costs();

  // Typed V2 order construction. Pure string building; no network, no send.
  static std::string build_order_json(const OrderSpec& spec);

  const Runtime& runtime() const { return rt_; }

 private:
  // GET through the executor (reserve -> sign -> send -> retry -> telemetry).
  std::expected<Response, ApiError> get_with_retry(const std::string& path) {
    return exec_.send(Method::Get, path);
  }

  Runtime rt_;
  RequestExecutor exec_;
};

// Kalshi as one trading::DataSource. This pass sources REST orderbook snapshots
// (WS later) and emits source-agnostic BookSnapshot NormalizedEvents. All
// Kalshi-specific decoding stays here; nothing Kalshi-shaped enters the bus.
// Read-only: no order paths are linked or called.
class KalshiDataSource : public trading::DataSource {
 public:
  KalshiDataSource(RestApi& api, std::vector<std::string> tickers,
                   trading::EntityRegistry& reg, int interval_ms = 1000)
      : api_(api), tickers_(std::move(tickers)), reg_(reg), interval_ms_(interval_ms) {}

  trading::SourceId id() const override { return trading::SourceId::Kalshi; }
  void set_sink(trading::MarketDataSink* sink) override { sink_ = sink; }
  void start() override;  // poll loop until stop()
  void stop() override { stop_ = true; }

  // One poll pass: fetch each ticker's orderbook, emit a BookSnapshot event.
  // Returns the number of events emitted. Testable without a poll loop.
  int poll_once();
  std::uint64_t emitted() const { return emitted_; }

 private:
  RestApi& api_;
  std::vector<std::string> tickers_;
  trading::EntityRegistry& reg_;
  int interval_ms_;
  trading::MarketDataSink* sink_ = nullptr;
  bool stop_ = false;
  std::uint64_t emitted_ = 0;
};

}  // namespace kalshi
