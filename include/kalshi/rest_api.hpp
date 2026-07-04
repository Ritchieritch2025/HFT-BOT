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

#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
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

struct ApiError {
  enum class Kind : std::uint8_t { Transport, Http, Kalshi };
  Kind kind = Kind::Transport;
  long http_status = 0;       // HTTP status for Http/Kalshi kinds
  int transport_code = 0;     // CURLcode for Transport kind
  std::string kalshi_code;    // e.g. "authentication_error" from the error body
  std::string message;
};

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

// Proactive rate limiter so data collection never relies on reactive 429s
// (throttled collection => gappy logs). Token bucket, refilled continuously.
class TokenBucket {
 public:
  TokenBucket(double rate_per_sec, double burst)
      : rate_(rate_per_sec), capacity_(burst), tokens_(burst),
        last_ns_(trading::mono_ns()) {}
  void acquire();  // blocks until a token is available (no-op if rate_ <= 0)

 private:
  const double rate_;
  const double capacity_;
  double tokens_;
  std::int64_t last_ns_;
  std::mutex m_;
};

class RestApi {
 public:
  RestApi(KalshiClient& client, Runtime rt, RetryPolicy policy = {},
          double rate_per_sec = 8.0, double burst = 8.0)
      : c_(client), rt_(std::move(rt)), policy_(policy), bucket_(rate_per_sec, burst) {}

  std::expected<ExchangeStatus, ApiError> exchange_status();
  std::expected<MarketsPage, ApiError> markets(const MarketsQuery& q);
  std::expected<MarketSummary, ApiError> market(std::string_view ticker);
  std::expected<OrderbookSnapshot, ApiError> orderbook(std::string_view ticker, int depth = 0);
  std::expected<std::string, ApiError> fills(std::string_view cursor = {});     // raw JSON (auth-gated)
  std::expected<std::string, ApiError> positions(std::string_view cursor = {}); // raw JSON (auth-gated)

  // Typed V2 order construction. Pure string building; no network, no send.
  static std::string build_order_json(const OrderSpec& spec);

  const Runtime& runtime() const { return rt_; }

 private:
  // GET with rate limit + retry (429/transient). Returns the final Response or
  // a Transport ApiError.
  std::expected<Response, ApiError> get_with_retry(const std::string& path);
  // Map a client call result to a typed ApiError when not ok.
  static ApiError map_error(const std::expected<Response, Error>& r);
  static ApiError parse_error_body(const Response& resp);

  KalshiClient& c_;
  Runtime rt_;
  RetryPolicy policy_;
  TokenBucket bucket_;
};

}  // namespace kalshi
