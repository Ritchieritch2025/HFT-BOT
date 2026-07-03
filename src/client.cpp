#include "kalshi/client.hpp"

#include <curl/curl.h>
#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/rsa.h>

#include <chrono>
#include <cstring>
#include <fstream>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>

namespace kalshi {

namespace {

std::once_flag g_curl_global_once;

// libcurl callbacks are C stack frames: exceptions must never cross them.
size_t write_body(char* ptr, size_t size, size_t nmemb, void* userdata) noexcept {
  auto* out = static_cast<std::string*>(userdata);
  const size_t n = size * nmemb;
  try {
    out->append(ptr, n);
  } catch (...) {
    return 0;  // out-of-memory: abort the transfer (CURLE_WRITE_ERROR)
  }
  return n;
}

// Captures the server's Date header (ms since epoch) so callers can detect
// local clock skew — the usual cause of otherwise-opaque 401 streaks.
size_t header_date(char* ptr, size_t size, size_t nmemb, void* userdata) noexcept {
  const size_t n = size * nmemb;
  if (n > 6 && strncasecmp(ptr, "date:", 5) == 0) {
    char buf[80];
    size_t len = n - 5;
    const char* v = ptr + 5;
    while (len > 0 && (*v == ' ' || *v == '\t')) { ++v; --len; }
    while (len > 0 && (v[len - 1] == '\r' || v[len - 1] == '\n')) --len;
    if (len < sizeof(buf)) {
      std::memcpy(buf, v, len);
      buf[len] = '\0';
      const time_t t = curl_getdate(buf, nullptr);
      if (t != static_cast<time_t>(-1))
        *static_cast<long long*>(userdata) = static_cast<long long>(t) * 1000;
    }
  }
  return n;
}

const char* method_string(Method m) {
  switch (m) {
    case Method::Get: return "GET";
    case Method::Post: return "POST";
    case Method::Put: return "PUT";
    case Method::Delete: return "DELETE";
  }
  return "GET";
}

std::string openssl_error_message(const char* what) {
  char buf[256] = {0};
  ERR_error_string_n(ERR_peek_last_error(), buf, sizeof(buf));
  ERR_clear_error();
  std::string msg = what;
  msg += ": ";
  msg += buf;
  return msg;
}

std::unexpected<Error> crypto_error(const char* what) {
  return std::unexpected(Error{
      .source = Error::Source::Crypto, .code = 0,
      .message = openssl_error_message(what)});
}

std::unexpected<Error> transport_error(int code, std::string message) {
  return std::unexpected(Error{
      .source = Error::Source::Transport, .code = code,
      .message = std::move(message)});
}

struct MdCtxFree {
  void operator()(EVP_MD_CTX* p) const noexcept { EVP_MD_CTX_free(p); }
};
using MdCtxPtr = std::unique_ptr<EVP_MD_CTX, MdCtxFree>;

struct SlistFree {
  void operator()(curl_slist* p) const noexcept { curl_slist_free_all(p); }
};
using SlistPtr = std::unique_ptr<curl_slist, SlistFree>;

// Restores the handle to a pristine state when a request scope exits, on
// every path. curl_easy_reset keeps live connections, the TLS session cache,
// and the DNS cache — it only clears options, so no pooled handle ever holds
// pointers into a dead stack frame (CURLOPT_ERRORBUFFER's lifetime rule).
struct HandleSanitizer {
  CURL* h;
  ~HandleSanitizer() { curl_easy_reset(h); }
};

EVP_PKEY* load_rsa_private_key(const std::string& pem) {
  BIO* bio = BIO_new_mem_buf(pem.data(), static_cast<int>(pem.size()));
  if (!bio) throw std::runtime_error("KalshiClient: BIO_new_mem_buf failed");
  // The callback refuses passphrase prompting: an encrypted key must fail
  // fast, not hang an unattended process on a hidden terminal prompt.
  EVP_PKEY* key = PEM_read_bio_PrivateKey(
      bio, nullptr, [](char*, int, int, void*) -> int { return -1; }, nullptr);
  BIO_free(bio);
  if (!key) {
    throw std::runtime_error(openssl_error_message(
        "KalshiClient: failed to parse private key PEM "
        "(must be an unencrypted RSA key)"));
  }
  if (!EVP_PKEY_is_a(key, "RSA") && !EVP_PKEY_is_a(key, "RSA-PSS")) {
    EVP_PKEY_free(key);
    throw std::runtime_error("KalshiClient: private key is not an RSA key");
  }
  return key;
}

}  // namespace

// RAII lease of one pooled connection; empty when acquisition timed out.
class KalshiClient::Lease {
 public:
  explicit Lease(KalshiClient& client)
      : client_(client), handle_(client.acquire()) {}
  ~Lease() {
    if (handle_) client_.release(handle_);
  }
  Lease(const Lease&) = delete;
  Lease& operator=(const Lease&) = delete;
  CURL* get() const noexcept { return handle_; }

 private:
  KalshiClient& client_;
  CURL* handle_;
};

KalshiClient::KalshiClient(Config cfg) : cfg_(std::move(cfg)) {
  if (cfg_.api_key_id.empty())
    throw std::runtime_error("KalshiClient: api_key_id is empty");
  if (cfg_.private_key_pem.empty())
    throw std::runtime_error("KalshiClient: private_key_pem is empty");
  if (cfg_.pool_size < 1)
    throw std::runtime_error("KalshiClient: pool_size must be >= 1");

  std::call_once(g_curl_global_once,
                 [] { curl_global_init(CURL_GLOBAL_DEFAULT); });

  key_header_ = "KALSHI-ACCESS-KEY: " + cfg_.api_key_id;  // before any owning state
  pkey_ = load_rsa_private_key(cfg_.private_key_pem);

  try {
    all_handles_.reserve(static_cast<size_t>(cfg_.pool_size));
    for (int i = 0; i < cfg_.pool_size; ++i) {
      CURL* h = curl_easy_init();
      if (!h) throw std::runtime_error("KalshiClient: curl_easy_init failed");
      all_handles_.push_back(h);
    }
    free_handles_ = all_handles_;
  } catch (...) {
    for (CURL* h : all_handles_) curl_easy_cleanup(h);
    EVP_PKEY_free(pkey_);
    throw;
  }
}

KalshiClient::~KalshiClient() {
  for (CURL* h : all_handles_) curl_easy_cleanup(h);
  EVP_PKEY_free(pkey_);
}

CURL* KalshiClient::acquire() {
  std::unique_lock lock(pool_mutex_);
  const bool got = pool_cv_.wait_for(
      lock, std::chrono::milliseconds(cfg_.acquire_timeout_ms),
      [this] { return !free_handles_.empty(); });
  if (!got) return nullptr;
  CURL* h = free_handles_.back();
  free_handles_.pop_back();
  return h;
}

void KalshiClient::release(CURL* handle) noexcept {
  {
    std::lock_guard lock(pool_mutex_);
    free_handles_.push_back(handle);
  }
  // notify_all: the CV is shared by acquire() and warmup(), whose predicates
  // differ — notify_one could wake only a waiter whose predicate is still
  // false and strand another whose predicate just became true.
  pool_cv_.notify_all();
}

std::expected<std::string, Error> KalshiClient::sign(
    std::string_view message) const {
  MdCtxPtr ctx(EVP_MD_CTX_new());
  if (!ctx) return crypto_error("EVP_MD_CTX_new");

  // pctx is owned by ctx; freed with it.
  EVP_PKEY_CTX* pctx = nullptr;
  if (EVP_DigestSignInit(ctx.get(), &pctx, EVP_sha256(), nullptr, pkey_) != 1)
    return crypto_error("EVP_DigestSignInit");
  if (EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING) != 1)
    return crypto_error("set_rsa_padding(PSS)");
  if (EVP_PKEY_CTX_set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST) != 1)
    return crypto_error("set_rsa_pss_saltlen(DIGEST)");
  if (EVP_PKEY_CTX_set_rsa_mgf1_md(pctx, EVP_sha256()) != 1)
    return crypto_error("set_rsa_mgf1_md(SHA-256)");

  const auto* data = reinterpret_cast<const unsigned char*>(message.data());
  size_t sig_len = 0;
  if (EVP_DigestSign(ctx.get(), nullptr, &sig_len, data, message.size()) != 1)
    return crypto_error("EVP_DigestSign(size)");
  std::vector<unsigned char> sig(sig_len);
  if (EVP_DigestSign(ctx.get(), sig.data(), &sig_len, data, message.size()) != 1)
    return crypto_error("EVP_DigestSign");

  // EVP_EncodeBlock writes 4*ceil(n/3) chars plus a trailing NUL.
  std::string b64;
  b64.resize(4 * ((sig_len + 2) / 3) + 1);
  const int b64_len = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(b64.data()),
                                      sig.data(), static_cast<int>(sig_len));
  if (b64_len < 0) return crypto_error("EVP_EncodeBlock");
  b64.resize(static_cast<size_t>(b64_len));
  return b64;
}

std::expected<SignedRequest, Error> KalshiClient::sign_request(
    Method method, std::string_view path, std::string_view json_body) const {
  const auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::system_clock::now().time_since_epoch())
                          .count();
  const std::string timestamp = std::to_string(now_ms);

  // Signed message: timestamp + METHOD + path-with-prefix, without the query
  // string ("1700000000000GET/trade-api/v2/portfolio/balance").
  const std::string_view sig_path = path.substr(0, path.find('?'));
  std::string message;
  message.reserve(timestamp.size() + 8 + cfg_.api_prefix.size() + sig_path.size());
  message += timestamp;
  message += method_string(method);
  message += cfg_.api_prefix;
  message += sig_path;

  auto signature = sign(message);
  if (!signature) return std::unexpected(std::move(signature).error());

  SignedRequest r;
  r.method = method;
  r.url.reserve(cfg_.base_url.size() + cfg_.api_prefix.size() + path.size());
  r.url += cfg_.base_url;
  r.url += cfg_.api_prefix;
  r.url += path;
  r.sig_header = "KALSHI-ACCESS-SIGNATURE: " + *signature;
  r.ts_header = "KALSHI-ACCESS-TIMESTAMP: " + timestamp;
  r.body = json_body;  // owned: a SignedRequest may sit in a queue
  r.signed_ts_ms = static_cast<long long>(now_ms);
  return r;
}

std::expected<Response, Error> KalshiClient::request(Method method,
                                                     std::string_view path,
                                                     std::string_view json_body) {
  // Sign before taking a connection: the ~100-200us RSA-PSS operation must
  // not shrink effective pool capacity under burst.
  auto signed_req = sign_request(method, path, json_body);
  if (!signed_req) return std::unexpected(std::move(signed_req).error());
  return send_signed(*signed_req);
}

std::expected<Response, Error> KalshiClient::send_signed(
    const SignedRequest& req) {
  Lease lease(*this);
  if (!lease.get()) {
    return transport_error(
        0, "connection pool exhausted (no free connection within " +
               std::to_string(cfg_.acquire_timeout_ms) + "ms)");
  }
  return send_request(lease.get(), req, cfg_.http2);
}

KalshiClient::Lane KalshiClient::make_lane() {
  CURL* h = curl_easy_init();
  if (!h) throw std::runtime_error("KalshiClient: curl_easy_init failed (lane)");
  return Lane(*this, h);
}

KalshiClient::Lane::~Lane() {
  if (handle_) curl_easy_cleanup(handle_);
}

std::expected<Response, Error> KalshiClient::Lane::send(
    const SignedRequest& req) {
  // Lanes always attempt HTTP/2 (2TLS = ALPN-negotiated on https, plain
  // HTTP/1.1 on http URLs and on servers that decline h2).
  return client_->send_request(handle_, req, /*use_http2=*/true);
}

std::expected<Response, Error> KalshiClient::Lane::ping() {
  auto req = client_->sign_request(Method::Get, "/exchange/status");
  if (!req) return std::unexpected(std::move(req).error());
  return send(*req);
}

std::expected<Response, Error> KalshiClient::send_request(
    CURL* h, const SignedRequest& req, bool use_http2) {
  const Method method = req.method;
  const std::string_view body = req.body;
  Response resp;
  resp.body.reserve(4096);
  char errbuf[CURL_ERROR_SIZE] = {0};

  // Declared after resp/errbuf: its destructor (curl_easy_reset) runs first
  // on scope exit, so the handle never keeps pointers to these locals.
  HandleSanitizer sanitizer{h};

  curl_easy_reset(h);
  curl_easy_setopt(h, CURLOPT_URL, req.url.c_str());
  curl_easy_setopt(h, CURLOPT_NOSIGNAL, 1L);  // required for multi-threaded use
  curl_easy_setopt(h, CURLOPT_TCP_NODELAY, 1L);
  curl_easy_setopt(h, CURLOPT_TCP_KEEPALIVE, 1L);
  curl_easy_setopt(h, CURLOPT_DNS_CACHE_TIMEOUT, 300L);
#if LIBCURL_VERSION_NUM >= 0x075500  // 7.85.0
  curl_easy_setopt(h, CURLOPT_PROTOCOLS_STR, "https,http");
#else
  curl_easy_setopt(h, CURLOPT_PROTOCOLS, CURLPROTO_HTTPS | CURLPROTO_HTTP);
#endif
#if LIBCURL_VERSION_NUM >= 0x074100  // 7.65.0
  // Never reuse a connection idle beyond this: Kalshi's edge kills idle
  // connections server-side, and a POST fired into a half-closed socket can
  // be transparently retried by curl — a replay hazard on the order path
  // (client_order_id idempotency is the second line of defense).
  curl_easy_setopt(h, CURLOPT_MAXAGE_CONN, cfg_.max_conn_idle_s);
#endif
  curl_easy_setopt(h, CURLOPT_CONNECTTIMEOUT_MS, cfg_.connect_timeout_ms);
  curl_easy_setopt(h, CURLOPT_TIMEOUT_MS, cfg_.request_timeout_ms);
  curl_easy_setopt(h, CURLOPT_HTTP_VERSION,
                   use_http2 ? CURL_HTTP_VERSION_2TLS : CURL_HTTP_VERSION_1_1);
  curl_easy_setopt(h, CURLOPT_WRITEFUNCTION, write_body);
  curl_easy_setopt(h, CURLOPT_WRITEDATA, &resp.body);
  curl_easy_setopt(h, CURLOPT_HEADERFUNCTION, header_date);
  curl_easy_setopt(h, CURLOPT_HEADERDATA, &resp.server_date_ms);
  curl_easy_setopt(h, CURLOPT_ERRORBUFFER, errbuf);
  curl_easy_setopt(h, CURLOPT_USERAGENT, "kalshi-cpp/0.1");
  if (cfg_.verbose) curl_easy_setopt(h, CURLOPT_VERBOSE, 1L);

  switch (method) {
    case Method::Get:
      curl_easy_setopt(h, CURLOPT_HTTPGET, 1L);
      break;
    case Method::Post:
      curl_easy_setopt(h, CURLOPT_POST, 1L);
      break;
    case Method::Put:
      curl_easy_setopt(h, CURLOPT_CUSTOMREQUEST, "PUT");
      break;
    case Method::Delete:
      curl_easy_setopt(h, CURLOPT_CUSTOMREQUEST, "DELETE");
      break;
  }
  if (method != Method::Get) {
    // POSTFIELDS is not copied by curl; req.body owns the bytes and outlives
    // the synchronous perform below.
    curl_easy_setopt(h, CURLOPT_POSTFIELDSIZE, static_cast<long>(body.size()));
    curl_easy_setopt(h, CURLOPT_POSTFIELDS, body.empty() ? "" : body.data());
  }

  SlistPtr headers;
  {
    const char* const lines[] = {
        key_header_.c_str(),
        req.sig_header.c_str(),
        req.ts_header.c_str(),
        "Content-Type: application/json",
        "Accept: application/json",
        // Suppress "Expect: 100-continue" on larger POSTs; it costs an RTT.
        "Expect:",
    };
    curl_slist* list = nullptr;
    for (const char* line : lines) {
      // Append failures must be caught per-node: a mid-chain NULL would
      // orphan earlier nodes and silently start a fresh, auth-less list.
      curl_slist* next = curl_slist_append(list, line);
      if (!next) {
        curl_slist_free_all(list);
        return transport_error(0, "curl_slist_append failed");
      }
      list = next;
    }
    headers.reset(list);
    curl_easy_setopt(h, CURLOPT_HTTPHEADER, list);
  }

  const CURLcode rc = curl_easy_perform(h);
  if (rc != CURLE_OK) {
    return transport_error(
        rc, errbuf[0] != '\0' ? errbuf : curl_easy_strerror(rc));
  }

  curl_easy_getinfo(h, CURLINFO_RESPONSE_CODE, &resp.status);
  curl_off_t total_us = 0;
  curl_easy_getinfo(h, CURLINFO_TOTAL_TIME_T, &total_us);
  resp.total_time_us = static_cast<long long>(total_us);
  return resp;
}

int KalshiClient::warmup() {
  // Take every handle out of the pool, run one round trip on each so its
  // TCP+TLS session is established, then return them. Intended to be called
  // once at startup before other threads start trading.
  std::vector<CURL*> taken;
  {
    std::unique_lock lock(pool_mutex_);
    pool_cv_.wait(lock,
                  [this] { return free_handles_.size() == all_handles_.size(); });
    taken.swap(free_handles_);
  }

  const auto give_back = [&]() noexcept {
    std::lock_guard lock(pool_mutex_);
    for (CURL* h : taken) free_handles_.push_back(h);
  };

  int warmed = 0;
  try {
    for (CURL* h : taken) {
      auto req = sign_request(Method::Get, "/exchange/status");
      if (!req) continue;
      auto r = send_request(h, *req, cfg_.http2);
      if (r && r->ok()) ++warmed;
    }
  } catch (...) {
    give_back();  // never strand the pool, even on bad_alloc
    pool_cv_.notify_all();
    throw;
  }
  give_back();
  pool_cv_.notify_all();
  return warmed;
}

std::string read_file(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("read_file: cannot open " + path);
  std::ostringstream ss;
  ss << in.rdbuf();
  return std::move(ss).str();
}

}  // namespace kalshi
