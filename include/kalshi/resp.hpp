#pragma once
//
// Minimal from-scratch RESP2 client (the Redis wire protocol) over one
// persistent TCP socket. Implements exactly what the pipeline needs —
// LPUSH / BRPOP / PUBLISH / SUBSCRIBE / LTRIM / PING — with binary-safe bulk
// strings (the packed wire structs travel as raw bytes). Works against any
// real Redis and against tests/mini_redis.py.
//
// Thread model: one RespClient per thread/role, like the daemons do. A single
// instance is NOT thread-safe. A client that has entered subscribe() only
// receives messages after that (Redis restricts subscribe-mode connections).
//
// On any socket error the connection is closed and an Error returned; callers
// reconnect via connect() (see ensure_connected() in the daemons).

#include <expected>
#include <initializer_list>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace kalshi::resp {

struct Error {
  std::string message;
};

struct Reply {
  enum class Type { Simple, Err, Integer, Bulk, Nil, Array };
  Type type = Type::Nil;
  std::string str;           // Simple / Err / Bulk payload
  long long integer = 0;     // Integer
  std::vector<Reply> array;  // Array elements
};

class RespClient {
 public:
  RespClient(std::string host, int port);
  ~RespClient();
  RespClient(const RespClient&) = delete;
  RespClient& operator=(const RespClient&) = delete;

  std::expected<void, Error> connect();  // no-op if already connected
  void close() noexcept;
  bool connected() const noexcept { return fd_ >= 0; }

  // Sends one command and reads one reply. An -ERR reply from the server is
  // returned as a normal Reply{type=Err}; only transport failures are Error.
  std::expected<Reply, Error> command(std::initializer_list<std::string_view> args);

  std::expected<void, Error> ping();
  std::expected<long long, Error> lpush(std::string_view key, std::string_view value);
  std::expected<long long, Error> publish(std::string_view channel, std::string_view message);
  std::expected<void, Error> ltrim(std::string_view key, long long start, long long stop);
  // Blocks up to timeout_s server-side; nullopt = timed out with no element.
  std::expected<std::optional<std::string>, Error> brpop(std::string_view key, int timeout_s);

  // Subscribe mode.
  std::expected<void, Error> subscribe(std::string_view channel);
  // Next published payload; nullopt on local poll timeout.
  std::expected<std::optional<std::string>, Error> next_message(int timeout_ms);

 private:
  std::expected<void, Error> send_command(std::initializer_list<std::string_view> args);
  std::expected<Reply, Error> read_reply();
  std::expected<std::string_view, Error> read_line();     // one CRLF-terminated line
  std::expected<void, Error> fill(size_t need);           // buffer >= need bytes
  std::expected<void, Error> write_all(const char* data, size_t len);
  std::unexpected<Error> fail(const std::string& what);   // closes fd, wraps errno

  std::string host_;
  int port_;
  int fd_ = -1;
  std::string buf_;    // receive buffer
  size_t consumed_ = 0;  // parsed prefix of buf_
};

}  // namespace kalshi::resp
