#include "kalshi/gateway.hpp"

#include "simdjson.h"
#include "trading/fixedpoint.hpp"

#include <cstdio>

namespace kalshi {

namespace {

// Parse a Kalshi "[[price_str, size_str], ...]" level array into Levels.
void parse_levels(simdjson::ondemand::value arr, std::vector<trading::Level>& out) {
  for (auto pair : arr.get_array()) {
    trading::Level lvl;
    int i = 0;
    bool ok = true;
    for (auto elem : pair.get_array()) {
      std::string_view s;
      if (elem.get(s) != simdjson::SUCCESS) { ok = false; break; }
      if (i == 0) {
        auto p = trading::parse_price_e4(s);
        if (!p) { ok = false; break; }
        lvl.price = *p;
      } else if (i == 1) {
        auto c = trading::parse_count_fp(s);
        if (!c) { ok = false; break; }
        lvl.size = *c;
      }
      ++i;
    }
    if (ok && i == 2) out.push_back(lvl);
  }
}

}  // namespace

std::optional<trading::NormalizedEvent> KalshiRawDecoder::decode(
    const trading::RawRecord& rec) {
  using namespace trading;
  NormalizedEvent ev;
  ev.source = rec.source;
  ev.entity_id = make_entity_id(rec.source, rec.source_ticker);
  ev.trace_id = next_trace_id();
  ev.source_event_time_ms = rec.source_event_time_ms;
  ev.local_receive_mono_ns = rec.recv_mono_ns;
  ev.local_receive_wall_ns = rec.recv_wall_ns;
  ev.source_sequence = rec.source_sequence;
  ev.source_stream_id = rec.source_stream_id;
  ev.stream_epoch = rec.stream_epoch;
  ev.raw_payload = RawPayloadView{rec.raw};  // valid until the sink returns

  try {
    simdjson::padded_string json(rec.raw);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(json);
    std::string_view type;
    if (doc["type"].get(type) != simdjson::SUCCESS) return std::nullopt;
    simdjson::ondemand::object msg;
    if (doc["msg"].get(msg) != simdjson::SUCCESS) return std::nullopt;

    if (type == "orderbook_snapshot") {
      BookSnapshot snap;
      simdjson::ondemand::value yes, no;
      if (msg["yes_dollars_fp"].get(yes) == simdjson::SUCCESS) parse_levels(yes, snap.yes);
      if (msg["no_dollars_fp"].get(no) == simdjson::SUCCESS) parse_levels(no, snap.no);
      ev.payload = std::move(snap);
      return ev;
    }
    if (type == "orderbook_delta") {
      BookDelta d;
      std::string_view ps, ds, side, coid;
      if (msg["price_dollars"].get(ps) != simdjson::SUCCESS) return std::nullopt;
      if (msg["delta_fp"].get(ds) != simdjson::SUCCESS) return std::nullopt;
      auto price = parse_price_e4(ps);   // excess precision -> nullopt -> reject
      auto delta = parse_delta_fp(ds);
      if (!price || !delta) return std::nullopt;
      d.price = *price;
      d.delta = *delta;
      d.side = (msg["side"].get(side) == simdjson::SUCCESS && side == "no")
                   ? Side::No : Side::Yes;
      // client_order_id passthrough (present only on your own order's delta).
      if (msg["client_order_id"].get(coid) == simdjson::SUCCESS && !coid.empty())
        d.client_order_id = std::string(coid);
      // ts_ms preferred as the source event time.
      std::int64_t ts_ms;
      if (msg["ts_ms"].get(ts_ms) == simdjson::SUCCESS) ev.source_event_time_ms = ts_ms;
      ev.payload = std::move(d);
      return ev;
    }
    if (type == "ticker") {
      Ticker t;
      std::string_view s;
      if (msg["price_dollars"].get(s) == simdjson::SUCCESS) t.last_price = parse_price_e4(s);
      if (msg["yes_bid_dollars"].get(s) == simdjson::SUCCESS) t.yes_bid = parse_price_e4(s);
      if (msg["yes_ask_dollars"].get(s) == simdjson::SUCCESS) t.yes_ask = parse_price_e4(s);
      if (msg["volume_fp"].get(s) == simdjson::SUCCESS) t.volume = parse_count_fp(s);
      if (msg["open_interest_fp"].get(s) == simdjson::SUCCESS) t.open_interest = parse_count_fp(s);
      std::int64_t ts_ms;  // ts_ms only; ts/time are deprecated (I9)
      if (msg["ts_ms"].get(ts_ms) == simdjson::SUCCESS) ev.source_event_time_ms = ts_ms;
      ev.payload = t;
      return ev;
    }
    if (type == "trade") {
      Trade tr;
      std::string_view s;
      // Yes-price in probability space (fallback to 1 - no_price if absent).
      if (msg["yes_price_dollars"].get(s) == simdjson::SUCCESS) {
        if (auto p = parse_price_e4(s)) tr.price = *p;
      } else if (msg["no_price_dollars"].get(s) == simdjson::SUCCESS) {
        if (auto p = parse_price_e4(s)) tr.price = static_cast<PriceE4>(kPriceMax - *p);
      }
      if (msg["count_fp"].get(s) == simdjson::SUCCESS)
        if (auto c = parse_count_fp(s)) tr.size = *c;
      // Prefer taker_outcome_side; fallback to deprecated taker_side.
      if (msg["taker_outcome_side"].get(s) == simdjson::SUCCESS ||
          msg["taker_side"].get(s) == simdjson::SUCCESS)
        tr.taker_side = (s == "no") ? Side::No : Side::Yes;
      if (msg["trade_id"].get(s) == simdjson::SUCCESS) tr.trade_id = std::string(s);
      std::int64_t ts_ms;
      if (msg["ts_ms"].get(ts_ms) == simdjson::SUCCESS) ev.source_event_time_ms = ts_ms;
      ev.payload = std::move(tr);
      return ev;
    }
    if (type == "market_lifecycle_v2" || type == "event_lifecycle") {
      Lifecycle lc;
      std::string_view et;
      if (msg["event_type"].get(et) == simdjson::SUCCESS) {
        if (et == "created") lc.state = Lifecycle::State::Created;
        else if (et == "open" || et == "active") lc.state = Lifecycle::State::Open;
        else if (et == "paused") lc.state = Lifecycle::State::Paused;
        else if (et == "closed") lc.state = Lifecycle::State::Closed;
        else if (et == "determined") lc.state = Lifecycle::State::Determined;
        else if (et == "settled") lc.state = Lifecycle::State::Settled;
        else { lc.state = Lifecycle::State::Unknown; lc.unknown_type = std::string(et); }
      } else {
        lc.state = Lifecycle::State::Unknown;  // type absent -> still round-trips
      }
      std::string_view sv;
      if (msg["settlement_value"].get(sv) == simdjson::SUCCESS)
        lc.settlement_value = parse_price_e4(sv);
      ev.payload = std::move(lc);
      return ev;
    }
    return std::nullopt;  // unmodeled type (round-trips via the raw log)
  } catch (const simdjson::simdjson_error&) {
    return std::nullopt;
  }
}

// --- KalshiExecutionEngine ---

void KalshiExecutionEngine::write_would_be(const trading::OrderIntent& oi) {
  if (log_path_.empty()) return;
  std::FILE* f = std::fopen(log_path_.c_str(), "ab");
  if (!f) return;
  std::fprintf(
      f,
      "{\"ts_ns\":%lld,\"mode\":\"%s\",\"env\":\"%s\",\"decision\":\"logged\","
      "\"entity_id\":%llu,\"trace_id\":%llu,\"side\":\"%s\",\"price\":%d,"
      "\"size\":%lld,\"tif\":\"%s\",\"post_only\":%s}\n",
      static_cast<long long>(trading::wall_ns()), to_string(rt_.mode), to_string(rt_.env),
      static_cast<unsigned long long>(oi.entity_id.v),
      static_cast<unsigned long long>(oi.trace_id.v), trading::to_string(oi.side),
      oi.limit_price, static_cast<long long>(oi.size),
      oi.tif == trading::OrderIntent::TimeInForce::IOC ? "ioc" : "gtc",
      oi.post_only ? "true" : "false");
  std::fclose(f);
}

trading::ExecDecision KalshiExecutionEngine::submit(const trading::OrderIntent& oi) {
  switch (rt_.mode) {
    case Mode::DataCollect:
      // The pipeline should never produce intents here; reject loudly.
      ++rejected_;
      std::fprintf(stderr,
                   "[exec] REJECTED order in data_collect mode (misconfiguration): "
                   "entity=%llu trace=%llu\n",
                   static_cast<unsigned long long>(oi.entity_id.v),
                   static_cast<unsigned long long>(oi.trace_id.v));
      return trading::ExecDecision::Rejected;

    case Mode::Shadow:
      // Never transmit — log the would-be order.
      write_would_be(oi);
      ++logged_;
      return trading::ExecDecision::Logged;

    case Mode::Live:
      // Throws unless live is explicitly enabled...
      require_orders_allowed(rt_);
      // ...and even then, real transmission is not wired in this pass. Fail
      // closed rather than silently claim a send.
      throw SafetyViolation(
          "live order transmission is not implemented in the foundations pass "
          "(fail closed)");
  }
  return trading::ExecDecision::Rejected;
}

}  // namespace kalshi
