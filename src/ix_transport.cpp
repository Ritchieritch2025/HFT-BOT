#include "kalshi/ix_transport.hpp"

#include "ixwebsocket/IXWebSocket.h"
#include "ixwebsocket/IXWebSocketHttpHeaders.h"

namespace kalshi {

IxWebSocketTransport::IxWebSocketTransport() : ws_(std::make_unique<ix::WebSocket>()) {
  ws_->disablePerMessageDeflate();  // deflate off at runtime (compiled out too)
  // Library owns reconnection with capped exponential backoff + jitter.
  ws_->enableAutomaticReconnection();
  ws_->setMinWaitBetweenReconnectionRetries(1000);    // 1s
  ws_->setMaxWaitBetweenReconnectionRetries(30000);   // 30s cap
  ws_->setPingInterval(0);  // Kalshi pings us; we only pong (auto) + watch silence
}

IxWebSocketTransport::~IxWebSocketTransport() {
  if (ws_) ws_->stop();
}

void IxWebSocketTransport::set_url(const std::string& url) { ws_->setUrl(url); }

void IxWebSocketTransport::set_headers(const WsHeaders& headers) {
  ix::WebSocketHttpHeaders h;
  for (const auto& [k, v] : headers) h[k] = v;
  ws_->setExtraHeaders(h);
}

void IxWebSocketTransport::on_message(OnMessage cb) { cb_ = std::move(cb); }

void IxWebSocketTransport::start() {
  ws_->setOnMessageCallback([this](const ix::WebSocketMessagePtr& m) {
    if (!cb_) return;
    WsMessage out;
    switch (m->type) {
      case ix::WebSocketMessageType::Open:
        out.type = WsMessage::Type::Open;
        break;
      case ix::WebSocketMessageType::Close:
        out.type = WsMessage::Type::Close;
        out.code = m->closeInfo.code;
        out.data = m->closeInfo.reason;
        break;
      case ix::WebSocketMessageType::Error:
        out.type = WsMessage::Type::Error;
        out.data = m->errorInfo.reason;
        break;
      case ix::WebSocketMessageType::Message:
        out.type = WsMessage::Type::Text;
        out.data = m->str;
        break;
      case ix::WebSocketMessageType::Ping:
        out.type = WsMessage::Type::Ping;
        out.data = m->str;
        break;
      case ix::WebSocketMessageType::Pong:
        out.type = WsMessage::Type::Pong;
        out.data = m->str;
        break;
      case ix::WebSocketMessageType::Fragment:
        return;  // reassembled by the library; ignore partial frames
    }
    cb_(out);
  });
  ws_->start();
}

void IxWebSocketTransport::stop() { ws_->stop(); }

bool IxWebSocketTransport::send_text(const std::string& text) {
  return ws_->sendText(text).success;
}

bool IxWebSocketTransport::is_open() const {
  return ws_->getReadyState() == ix::ReadyState::Open;
}

}  // namespace kalshi
