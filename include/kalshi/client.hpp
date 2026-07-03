#pragma once
//
// KalshiClient — minimal C++23 REST client for the Kalshi Trade API v2.
//
// Transport : libcurl easy handles, pooled and reused => persistent TLS
//             connections with keep-alive (one connection per pool slot).
// Auth      : RSA-PSS (SHA-256, MGF1-SHA256, salt = digest length) request
//             signatures via OpenSSL 3.x, per Kalshi API-key spec.
// JSON      : responses are returned as raw bytes; parse with simdjson
//             (vendored in third_party/simdjson) or any parser you prefer.
//
// Thread safety: KalshiClient::request() may be called concurrently from any
// number of threads. Each call leases one pooled connection for its duration;
// callers block (FIFO-ish) when all connections are busy. Construct once,
// share by reference, destroy only after all in-flight requests returned.
//
// This header deliberately includes neither <curl/curl.h> nor OpenSSL
// headers; the typedefs below match the public definitions in those headers.

#include <version>
#if !defined(__cpp_lib_expected) || __cpp_lib_expected < 202202L
#error "KalshiClient requires C++23 std::expected (GCC 12+ / Clang 16+ with -std=c++23)"
#endif

#include <condition_variable>
#include <cstdint>
#include <expected>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

typedef void CURL;                        // matches <curl/curl.h>
typedef struct evp_pkey_st EVP_PKEY;      // matches <openssl/types.h>

namespace kalshi {

enum class Method : std::uint8_t { Get, Post, Put, Delete };

struct Error {
  enum class Source : std::uint8_t {
    Transport,  // libcurl failure: code holds the CURLcode
    Crypto,     // OpenSSL failure while signing: code is 0
  };
  Source source = Source::Transport;
  int code = 0;
  std::string message;
};

struct Response {
  long status = 0;              // HTTP status code
  std::string body;             // raw response payload (JSON)
  long long total_time_us = 0;  // request round-trip time measured by libcurl
  long long server_date_ms = 0; // server Date header (ms since epoch, 0 if
                                // absent) — compare with local time to spot
                                // the clock skew behind 401 streaks

  [[nodiscard]] bool ok() const noexcept { return status >= 200 && status < 300; }
};

struct Config {
  std::string api_key_id;       // Kalshi API key id (UUID), sent as KALSHI-ACCESS-KEY
  std::string private_key_pem;  // RSA private key, PEM text (PKCS#1 or PKCS#8)

  std::string base_url = "https://api.elections.kalshi.com";
  std::string api_prefix = "/trade-api/v2";

  int pool_size = 4;                // persistent connections == max in-flight requests
  long connect_timeout_ms = 2000;
  long request_timeout_ms = 5000;   // total per-request budget, including connect
  long acquire_timeout_ms = 1000;   // max wait for a free pooled connection;
                                    // on expiry request() fails fast (Error)
                                    // instead of stalling the trading loop
  long max_conn_idle_s = 30;        // never reuse a connection idle longer than
                                    // this (avoids POSTing into sockets the
                                    // server has half-closed)
  bool http2 = false;               // false => HTTP/1.1, one dedicated conn per slot
  bool verbose = false;             // libcurl debug output on stderr
};

class KalshiClient {
 public:
  // Parses the private key and creates the connection pool.
  // Throws std::runtime_error on invalid configuration, bad key, or curl
  // initialization failure. No network I/O happens here; see warmup().
  explicit KalshiClient(Config cfg);
  ~KalshiClient();

  KalshiClient(const KalshiClient&) = delete;
  KalshiClient& operator=(const KalshiClient&) = delete;

  // Signs and sends one request. `path` is relative to Config::api_prefix,
  // e.g. "/portfolio/balance" or "/markets?limit=100". The query string is
  // sent but excluded from the signature, per the Kalshi spec. `json_body`
  // is sent verbatim for Post/Put/Delete and must stay valid until this
  // call returns (it is not copied).
  //
  // Transport and signing failures return Error; HTTP-level errors (4xx/5xx)
  // return a Response — check Response::ok() / status and parse body.
  std::expected<Response, Error> request(Method method, std::string_view path,
                                         std::string_view json_body = {});

  // Establishes TLS on every pooled connection by requesting
  // GET {api_prefix}/exchange/status on each. Call once at startup, before
  // issuing requests from other threads. Returns how many connections
  // completed the round trip.
  int warmup();

  // Signs an exact message with RSA-PSS(SHA-256) and returns base64.
  // request() composes and signs messages itself; this is exposed for tests.
  std::expected<std::string, Error> sign(std::string_view message) const;

  const Config& config() const noexcept { return cfg_; }

 private:
  class Lease;

  // URL + auth headers, composed and signed before a connection is leased.
  struct Prepared {
    std::string url;
    std::string sig_header;
    std::string ts_header;
  };

  CURL* acquire();  // nullptr when acquire_timeout_ms expires
  void release(CURL* handle) noexcept;
  std::expected<Prepared, Error> prepare(Method method,
                                         std::string_view path) const;
  std::expected<Response, Error> send_request(CURL* handle, Method method,
                                              const Prepared& prepared,
                                              std::string_view body);

  Config cfg_;
  EVP_PKEY* pkey_ = nullptr;
  std::string key_header_;  // "KALSHI-ACCESS-KEY: <id>", built once

  std::mutex pool_mutex_;
  std::condition_variable pool_cv_;
  std::vector<CURL*> all_handles_;
  std::vector<CURL*> free_handles_;
};

// Reads a whole file (e.g. the PEM key). Throws std::runtime_error on failure.
std::string read_file(const std::string& path);

}  // namespace kalshi
