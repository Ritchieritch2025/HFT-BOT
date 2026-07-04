// Golden-message decoder tests: exact Kalshi WS JSON (from
// docs/kalshi_ws_protocol.md) -> NormalizedEvent per type. Covers delta
// client_order_id passthrough, ticker/trade ts_ms, taker-side fallback,
// lifecycle known + unknown-type tolerance, excess-precision rejection, and
// unknown message type -> nullopt.

#include "kalshi/gateway.hpp"
#include "trading/bus.hpp"
#include "trading/storage.hpp"

#include <iostream>
#include <string>

using namespace trading;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}

RawRecord rec(std::string ticker, std::string raw, std::uint64_t seq = 0) {
  RawRecord r;
  r.source = SourceId::Kalshi;
  r.source_ticker = std::move(ticker);
  if (seq) r.source_sequence = seq;
  r.raw = std::move(raw);
  return r;
}
}  // namespace

int main() {
  kalshi::KalshiRawDecoder dec;

  // snapshot
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"orderbook_snapshot","sid":7,"seq":2,"msg":{"market_ticker":"MKT","yes_dollars_fp":[["0.0800","300.00"]],"no_dollars_fp":[["0.5400","20.00"]]}})", 2));
    check(ev && ev->kind() == Kind::BookSnapshot, "snapshot decodes");
    const auto& s = std::get<BookSnapshot>(ev->payload);
    check(s.yes.size() == 1 && s.yes[0].price == 800 && s.yes[0].size == 30000,
          "snapshot yes level 0.0800/300.00 -> 800/30000");
    check(ev->source == SourceId::Kalshi && ev->trace_id.v != 0, "snapshot carries source+trace");
  }

  // delta + client_order_id passthrough + ts_ms
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"orderbook_delta","sid":7,"seq":3,"msg":{"market_ticker":"MKT","price_dollars":"0.960","delta_fp":"-54.00","side":"yes","ts_ms":1669149841000,"client_order_id":"abc-123"}})", 3));
    check(ev && ev->kind() == Kind::BookDelta, "delta decodes");
    const auto& d = std::get<BookDelta>(ev->payload);
    check(d.price == 9600 && d.delta == -5400 && d.side == Side::Yes,
          "delta 0.960/-54.00/yes -> 9600/-5400");
    check(d.client_order_id.has_value() && *d.client_order_id == "abc-123",
          "delta client_order_id passthrough");
    check(ev->source_event_time_ms == 1669149841000, "delta ts_ms captured");
  }

  // delta with excess-precision price -> rejected (nullopt)
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"orderbook_delta","msg":{"price_dollars":"0.96001","delta_fp":"1.00","side":"yes"}})"));
    check(!ev.has_value(), "delta with >4dp price rejected");
  }

  // ticker, ts_ms only
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"ticker","sid":11,"msg":{"market_ticker":"MKT","price_dollars":"0.480","yes_bid_dollars":"0.450","yes_ask_dollars":"0.530","volume_fp":"33896.00","ts":1669149841,"ts_ms":1669149841000}})"));
    check(ev && ev->kind() == Kind::Ticker, "ticker decodes");
    const auto& t = std::get<Ticker>(ev->payload);
    check(t.last_price == 4800 && t.yes_bid == 4500 && t.yes_ask == 5300 && t.volume == 3389600,
          "ticker fields -> PriceE4/CountFp");
    check(ev->source_event_time_ms == 1669149841000, "ticker uses ts_ms (not ts)");
  }

  // trade: taker_outcome_side preferred, dedup id, ts_ms
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"trade","msg":{"trade_id":"t-9","market_ticker":"MKT","yes_price_dollars":"0.4200","count_fp":"5.00","taker_outcome_side":"no","ts_ms":123}})"));
    check(ev && ev->kind() == Kind::Trade, "trade decodes");
    const auto& tr = std::get<Trade>(ev->payload);
    check(tr.price == 4200 && tr.size == 500 && tr.taker_side == Side::No && tr.trade_id == "t-9",
          "trade yes 0.42/5.00/no/t-9");
  }
  // trade: fallback to deprecated taker_side when taker_outcome_side absent
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"trade","msg":{"trade_id":"t-10","yes_price_dollars":"0.5000","count_fp":"1.00","taker_side":"yes"}})"));
    check(ev && std::get<Trade>(ev->payload).taker_side == Side::Yes,
          "trade falls back to deprecated taker_side");
  }

  // lifecycle: known + unknown-type tolerance
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"market_lifecycle_v2","msg":{"market_ticker":"MKT","event_type":"determined","settlement_value":"1.0000"}})"));
    check(ev && ev->kind() == Kind::Lifecycle, "lifecycle decodes");
    const auto& lc = std::get<Lifecycle>(ev->payload);
    check(lc.state == Lifecycle::State::Determined && lc.settlement_value == 10000,
          "determined + settlement_value 1.0000");
  }
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"market_lifecycle_v2","msg":{"market_ticker":"MKT","event_type":"price_level_structure_updated"}})"));
    check(ev && ev->kind() == Kind::Lifecycle, "unknown lifecycle type still decodes");
    const auto& lc = std::get<Lifecycle>(ev->payload);
    check(lc.state == Lifecycle::State::Unknown && lc.unknown_type == "price_level_structure_updated",
          "unknown event_type round-trips as Unknown{type} (tolerated, not crashed)");
  }
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"event_lifecycle","msg":{"event_type":"created"}})"));
    check(ev && std::get<Lifecycle>(ev->payload).state == Lifecycle::State::Created,
          "event_lifecycle created decodes");
  }

  // unknown message type -> nullopt (round-trips only via raw log)
  {
    auto ev = dec.decode(rec("MKT", R"({"type":"some_future_channel","msg":{}})"));
    check(!ev.has_value(), "unknown message type -> nullopt");
  }

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
