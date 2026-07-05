#include "kalshi/rest_api.hpp"

#include <curl/curl.h>

#include "simdjson.h"

#include <algorithm>
#include <cstdio>
#include <limits>
#include <thread>

namespace kalshi {

namespace {

// Transient libcurl failures worth retrying (connection/timeout/reset). A
// deliberate allowlist — most CURLcodes are permanent misconfigurations.
bool transient_curl(int code) {
  switch (code) {
    case CURLE_COULDNT_CONNECT:
    case CURLE_COULDNT_RESOLVE_HOST:
    case CURLE_OPERATION_TIMEDOUT:
    case CURLE_SEND_ERROR:
    case CURLE_RECV_ERROR:
    case CURLE_GOT_NOTHING:
    case CURLE_PARTIAL_FILE:
      return true;
    default:
      return false;
  }
}

bool transient_http(long status) {
  return status == 502 || status == 503 || status == 504;
}

// Decode a Kalshi dollar-string OR integer-cents number into PriceE4. Field is
// tried by (dollars_key) first, then (cents_key). Absent => nullopt.
std::optional<PriceE4> decode_price(simdjson::ondemand::object& obj,
                                    const char* dollars_key, const char* cents_key) {
  std::string_view s;
  if (obj[dollars_key].get(s) == simdjson::SUCCESS) {
    auto v = trading::parse_price_e4(s);
    if (v) return v;
  }
  std::int64_t cents = 0;
  if (cents_key && obj[cents_key].get(cents) == simdjson::SUCCESS)
    return trading::cents_to_e4(static_cast<int>(cents));
  return std::nullopt;
}

std::optional<CountFp> decode_count(simdjson::ondemand::object& obj,
                                    const char* fp_key, const char* legacy_key) {
  std::string_view s;
  if (obj[fp_key].get(s) == simdjson::SUCCESS) {
    auto v = trading::parse_count_fp(s);
    if (v) return v;
  }
  std::int64_t n = 0;
  if (legacy_key && obj[legacy_key].get(n) == simdjson::SUCCESS)
    return static_cast<CountFp>(n * 100);  // whole contracts -> x100
  return std::nullopt;
}

// Parse "[[price,size],...]" level arrays into Levels. `cents` selects the
// legacy integer-cents schema ([42,100]) vs the dollar-string schema
// (["0.4200","100.00"]).
void decode_levels(simdjson::ondemand::value arr, std::vector<Level>& out, bool cents) {
  for (auto pair : arr.get_array()) {
    Level lvl;
    int i = 0;
    bool ok = true;
    for (auto elem : pair.get_array()) {
      if (i == 0) {
        if (cents) {
          std::int64_t c;
          if (elem.get(c) != simdjson::SUCCESS) { ok = false; break; }
          lvl.price = trading::cents_to_e4(static_cast<int>(c));
        } else {
          std::string_view s;
          auto p = elem.get(s) == simdjson::SUCCESS ? trading::parse_price_e4(s) : std::nullopt;
          if (!p) { ok = false; break; }
          lvl.price = *p;
        }
      } else if (i == 1) {
        if (cents) {
          std::int64_t n;
          if (elem.get(n) != simdjson::SUCCESS) { ok = false; break; }
          lvl.size = n * 100;  // whole contracts -> CountFp
        } else {
          std::string_view s;
          auto c = elem.get(s) == simdjson::SUCCESS ? trading::parse_count_fp(s) : std::nullopt;
          if (!c) { ok = false; break; }
          lvl.size = *c;
        }
      }
      ++i;
    }
    if (ok && i == 2) out.push_back(lvl);
  }
  std::sort(out.begin(), out.end(),
            [](const Level& a, const Level& b) { return a.price < b.price; });
}

std::string escape(std::string_view s) {  // ticker path-segment safety
  std::string o;
  for (char c : s)
    if (c == '?' || c == '#' || c == ' ') { /* drop unsafe */ } else o += c;
  return o;
}

}  // namespace

namespace {
// Proactively throttle on a bucket: reserve `cost` and, if the grant carries a
// wait, sleep it before returning (reserve-before-send, hard rule 3). Deadline
// is unbounded here — collection must not skip work, only pace it.
void throttle(TokenBucketI64& bucket, int cost) {
  const Reservation r = bucket.reserve_or_wait(cost, trading::mono_ns(),
                                               std::numeric_limits<std::int64_t>::max());
  if (r.granted && r.wait_ns > 0)
    std::this_thread::sleep_for(std::chrono::nanoseconds(r.wait_ns));
}
}  // namespace

ApiError RestApi::parse_error_body(const Response& resp) {
  ApiError e;
  e.kind = ApiError::Kind::Http;
  e.http_status = resp.status;
  e.message = "HTTP " + std::to_string(resp.status);
  try {
    simdjson::padded_string json(resp.body);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(json);
    simdjson::ondemand::object err;
    if (doc["error"].get(err) == simdjson::SUCCESS) {
      e.kind = ApiError::Kind::Kalshi;
      std::string_view code, msg;
      if (err["code"].get(code) == simdjson::SUCCESS) e.kalshi_code = std::string(code);
      if (err["message"].get(msg) == simdjson::SUCCESS) e.message = std::string(msg);
    }
  } catch (const simdjson::simdjson_error&) {
    // Non-JSON body: keep the HTTP-status message.
  }
  return e;
}

ApiError RestApi::map_error(const std::expected<Response, Error>& r) {
  ApiError e;
  const Error& err = r.error();
  e.kind = ApiError::Kind::Transport;
  e.transport_code = err.code;
  e.message = err.message;
  return e;
}

std::expected<Response, ApiError> RestApi::get_with_retry(const std::string& path) {
  ApiError last;
  for (int attempt = 0; attempt < policy_.max_attempts; ++attempt) {
    throttle(read_bucket_, 1);  // 1 read token/GET; per-endpoint cost lands in T5
    auto r = c_.request(Method::Get, path);

    if (!r) {  // transport failure
      last = map_error(r);
      if (transient_curl(r.error().code) && attempt + 1 < policy_.max_attempts) {
        long backoff = std::min(policy_.max_backoff_ms,
                                policy_.base_ms * (1L << attempt));
        std::this_thread::sleep_for(std::chrono::milliseconds(backoff));
        continue;
      }
      return std::unexpected(last);
    }

    const long status = r->status;
    if (status == 429 || transient_http(status)) {
      last = parse_error_body(*r);
      if (attempt + 1 < policy_.max_attempts) {
        long wait;
        if (status == 429 && r->retry_after_ms >= 0) {
          // Honor Retry-After but clamp — a hostile "Retry-After: 86400" must
          // not stall the collector.
          wait = std::min<long>(r->retry_after_ms, policy_.retry_after_cap_ms);
        } else {
          wait = std::min(policy_.max_backoff_ms, policy_.base_ms * (1L << attempt));
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(wait));
        continue;
      }
      return std::unexpected(last);
    }

    return *r;  // any other status (incl. 4xx we don't retry) returns as Response
  }
  return std::unexpected(last);
}

std::expected<ExchangeStatus, ApiError> RestApi::exchange_status() {
  auto r = get_with_retry("/exchange/status");
  if (!r) return std::unexpected(r.error());
  if (!r->ok()) return std::unexpected(parse_error_body(*r));
  ExchangeStatus s;
  try {
    simdjson::padded_string json(r->body);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(json);
    bool b;
    if (doc["exchange_active"].get(b) == simdjson::SUCCESS) s.exchange_active = b;
    if (doc["trading_active"].get(b) == simdjson::SUCCESS) s.trading_active = b;
  } catch (const simdjson::simdjson_error& e) {
    return std::unexpected(ApiError{ApiError::Kind::Http, r->status, 0, "", e.what()});
  }
  return s;
}

std::expected<MarketsPage, ApiError> RestApi::markets(const MarketsQuery& q) {
  std::string path = "/markets?limit=" + std::to_string(q.limit);
  if (!q.status.empty()) path += "&status=" + escape(q.status);
  if (!q.series_ticker.empty()) path += "&series_ticker=" + escape(q.series_ticker);
  if (!q.cursor.empty()) path += "&cursor=" + escape(q.cursor);

  auto r = get_with_retry(path);
  if (!r) return std::unexpected(r.error());
  if (!r->ok()) return std::unexpected(parse_error_body(*r));

  MarketsPage page;
  try {
    simdjson::padded_string json(r->body);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(json);
    std::string_view cursor;
    if (doc["cursor"].get(cursor) == simdjson::SUCCESS) page.next_cursor = std::string(cursor);
    simdjson::ondemand::array arr;
    if (doc["markets"].get(arr) == simdjson::SUCCESS) {
      for (auto m : arr) {
        simdjson::ondemand::object obj;
        if (m.get(obj) != simdjson::SUCCESS) continue;
        MarketSummary ms;
        std::string_view t, st;
        if (obj["ticker"].get(t) == simdjson::SUCCESS) ms.ticker = std::string(t);
        if (obj["status"].get(st) == simdjson::SUCCESS) ms.status = std::string(st);
        ms.yes_bid = decode_price(obj, "yes_bid_dollars", "yes_bid");
        ms.yes_ask = decode_price(obj, "yes_ask_dollars", "yes_ask");
        ms.last_price = decode_price(obj, "last_price_dollars", "last_price");
        ms.volume = decode_count(obj, "volume_fp", "volume");
        ms.open_interest = decode_count(obj, "open_interest_fp", "open_interest");
        page.markets.push_back(std::move(ms));
      }
    }
  } catch (const simdjson::simdjson_error& e) {
    return std::unexpected(ApiError{ApiError::Kind::Http, r->status, 0, "", e.what()});
  }
  return page;
}

std::expected<MarketSummary, ApiError> RestApi::market(std::string_view ticker) {
  auto r = get_with_retry("/markets/" + escape(ticker));
  if (!r) return std::unexpected(r.error());
  if (!r->ok()) return std::unexpected(parse_error_body(*r));
  MarketSummary ms;
  try {
    simdjson::padded_string json(r->body);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(json);
    simdjson::ondemand::object obj;
    if (doc["market"].get(obj) != simdjson::SUCCESS)
      return std::unexpected(ApiError{ApiError::Kind::Http, r->status, 0, "", "no market object"});
    std::string_view t, st;
    if (obj["ticker"].get(t) == simdjson::SUCCESS) ms.ticker = std::string(t);
    if (obj["status"].get(st) == simdjson::SUCCESS) ms.status = std::string(st);
    ms.yes_bid = decode_price(obj, "yes_bid_dollars", "yes_bid");
    ms.yes_ask = decode_price(obj, "yes_ask_dollars", "yes_ask");
    ms.last_price = decode_price(obj, "last_price_dollars", "last_price");
    ms.volume = decode_count(obj, "volume_fp", "volume");
    ms.open_interest = decode_count(obj, "open_interest_fp", "open_interest");
  } catch (const simdjson::simdjson_error& e) {
    return std::unexpected(ApiError{ApiError::Kind::Http, r->status, 0, "", e.what()});
  }
  return ms;
}

std::expected<OrderbookSnapshot, ApiError> RestApi::orderbook(std::string_view ticker, int depth) {
  std::string path = "/markets/" + escape(ticker) + "/orderbook";
  if (depth > 0) path += "?depth=" + std::to_string(depth);
  auto r = get_with_retry(path);
  if (!r) return std::unexpected(r.error());
  if (!r->ok()) return std::unexpected(parse_error_body(*r));

  OrderbookSnapshot ob;
  ob.ticker = std::string(ticker);
  try {
    simdjson::padded_string json(r->body);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(json);
    simdjson::ondemand::object book;
    // New schema: "orderbook_fp" (dollar strings). Legacy: "orderbook" (cents).
    bool cents = false;
    if (doc["orderbook_fp"].get(book) == simdjson::SUCCESS) {
      cents = false;
    } else if (doc["orderbook"].get(book) == simdjson::SUCCESS) {
      cents = true;
    } else {
      return ob;  // empty book
    }
    simdjson::ondemand::value yes, no;
    const char* yes_key = cents ? "yes" : "yes_dollars";
    const char* no_key = cents ? "no" : "no_dollars";
    if (book[yes_key].get(yes) == simdjson::SUCCESS) decode_levels(yes, ob.yes, cents);
    if (book[no_key].get(no) == simdjson::SUCCESS) decode_levels(no, ob.no, cents);
  } catch (const simdjson::simdjson_error& e) {
    return std::unexpected(ApiError{ApiError::Kind::Http, r->status, 0, "", e.what()});
  }
  return ob;
}

std::expected<std::string, ApiError> RestApi::fills(std::string_view cursor) {
  std::string path = "/portfolio/fills";
  if (!cursor.empty()) path += "?cursor=" + escape(cursor);
  auto r = get_with_retry(path);
  if (!r) return std::unexpected(r.error());
  if (!r->ok()) return std::unexpected(parse_error_body(*r));
  return r->body;
}

std::expected<std::string, ApiError> RestApi::positions(std::string_view cursor) {
  std::string path = "/portfolio/positions";
  if (!cursor.empty()) path += "?cursor=" + escape(cursor);
  auto r = get_with_retry(path);
  if (!r) return std::unexpected(r.error());
  if (!r->ok()) return std::unexpected(parse_error_body(*r));
  return r->body;
}

// --- KalshiDataSource ---

int KalshiDataSource::poll_once() {
  int n = 0;
  for (const std::string& t : tickers_) {
    auto ob = api_.orderbook(t);
    if (!ob) continue;  // transient/typed error; skip this tick
    trading::NormalizedEvent ev;
    ev.source = trading::SourceId::Kalshi;
    ev.entity_id = reg_.register_entity(trading::SourceId::Kalshi, t);
    ev.trace_id = trading::next_trace_id();
    const auto ts = trading::stamp_receive();
    ev.local_receive_mono_ns = ts.local_receive_mono_ns;
    ev.local_receive_wall_ns = ts.local_receive_wall_ns;
    ev.publish_time_ns = ts.local_receive_mono_ns;
    trading::BookSnapshot snap;
    snap.yes = std::move(ob->yes);
    snap.no = std::move(ob->no);
    ev.payload = std::move(snap);
    if (sink_) sink_->on_event(ev);
    ++emitted_;
    ++n;
  }
  return n;
}

void KalshiDataSource::start() {
  while (!stop_) {
    poll_once();
    std::this_thread::sleep_for(std::chrono::milliseconds(interval_ms_));
  }
}

std::expected<std::vector<OrderbookSnapshot>, ApiError> RestApi::batch_orderbook(
    const std::vector<std::string>& tickers) {
  if (tickers.empty() || tickers.size() > 100)
    return std::unexpected(ApiError{ApiError::Kind::Http, 0, 0, "", "batch size must be 1..100"});
  std::string path = "/markets/orderbooks";
  for (std::size_t i = 0; i < tickers.size(); ++i)
    path += (i == 0 ? "?tickers=" : "&tickers=") + escape(tickers[i]);  // form-explode

  auto r = get_with_retry(path);
  if (!r) return std::unexpected(r.error());
  if (!r->ok()) return std::unexpected(parse_error_body(*r));

  std::vector<OrderbookSnapshot> out;
  try {
    simdjson::padded_string json(r->body);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(json);
    simdjson::ondemand::array arr;
    if (doc["orderbooks"].get(arr) == simdjson::SUCCESS) {
      for (auto item : arr) {
        simdjson::ondemand::object obj;
        if (item.get(obj) != simdjson::SUCCESS) continue;
        OrderbookSnapshot ob;
        std::string_view tk;
        if (obj["market_ticker"].get(tk) == simdjson::SUCCESS) ob.ticker = std::string(tk);
        simdjson::ondemand::object book;
        bool cents = false;
        if (obj["orderbook_fp"].get(book) == simdjson::SUCCESS) cents = false;
        else if (obj["orderbook"].get(book) == simdjson::SUCCESS) cents = true;
        else { out.push_back(std::move(ob)); continue; }
        simdjson::ondemand::value yes, no;
        if (book[cents ? "yes" : "yes_dollars"].get(yes) == simdjson::SUCCESS)
          decode_levels(yes, ob.yes, cents);
        if (book[cents ? "no" : "no_dollars"].get(no) == simdjson::SUCCESS)
          decode_levels(no, ob.no, cents);
        out.push_back(std::move(ob));
      }
    }
  } catch (const simdjson::simdjson_error& e) {
    return std::unexpected(ApiError{ApiError::Kind::Http, r->status, 0, "", e.what()});
  }
  return out;
}

std::expected<AccountLimits, ApiError> RestApi::account_limits() {
  const bool live = rt_.mode == Mode::Live;
  auto fail = [&](ApiError e) -> std::expected<AccountLimits, ApiError> {
    if (live) return std::unexpected(e);  // live fails closed
    std::fprintf(stderr, "[limits] account_limits fell back to conservative basic tier: %s\n",
                 e.message.c_str());
    return conservative_limits();  // off-live: conservative fallback, never errors
  };

  auto r = get_with_retry("/account/limits");
  if (!r) return fail(r.error());
  if (!r->ok()) return fail(parse_error_body(*r));

  auto parsed = parse_account_limits(r->body);
  if (!parsed)
    return fail(ApiError{ApiError::Kind::Http, r->status, 0, "", "malformed /account/limits body"});

  // Fail-closed invariant checks (T1.2): live throws, off-live warns + fallback.
  if (auto err = limits_invariant_error(*parsed)) {
    if (live)
      return std::unexpected(ApiError{ApiError::Kind::Http, r->status, 0, "",
                                      "account limits failed invariant: " + *err});
    std::fprintf(stderr, "[limits] invariant violation (%s); using conservative basic tier\n",
                 err->c_str());
    return conservative_limits();
  }

  // Tier-table safeguard (F4): warn-only, keep server values (authoritative).
  for (const auto& w : tier_table_warnings(*parsed))
    std::fprintf(stderr, "[limits] %s: %s\n", kEvLimitsTableMismatch, w.c_str());

  return *parsed;
}

std::expected<EndpointCostTable, ApiError> RestApi::endpoint_costs() {
  const bool live = rt_.mode == Mode::Live;
  auto fail = [&](ApiError e) -> std::expected<EndpointCostTable, ApiError> {
    if (live) return std::unexpected(e);
    std::fprintf(stderr, "[limits] endpoint_costs fell back to all-default costs: %s\n",
                 e.message.c_str());
    return EndpointCostTable{};  // default_cost=10, from_server=false
  };

  auto r = get_with_retry("/account/endpoint_costs");
  if (!r) return fail(r.error());
  if (!r->ok()) return fail(parse_error_body(*r));
  return parse_endpoint_costs(r->body);  // tolerant parser; never throws
}

std::string RestApi::build_order_json(const OrderSpec& spec) {
  // V2 book is YES-normalized: buy-YES / sell-NO rest as bids; sell-YES /
  // buy-NO as asks at the complementary price.
  const bool is_bid = spec.buy == (spec.side == trading::Side::Yes);
  const PriceE4 yes_price = (spec.side == trading::Side::Yes)
                                ? spec.price
                                : static_cast<PriceE4>(trading::kPriceMax - spec.price);

  std::string j;
  j.reserve(256);
  j += R"({"ticker":")";
  j += spec.ticker;
  j += R"(","side":")";
  j += is_bid ? "bid" : "ask";
  j += R"(","count":")";
  j += trading::format_count_fp(spec.count);
  j += R"(","price":")";
  j += trading::format_price_e4(yes_price);
  j += R"(","time_in_force":")";
  j += spec.ioc ? "immediate_or_cancel" : "good_till_canceled";
  j += R"(","self_trade_prevention_type":"taker_at_cross")";
  if (spec.post_only) j += R"(,"post_only":true)";
  if (!spec.client_order_id.empty()) {
    j += R"(,"client_order_id":")";
    j += spec.client_order_id;
    j += '"';
  }
  j += '}';
  return j;
}

}  // namespace kalshi
