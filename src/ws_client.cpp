#include "kalshi/ws_client.hpp"

#include "simdjson.h"
#include "trading/timestamp.hpp"

#include <cstdio>

namespace kalshi {

namespace {

// Append a JSON array of quoted tickers: ["A","B"].
void append_ticker_array(std::string& j, const std::vector<std::string>& tickers) {
  j += '[';
  for (std::size_t i = 0; i < tickers.size(); ++i) {
    if (i) j += ',';
    j += '"';
    j += tickers[i];  // Kalshi tickers are [A-Za-z0-9._-]; safe to inline
    j += '"';
  }
  j += ']';
}

}  // namespace

KalshiWsClient::KalshiWsClient(IWebSocketTransport& transport, WsConfig cfg, Signer signer)
    : t_(transport), cfg_(std::move(cfg)), signer_(std::move(signer)), epoch_(cfg_.epoch) {}

WsHeaders KalshiWsClient::build_auth_headers(std::int64_t now_ms) const {
  const std::string ts = std::to_string(now_ms);
  // Signed message = timestamp + "GET" + ws_sign_path (no host, no query) — I10.
  std::string msg = ts;
  msg += "GET";
  msg += cfg_.ws_sign_path;
  auto sig = signer_ ? signer_(msg) : std::nullopt;
  if (!sig) return {};  // no auth -> handshake will be rejected
  return {{"KALSHI-ACCESS-KEY", cfg_.api_key_id},
          {"KALSHI-ACCESS-SIGNATURE", *sig},
          {"KALSHI-ACCESS-TIMESTAMP", ts}};
}

std::string KalshiWsClient::build_subscribe(int id, const std::vector<std::string>& tickers) const {
  std::string j = R"({"id":)";
  j += std::to_string(id);
  j += R"(,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":)";
  append_ticker_array(j, tickers);
  j += R"(,"use_yes_price":)";
  j += cfg_.use_yes_price ? "true" : "false";  // I5: always explicit
  j += "}}";
  return j;
}

std::string KalshiWsClient::build_unsubscribe(int id, const std::vector<std::uint64_t>& sids) const {
  std::string j = R"({"id":)";
  j += std::to_string(id);
  j += R"(,"cmd":"unsubscribe","params":{"sids":[)";
  for (std::size_t i = 0; i < sids.size(); ++i) {
    if (i) j += ',';
    j += std::to_string(sids[i]);
  }
  j += "]}}";
  return j;
}

std::string KalshiWsClient::build_get_snapshot(int id, const std::vector<std::uint64_t>& sids,
                                               const std::vector<std::string>& tickers) const {
  std::string j = R"({"id":)";
  j += std::to_string(id);
  j += R"(,"cmd":"update_subscription","params":{"sids":[)";
  for (std::size_t i = 0; i < sids.size(); ++i) {
    if (i) j += ',';
    j += std::to_string(sids[i]);
  }
  j += R"(],"action":"get_snapshot","market_tickers":)";
  append_ticker_array(j, tickers);
  j += "}}";
  return j;
}

void KalshiWsClient::start() {
  t_.on_message([this](const WsMessage& m) { on_message(m); });
  t_.set_url(cfg_.url);
  t_.set_headers(build_auth_headers(trading::wall_ns() / 1'000'000));
  t_.start();
}

void KalshiWsClient::stop() { t_.stop(); }

void KalshiWsClient::on_message(const WsMessage& m) {
  last_activity_ms_ = trading::wall_ns() / 1'000'000;  // any inbound frame = alive (I6)
  switch (m.type) {
    case WsMessage::Type::Open: on_open(); break;
    case WsMessage::Type::Close: on_close(); break;
    case WsMessage::Type::Text: ++messages_; on_text(m.data); break;
    case WsMessage::Type::Ping:
    case WsMessage::Type::Pong:
      break;  // transport auto-pongs (heartbeat echo); we only note liveness
    case WsMessage::Type::Error: ++errors_; break;
  }
}

void KalshiWsClient::on_open() {
  if (opened_once_) {
    ++epoch_;         // reconnect: new stream epoch (I8), drop old-sid state
    ++reconnects_;
  }
  opened_once_ = true;
  resubscribe();
}

void KalshiWsClient::on_close() {
  // State keyed to the dead connection's sids is abandoned; the next Open bumps
  // the epoch and resubscribes. (The transport owns backoff+jitter reconnect.)
}

void KalshiWsClient::resubscribe() {
  if (want_.empty()) return;
  t_.send_text(build_subscribe(next_id(), want_));
}

void KalshiWsClient::on_text(const std::string& text) {
  simdjson::padded_string json(text);
  simdjson::ondemand::parser parser;
  simdjson::ondemand::document doc;
  if (parser.iterate(json).get(doc) != simdjson::SUCCESS) return;

  std::string_view type;
  if (doc["type"].get(type) != simdjson::SUCCESS) return;
  std::uint64_t sid = 0, seq = 0;
  const bool has_sid = doc["sid"].get(sid) == simdjson::SUCCESS;
  const bool has_seq = doc["seq"].get(seq) == simdjson::SUCCESS;

  if (type == "error") {
    ++errors_;
    std::int64_t code = 0;
    simdjson::ondemand::object msg;
    if (doc["msg"].get(msg) == simdjson::SUCCESS) (void)msg["code"].get(code);
    if (code == 25) ++overflow_events_;  // buffer overflow: data lost (I7, Phase 3)
    return;
  }

  // ok / unsubscribed carry sid+seq and MUST advance the per-sid counter (I1).
  if (type == "ok" || type == "unsubscribed") {
    if (books_ && has_sid && has_seq) books_->on_control_seq(sid, epoch_, seq);
    return;
  }
  if (type == "subscribed") return;  // ack only

  // Market-data messages: decode via the Kalshi decoder into a NormalizedEvent.
  std::string_view ticker;
  simdjson::ondemand::object msg;
  if (doc["msg"].get(msg) == simdjson::SUCCESS) (void)msg["market_ticker"].get(ticker);

  trading::RawRecord rec;
  rec.source = trading::SourceId::Kalshi;
  rec.source_ticker = std::string(ticker);
  rec.channel = std::string(type);
  rec.source_stream_id = has_sid ? std::optional<std::uint64_t>(sid) : std::nullopt;
  rec.stream_epoch = epoch_;
  if (has_seq) rec.source_sequence = seq;
  rec.raw = text;  // full envelope, byte-exact

  auto ev = decoder_.decode(rec);
  if (!ev) return;
  if (sink_) sink_->on_event(*ev);
  if (!books_) return;

  // Route orderbook messages into the sid-aware manager.
  if (ev->kind() == trading::Kind::BookSnapshot) {
    const auto& bs = std::get<trading::BookSnapshot>(ev->payload);
    books_->bind(sid, epoch_, ev->entity_id);
    books_->on_snapshot(sid, epoch_, ev->entity_id, SnapshotView{bs.yes, bs.no, seq}, seq);
  } else if (ev->kind() == trading::Kind::BookDelta) {
    const auto& d = std::get<trading::BookDelta>(ev->payload);
    books_->on_delta(sid, epoch_, ev->entity_id, d.side, d.price, d.delta, seq);
  }
}

}  // namespace kalshi
