#pragma once
//
// Production IWebSocketTransport backed by the vendored ixwebsocket library.
// The ixwebsocket headers are confined to src/ix_transport.cpp (pimpl) so the
// rest of the codebase never sees them. permessage-deflate is disabled at
// runtime here (and compiled out at build); the library owns backoff+jitter
// reconnection and RFC-compliant pong echoing of the heartbeat payload.

#include "kalshi/ws_transport.hpp"

#include <memory>

namespace ix {
class WebSocket;
}

namespace kalshi {

class IxWebSocketTransport final : public IWebSocketTransport {
 public:
  IxWebSocketTransport();
  ~IxWebSocketTransport() override;

  void set_url(const std::string& url) override;
  void set_headers(const WsHeaders& headers) override;
  void on_message(OnMessage cb) override;

  void start() override;
  void stop() override;
  bool send_text(const std::string& text) override;
  bool is_open() const override;

 private:
  std::unique_ptr<ix::WebSocket> ws_;
  OnMessage cb_;
};

}  // namespace kalshi
