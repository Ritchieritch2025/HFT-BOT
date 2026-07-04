#pragma once
//
// Transport-independent WebSocket seam. Everything above this interface (auth,
// subscription management, sid tracking, book application) is testable without
// any real socket by driving a MockWebSocketTransport. The production
// implementation (IxWebSocketTransport, src/ix_transport.cpp) wraps the
// vendored ixwebsocket library; a hand-rolled transport would slot in here too
// if the quantitative trigger is ever hit.
//
// Threading: the on_message callback fires on the transport's own thread. The
// callee must not block (bus threading contract). The KalshiWsClient is the
// single writer of outbound frames.

#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace kalshi {

struct WsMessage {
  enum class Type { Open, Close, Error, Text, Ping, Pong };
  Type type = Type::Text;
  std::string data;  // text payload / close reason / error text / ping payload
  int code = 0;      // close code or error code (0 otherwise)
};

using WsHeaders = std::vector<std::pair<std::string, std::string>>;

struct IWebSocketTransport {
  using OnMessage = std::function<void(const WsMessage&)>;

  virtual ~IWebSocketTransport() = default;

  virtual void set_url(const std::string& url) = 0;
  virtual void set_headers(const WsHeaders& headers) = 0;   // handshake headers
  virtual void on_message(OnMessage cb) = 0;

  virtual void start() = 0;  // connect; Open/Close/Error/Text arrive via callback
  virtual void stop() = 0;   // clean close handshake
  virtual bool send_text(const std::string& text) = 0;  // false if not open
  virtual bool is_open() const = 0;
};

// In-process test double. Records outbound frames, lets the test inject inbound
// messages, and simulates open/close — no sockets, no threads.
class MockWebSocketTransport final : public IWebSocketTransport {
 public:
  void set_url(const std::string& url) override { url_ = url; }
  void set_headers(const WsHeaders& headers) override { headers_ = headers; }
  void on_message(OnMessage cb) override { cb_ = std::move(cb); }

  void start() override {
    open_ = true;
    if (cb_) cb_(WsMessage{WsMessage::Type::Open, "", 0});
  }
  void stop() override {
    if (open_ && cb_) cb_(WsMessage{WsMessage::Type::Close, "stopped", 1000});
    open_ = false;
  }
  bool send_text(const std::string& text) override {
    if (!open_) return false;
    sent_.push_back(text);
    return true;
  }
  bool is_open() const override { return open_; }

  // --- test controls ---
  void inject_text(const std::string& text) {
    if (cb_) cb_(WsMessage{WsMessage::Type::Text, text, 0});
  }
  void inject_ping(const std::string& payload) {
    if (cb_) cb_(WsMessage{WsMessage::Type::Ping, payload, 0});
  }
  void inject_close(int code = 1006) {
    open_ = false;
    if (cb_) cb_(WsMessage{WsMessage::Type::Close, "dropped", code});
  }
  void drop() { inject_close(1006); }  // simulate an unexpected disconnect

  const std::vector<std::string>& sent() const { return sent_; }
  const std::string& last_sent() const { return sent_.back(); }
  const WsHeaders& headers() const { return headers_; }
  const std::string& url() const { return url_; }
  void clear_sent() { sent_.clear(); }

 private:
  std::string url_;
  WsHeaders headers_;
  OnMessage cb_;
  std::vector<std::string> sent_;
  bool open_ = false;
};

}  // namespace kalshi
