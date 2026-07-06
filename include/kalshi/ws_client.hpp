#pragma once
//
// Kalshi WebSocket v2 client — transport-independent. Drives an
// IWebSocketTransport (ixwebsocket in prod, MockWebSocketTransport in tests):
//   - builds the signed handshake auth headers (docs/kalshi_ws_protocol.md I10);
//   - builds subscribe / unsubscribe / update_subscription commands with a
//     monotonic client `id` and explicit market_tickers + use_yes_price:false (I5);
//   - parses the response envelope (id/type/sid/seq/msg), tracks sid, routes
//     orderbook snapshots/deltas through the decoder into an OrderBookManager
//     (per-sid seq handling, I1/I3), and emits NormalizedEvents to a sink;
//   - on reconnect (transport Open after the first) bumps stream_epoch (I8),
//     drops old-sid state, and resubscribes;
//   - tracks last-activity for the ping-silence watchdog (I6).
//
// Read-only market data. Never sends orders.

#include "kalshi/gateway.hpp"
#include "kalshi/orderbook.hpp"
#include "kalshi/ws_transport.hpp"
#include "trading/bus.hpp"

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace kalshi {

class WsRecorder;  // include/kalshi/ws_recorder.hpp

// I5: the orderbook no-side pricing convention. `false` = no-leg pricing (the
// no side carries its own price). Kalshi will flip the default and later remove
// the flag; flipping our convention is this ONE constant + the tests that pin
// it. Both the subscribe builder and the decoder honor it.
inline constexpr bool kUseYesPrice = false;

struct WsConfig {
  std::string url;                             // wss://.../trade-api/ws/v2 (or ws:// mock)
  std::string api_key_id;
  std::string ws_sign_path = "/trade-api/ws/v2";
  std::uint32_t epoch = 1;                     // starting stream epoch
  bool use_yes_price = kUseYesPrice;           // I5: sent explicitly
};

class KalshiWsClient {
 public:
  // Signs `timestamp + "GET" + ws_sign_path` with RSA-PSS -> base64. Returns
  // nullopt on failure (auth headers then omitted -> handshake will 401).
  using Signer = std::function<std::optional<std::string>(std::string_view)>;

  KalshiWsClient(IWebSocketTransport& transport, WsConfig cfg, Signer signer);

  void set_sink(trading::MarketDataSink* s) { sink_ = s; }
  void set_book_manager(OrderBookManager* m) { books_ = m; }
  void set_recorder(WsRecorder* r) { recorder_ = r; }  // durable raw log (Phase 4)

  // Register orderbook_delta subscriptions; sent on open and every resubscribe.
  void want_orderbook(std::vector<std::string> tickers) { want_ = std::move(tickers); }
  // Firehose mode: subscribe to the configured channels with NO market filter,
  // streaming every market exchange-wide (use with ticker/trade channels).
  void set_firehose(bool on) { firehose_ = on; }
  void want_channels(std::vector<std::string> channels) {
    if (!channels.empty()) channels_ = std::move(channels);
  }

  void start();  // set url + auth headers, connect
  void stop();

  // --- exposed for tests (pure builders / auth) ---
  WsHeaders build_auth_headers(std::int64_t now_ms) const;
  std::string build_subscribe(int id, const std::vector<std::string>& tickers) const;
  std::string build_unsubscribe(int id, const std::vector<std::uint64_t>& sids) const;
  std::string build_get_snapshot(int id, const std::vector<std::uint64_t>& sids,
                                 const std::vector<std::string>& tickers) const;

  // --- stats / health ---
  std::uint32_t epoch() const { return epoch_; }
  std::uint64_t messages() const { return messages_; }
  std::uint64_t reconnects() const { return reconnects_; }
  std::uint64_t errors() const { return errors_; }
  std::uint64_t overflow_events() const { return overflow_events_; }  // error 25 (I7)
  std::uint64_t lifecycle_deletes() const { return lifecycle_deletes_; }
  std::int64_t last_activity_ms() const { return last_activity_ms_; }
  bool ping_silent(std::int64_t now_ms, std::int64_t timeout_ms = 30000) const {
    return last_activity_ms_ != 0 && now_ms - last_activity_ms_ > timeout_ms;
  }

 private:
  void on_message(const WsMessage&);
  void on_open();
  void on_close();
  void on_text(const std::string& text);
  void resubscribe();
  int next_id() { return next_id_++; }

  IWebSocketTransport& t_;
  WsConfig cfg_;
  Signer signer_;
  KalshiRawDecoder decoder_;
  trading::MarketDataSink* sink_ = nullptr;
  OrderBookManager* books_ = nullptr;
  WsRecorder* recorder_ = nullptr;
  std::vector<std::string> want_;
  bool firehose_ = false;
  std::vector<std::string> channels_{"orderbook_delta"};

  std::uint32_t epoch_;
  bool opened_once_ = false;
  int next_id_ = 1;
  std::uint64_t messages_ = 0, reconnects_ = 0, errors_ = 0, overflow_events_ = 0;
  std::uint64_t lifecycle_deletes_ = 0;
  std::int64_t last_activity_ms_ = 0;
};

}  // namespace kalshi
