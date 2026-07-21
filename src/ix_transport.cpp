#include "kalshi/ix_transport.hpp"

#include "ixwebsocket/IXSocketTLSOptions.h"
#include "ixwebsocket/IXWebSocket.h"
#include "ixwebsocket/IXWebSocketHttpHeaders.h"

#include <cstdlib>
#include <filesystem>

namespace kalshi {

namespace {
// The statically-linked OpenSSL has no usable default trust store on macOS, so
// TLS cert verification fails ("certificate verify failed") unless we point it
// at a CA bundle. Honor SSL_CERT_FILE, else probe the common system locations.
// Never disables verification — that would break the TLS safety invariant.
std::string find_ca_bundle() {
  if (const char* env = std::getenv("SSL_CERT_FILE"); env && *env &&
      std::filesystem::exists(env))
    return env;
  for (const char* p : {
           "/etc/ssl/cert.pem",                        // macOS system OpenSSL compat
           "/etc/ssl/certs/ca-certificates.crt",       // Debian/Ubuntu
           "/etc/pki/tls/certs/ca-bundle.crt",         // RHEL/Fedora
           "/opt/homebrew/etc/openssl@3/cert.pem",     // Homebrew (Apple silicon)
           "/usr/local/etc/openssl@3/cert.pem",        // Homebrew (Intel)
       }) {
    std::error_code ec;
    if (std::filesystem::exists(p, ec)) return p;
  }
  return {};
}
}  // namespace

IxWebSocketTransport::IxWebSocketTransport() : ws_(std::make_unique<ix::WebSocket>()) {
  ws_->disablePerMessageDeflate();  // deflate off at runtime (compiled out too)
  // Library owns reconnection with capped exponential backoff + jitter.
  ws_->enableAutomaticReconnection();
  ws_->setMinWaitBetweenReconnectionRetries(1000);    // 1s
  ws_->setMaxWaitBetweenReconnectionRetries(30000);   // 30s cap
  ws_->setPingInterval(0);  // Kalshi pings us; we only pong (auto) + watch silence

  // TLS trust: verify the server cert against a real CA bundle (fail closed if
  // none is found — a missing bundle surfaces as a connect error, never as a
  // silently-insecure connection).
  ix::SocketTLSOptions tls;
  tls.caFile = find_ca_bundle();
  ws_->setTLSOptions(tls);
}

IxWebSocketTransport::~IxWebSocketTransport() {
  if (ws_) ws_->stop();
}

void IxWebSocketTransport::set_url(const std::string& url) { ws_->setUrl(url); }

void IxWebSocketTransport::set_headers(const WsHeaders& headers) {
  ix::WebSocketHttpHeaders h;
  for (const auto& [k, v] : headers) h[k] = v;
  // Suppress ixwebsocket's auto-injected browser-style Origin header: Kalshi's
  // gateway 403s the WS upgrade when an Origin is present (empty value + the
  // handshake patch => the header is omitted entirely).
  if (h.find("Origin") == h.end()) h["Origin"] = "";
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
