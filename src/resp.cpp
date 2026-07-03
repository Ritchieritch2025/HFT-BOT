#include "kalshi/resp.hpp"

#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <charconv>
#include <cstring>

namespace kalshi::resp {

namespace {

constexpr int kIoTimeoutSec = 5;  // default socket send/recv timeout

#ifdef MSG_NOSIGNAL
constexpr int kSendFlags = MSG_NOSIGNAL;  // Linux: no SIGPIPE on dead peer
#else
constexpr int kSendFlags = 0;             // macOS uses SO_NOSIGPIPE below
#endif

void set_io_timeout(int fd, int which, int seconds) {
  timeval tv{};
  tv.tv_sec = seconds;
  setsockopt(fd, SOL_SOCKET, which, &tv, sizeof(tv));
}

bool parse_ll(std::string_view s, long long& out) {
  const auto [ptr, ec] = std::from_chars(s.data(), s.data() + s.size(), out);
  return ec == std::errc() && ptr == s.data() + s.size();
}

}  // namespace

RespClient::RespClient(std::string host, int port)
    : host_(std::move(host)), port_(port) {}

RespClient::~RespClient() { close(); }

void RespClient::close() noexcept {
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
  buf_.clear();
  consumed_ = 0;
}

std::unexpected<Error> RespClient::fail(const std::string& what) {
  const int saved_errno = errno;
  std::string msg = what;
  if (saved_errno != 0) {
    msg += ": ";
    msg += std::strerror(saved_errno);
  }
  close();
  return std::unexpected(Error{std::move(msg)});
}

std::expected<void, Error> RespClient::connect() {
  if (fd_ >= 0) return {};

  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* res = nullptr;
  const std::string port_str = std::to_string(port_);
  if (const int rc = ::getaddrinfo(host_.c_str(), port_str.c_str(), &hints, &res);
      rc != 0) {
    return std::unexpected(Error{std::string("getaddrinfo: ") + gai_strerror(rc)});
  }

  int fd = -1;
  for (const addrinfo* ai = res; ai != nullptr; ai = ai->ai_next) {
    fd = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
    if (fd < 0) continue;
    if (::connect(fd, ai->ai_addr, ai->ai_addrlen) == 0) break;
    ::close(fd);
    fd = -1;
  }
  ::freeaddrinfo(res);
  if (fd < 0) return fail("connect " + host_ + ":" + port_str);

  const int one = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
#ifdef SO_NOSIGPIPE
  setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
  set_io_timeout(fd, SO_RCVTIMEO, kIoTimeoutSec);
  set_io_timeout(fd, SO_SNDTIMEO, kIoTimeoutSec);

  fd_ = fd;
  buf_.clear();
  consumed_ = 0;
  return {};
}

std::expected<void, Error> RespClient::write_all(const char* data, size_t len) {
  while (len > 0) {
    const ssize_t n = ::send(fd_, data, len, kSendFlags);
    if (n < 0) {
      if (errno == EINTR) continue;
      return fail("send");
    }
    data += n;
    len -= static_cast<size_t>(n);
  }
  return {};
}

std::expected<void, Error> RespClient::send_command(
    std::initializer_list<std::string_view> args) {
  std::string out;
  size_t est = 16;
  for (const auto a : args) est += a.size() + 16;
  out.reserve(est);

  out += '*';
  out += std::to_string(args.size());
  out += "\r\n";
  for (const auto a : args) {
    out += '$';
    out += std::to_string(a.size());
    out += "\r\n";
    out.append(a.data(), a.size());
    out += "\r\n";
  }
  return write_all(out.data(), out.size());
}

std::expected<void, Error> RespClient::fill(size_t need) {
  while (buf_.size() - consumed_ < need) {
    char tmp[16384];
    const ssize_t n = ::recv(fd_, tmp, sizeof(tmp), 0);
    if (n < 0) {
      if (errno == EINTR) continue;
      return fail("recv");
    }
    if (n == 0) {
      errno = 0;
      return fail("connection closed by peer");
    }
    buf_.append(tmp, static_cast<size_t>(n));
  }
  return {};
}

std::expected<std::string_view, Error> RespClient::read_line() {
  for (;;) {
    const size_t pos = buf_.find("\r\n", consumed_);
    if (pos != std::string::npos) {
      std::string_view line(buf_.data() + consumed_, pos - consumed_);
      consumed_ = pos + 2;
      return line;  // valid only until the next fill()/read
    }
    // Need at least one more byte than we currently hold.
    if (auto f = fill(buf_.size() - consumed_ + 1); !f)
      return std::unexpected(f.error());
  }
}

std::expected<Reply, Error> RespClient::read_reply() {
  auto line = read_line();
  if (!line) return std::unexpected(line.error());
  if (line->empty()) {
    errno = 0;
    return fail("protocol error: empty line");
  }

  const char tag = line->front();
  const std::string_view rest = line->substr(1);

  switch (tag) {
    case '+': {
      Reply r;
      r.type = Reply::Type::Simple;
      r.str.assign(rest);
      return r;
    }
    case '-': {
      Reply r;
      r.type = Reply::Type::Err;
      r.str.assign(rest);
      return r;
    }
    case ':': {
      Reply r;
      r.type = Reply::Type::Integer;
      if (!parse_ll(rest, r.integer)) {
        errno = 0;
        return fail("protocol error: bad integer");
      }
      return r;
    }
    case '$': {
      long long len = 0;
      if (!parse_ll(rest, len) || len > (64LL << 20)) {
        errno = 0;
        return fail("protocol error: bad bulk length");
      }
      if (len < 0) {
        Reply r;
        r.type = Reply::Type::Nil;
        return r;
      }
      // `rest`/`line` die here; fill() may reallocate buf_.
      if (auto f = fill(static_cast<size_t>(len) + 2); !f)
        return std::unexpected(f.error());
      Reply r;
      r.type = Reply::Type::Bulk;
      r.str.assign(buf_.data() + consumed_, static_cast<size_t>(len));
      consumed_ += static_cast<size_t>(len) + 2;  // payload + CRLF
      return r;
    }
    case '*': {
      long long n = 0;
      if (!parse_ll(rest, n) || n > 1'000'000) {
        errno = 0;
        return fail("protocol error: bad array length");
      }
      Reply r;
      if (n < 0) {
        r.type = Reply::Type::Nil;
        return r;
      }
      r.type = Reply::Type::Array;
      r.array.reserve(static_cast<size_t>(n));
      for (long long i = 0; i < n; ++i) {
        auto elem = read_reply();
        if (!elem) return elem;
        r.array.push_back(std::move(*elem));
      }
      return r;
    }
    default:
      errno = 0;
      return fail("protocol error: unknown reply tag");
  }
}

std::expected<Reply, Error> RespClient::command(
    std::initializer_list<std::string_view> args) {
  if (fd_ < 0) {
    if (auto c = connect(); !c) return std::unexpected(c.error());
  }
  if (auto w = send_command(args); !w) return std::unexpected(w.error());
  auto r = read_reply();
  if (r) {  // drop the parsed prefix; keep any pipelined remainder
    if (consumed_ == buf_.size()) buf_.clear();
    else buf_.erase(0, consumed_);
    consumed_ = 0;
  }
  return r;
}

std::expected<void, Error> RespClient::ping() {
  auto r = command({"PING"});
  if (!r) return std::unexpected(r.error());
  if (r->type == Reply::Type::Simple && r->str == "PONG") return {};
  return std::unexpected(Error{"unexpected PING reply: " + r->str});
}

std::expected<long long, Error> RespClient::lpush(std::string_view key,
                                                  std::string_view value) {
  auto r = command({"LPUSH", key, value});
  if (!r) return std::unexpected(r.error());
  if (r->type == Reply::Type::Integer) return r->integer;
  return std::unexpected(Error{"LPUSH error: " + r->str});
}

std::expected<long long, Error> RespClient::publish(std::string_view channel,
                                                    std::string_view message) {
  auto r = command({"PUBLISH", channel, message});
  if (!r) return std::unexpected(r.error());
  if (r->type == Reply::Type::Integer) return r->integer;
  return std::unexpected(Error{"PUBLISH error: " + r->str});
}

std::expected<void, Error> RespClient::ltrim(std::string_view key,
                                             long long start, long long stop) {
  auto r = command({"LTRIM", key, std::to_string(start), std::to_string(stop)});
  if (!r) return std::unexpected(r.error());
  if (r->type == Reply::Type::Simple) return {};
  return std::unexpected(Error{"LTRIM error: " + r->str});
}

std::expected<std::optional<std::string>, Error> RespClient::brpop(
    std::string_view key, int timeout_s) {
  if (fd_ < 0) {
    if (auto c = connect(); !c) return std::unexpected(c.error());
  }
  // The server holds the reply up to timeout_s; the socket timeout must
  // outlast it (0 means block forever on both sides).
  set_io_timeout(fd_, SO_RCVTIMEO, timeout_s == 0 ? 0 : timeout_s + 3);
  auto r = command({"BRPOP", key, std::to_string(timeout_s)});
  if (fd_ >= 0) set_io_timeout(fd_, SO_RCVTIMEO, kIoTimeoutSec);
  if (!r) return std::unexpected(r.error());

  if (r->type == Reply::Type::Nil) return std::nullopt;  // timed out, empty
  if (r->type == Reply::Type::Array && r->array.size() == 2 &&
      r->array[1].type == Reply::Type::Bulk) {
    return std::optional<std::string>(std::move(r->array[1].str));
  }
  return std::unexpected(Error{"unexpected BRPOP reply"});
}

std::expected<void, Error> RespClient::subscribe(std::string_view channel) {
  auto r = command({"SUBSCRIBE", channel});
  if (!r) return std::unexpected(r.error());
  if (r->type == Reply::Type::Array && r->array.size() >= 1 &&
      r->array[0].str == "subscribe") {
    return {};
  }
  return std::unexpected(Error{"unexpected SUBSCRIBE reply"});
}

std::expected<std::optional<std::string>, Error> RespClient::next_message(
    int timeout_ms) {
  if (fd_ < 0) return std::unexpected(Error{"not connected"});

  // Anything already buffered takes priority over polling the socket.
  if (buf_.size() == consumed_) {
    pollfd pfd{.fd = fd_, .events = POLLIN, .revents = 0};
    const int rc = ::poll(&pfd, 1, timeout_ms);
    if (rc < 0) {
      if (errno == EINTR) return std::nullopt;
      return std::unexpected(fail("poll").error());
    }
    if (rc == 0) return std::nullopt;  // timeout
  }

  auto r = read_reply();
  if (r) {
    if (consumed_ == buf_.size()) buf_.clear();
    else buf_.erase(0, consumed_);
    consumed_ = 0;
  }
  if (!r) return std::unexpected(r.error());

  // ["message", channel, payload]; anything else (e.g. subscribe acks) is skipped.
  if (r->type == Reply::Type::Array && r->array.size() == 3 &&
      r->array[0].str == "message" && r->array[2].type == Reply::Type::Bulk) {
    return std::optional<std::string>(std::move(r->array[2].str));
  }
  return std::nullopt;
}

}  // namespace kalshi::resp
