#include "kalshi/ws_client.hpp"

#include "kalshi/ws_recorder.hpp"
#include "simdjson.h"
#include "trading/timestamp.hpp"

#include <cstdio>

namespace kalshi {

namespace {

// Append a JSON array of quoted Kalshi-safe strings: ["A","B"].
void append_string_array(std::string& j, const std::vector<std::string>& values) {
  j += '[';
  for (std::size_t i = 0; i < values.size(); ++i) {
    if (i) j += ',';
    j += '"';
    j += values[i];  // Kalshi channel/ticker names are [A-Za-z0-9._-]; safe to inline
    j += '"';
  }
  j += ']';
}

}  // namespace

KalshiWsClient::KalshiWsClient(IWebSocketTransport& transport, WsConfig cfg, Signer signer)
    : t_(transport),
      cfg_(std::move(cfg)),
      signer_(std::move(signer)),
      now_ms_([] { return trading::wall_ns() / 1'000'000; }),
      epoch_(cfg_.epoch) {}

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
  j += R"(,"cmd":"subscribe","params":{"channels":)";
  append_string_array(j, channels_);
  // Firehose: with no tickers we omit market_tickers entirely, so ticker/trade
  // channels stream EVERY market exchange-wide (Kalshi treats an absent filter
  // as "all"). A per-ticker subscribe still sends the filter as before.
  if (!tickers.empty()) {
    j += R"(,"market_tickers":)";
    append_string_array(j, tickers);
  }
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
  append_string_array(j, tickers);
  j += "}}";
  return j;
}

void KalshiWsClient::start() {
  t_.on_message([this](const WsMessage& m) { on_message(m); });
  t_.set_url(cfg_.url);
  refresh_auth();  // sign the first handshake
  t_.start();
}

void KalshiWsClient::stop() { t_.stop(); }

void KalshiWsClient::refresh_auth() {
  // ixwebsocket owns the reconnect loop and replays whatever headers are set on
  // the transport; a signature signed once at startup goes stale within minutes
  // and Kalshi 401s every reconnect until the process restarts (root cause of
  // the 2026-07-07 06:00–09:00 UTC capture gap). Re-sign with a current
  // timestamp before each connection attempt. Called on the transport thread
  // from on_close (dropped after open) and from the Error branch (handshake
  // rejected, e.g. 401) — both run to completion before ixwebsocket's next
  // connect(), which re-reads the extra headers.
  t_.set_headers(build_auth_headers(now_ms_()));
}

void KalshiWsClient::on_message(const WsMessage& m) {
  last_activity_ms_ = trading::wall_ns() / 1'000'000;  // any inbound frame = alive (I6)
  switch (m.type) {
    case WsMessage::Type::Open: on_open(); break;
    case WsMessage::Type::Close: on_close(); break;
    case WsMessage::Type::Text: ++messages_; on_text(m.data); break;
    case WsMessage::Type::Ping:
    case WsMessage::Type::Pong:
      break;  // transport auto-pongs (heartbeat echo); we only note liveness
    case WsMessage::Type::Error:
      ++errors_;
      // Surface the transport error reason (no secrets in it) so a failing
      // handshake/subscribe is diagnosable instead of a silent error counter.
      std::fprintf(stderr, "[ws] transport error: %.200s\n", m.data.c_str());
      // A transport Error is a failed connection attempt (handshake/connect);
      // ixwebsocket will retry. A 401 leaves no Open->Close, so re-sign HERE or
      // the stale signature repeats forever (the incident's 401 lockout loop).
      refresh_auth();
      break;
  }
}

void KalshiWsClient::on_open() {
  if (opened_once_) {
    ++epoch_;         // reconnect: new stream epoch (I8), drop old-sid state
    ++reconnects_;
    if (recorder_) recorder_->mark("epoch_change", epoch_);
  }
  opened_once_ = true;
  resubscribe();
}

void KalshiWsClient::on_close() {
  // State keyed to the dead connection's sids is abandoned; the next Open bumps
  // the epoch and resubscribes. (The transport owns backoff+jitter reconnect.)
  // Re-sign fresh auth headers now so the transport's next reconnect handshake
  // carries a current signature instead of the stale startup one (I10 incident).
  refresh_auth();
}

void KalshiWsClient::resubscribe() {
  if (want_.empty() && !firehose_) return;  // firehose subscribes with no filter
  t_.send_text(build_subscribe(next_id(), want_));
}

void KalshiWsClient::on_text(const std::string& text) {
  simdjson::padded_string json(text);
  simdjson::ondemand::parser parser;
  simdjson::ondemand::document doc;
  if (parser.iterate(json).get(doc) != simdjson::SUCCESS) return;
  // Confirm the frame is a JSON object before indexing fields — ondemand lookup
  // on a non-object top level (array/scalar/null) is UB. WS bytes are untrusted.
  simdjson::ondemand::object root;
  if (doc.get_object().get(root) != simdjson::SUCCESS) return;

  std::string_view type;
  if (root["type"].get(type) != simdjson::SUCCESS) return;
  std::uint64_t sid = 0, seq = 0;
  const bool has_sid = root["sid"].get(sid) == simdjson::SUCCESS;
  const bool has_seq = root["seq"].get(seq) == simdjson::SUCCESS;

  // Build the record once (lightweight envelope fields; the heavy decode is
  // isolated below). Record EVERY message on the connection (Phase 4) — one
  // owned copy handed to the recorder's ring.
  std::string_view env_ticker;
  {
    simdjson::ondemand::object m0;
    if (root["msg"].get(m0) == simdjson::SUCCESS) (void)m0["market_ticker"].get(env_ticker);
  }
  trading::RawRecord rec;
  rec.source = trading::SourceId::Kalshi;
  rec.channel = std::string(type);
  rec.source_ticker = std::string(env_ticker);
  rec.source_stream_id = has_sid ? std::optional<std::uint64_t>(sid) : std::nullopt;
  rec.stream_epoch = epoch_;
  if (has_seq) rec.source_sequence = seq;
  rec.recv_mono_ns = trading::mono_ns();
  rec.recv_wall_ns = trading::wall_ns();
  rec.raw = text;
  if (recorder_) recorder_->record(trading::RawRecord(rec));  // owned copy -> ring

  if (type == "error") {
    ++errors_;
    std::int64_t code = 0;
    simdjson::ondemand::object msg;
    if (root["msg"].get(msg) == simdjson::SUCCESS) (void)msg["code"].get(code);
    if (code == 25) ++overflow_events_;  // buffer overflow: data lost (I7, Phase 3)
    return;
  }

  // ok / unsubscribed carry sid+seq and MUST advance the per-sid counter (I1).
  if (type == "ok" || type == "unsubscribed") {
    if (books_ && has_sid && has_seq) books_->on_control_seq(sid, epoch_, seq);
    return;
  }
  if (type == "subscribed") return;  // ack only

  // Market-data messages: decode via the Kalshi decoder (fresh parse of rec.raw).
  auto ev = decoder_.decode(rec);
  if (!ev) return;
  if (sink_) sink_->on_event(*ev);

  // Lifecycle: a determined/settled market is done — free its book state (I9).
  if (ev->kind() == trading::Kind::Lifecycle) {
    const auto& lc = std::get<trading::Lifecycle>(ev->payload);
    if ((lc.state == trading::Lifecycle::State::Determined ||
         lc.state == trading::Lifecycle::State::Settled) &&
        books_) {
      books_->forget(ev->entity_id);
      ++lifecycle_deletes_;
    }
    return;
  }
  if (!books_) return;

  // Route orderbook messages into the sid-aware manager.
  if (ev->kind() == trading::Kind::BookSnapshot) {
    const auto& bs = std::get<trading::BookSnapshot>(ev->payload);
    books_->bind(sid, epoch_, ev->entity_id);
    books_->on_snapshot(sid, epoch_, ev->entity_id, SnapshotView{bs.yes, bs.no, seq}, seq);
  } else if (ev->kind() == trading::Kind::BookDelta) {
    const auto& d = std::get<trading::BookDelta>(ev->payload);
    if (books_->on_delta(sid, epoch_, ev->entity_id, d.side, d.price, d.delta, seq) ==
            ApplyResult::NeedResync &&
        recorder_) {
      recorder_->mark("gap", epoch_, sid);  // stream gap -> marker in the raw log
    }
  }
}

}  // namespace kalshi
