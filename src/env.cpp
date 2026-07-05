#include "kalshi/env.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdio>
#include <cstdlib>

namespace kalshi {

namespace {

std::string getenv_or(const char* key, const char* fallback) {
  const char* v = std::getenv(key);
  return v ? std::string(v) : std::string(fallback);
}

std::string lower(std::string s) {
  std::transform(s.begin(), s.end(), s.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  return s;
}

bool truthy(const char* key) {
  const char* v = std::getenv(key);
  if (!v) return false;
  const std::string s = lower(v);
  return s == "1" || s == "true" || s == "yes" || s == "on";
}

// Split "scheme://host[:port][/path]" into scheme + host (no port, no path).
struct UrlParts {
  std::string scheme;
  std::string host;
  bool ok = false;
};
UrlParts parse_url(const std::string& url) {
  UrlParts p;
  const auto sep = url.find("://");
  if (sep == std::string::npos) return p;
  p.scheme = lower(url.substr(0, sep));
  std::string rest = url.substr(sep + 3);
  const auto end = rest.find_first_of(":/");
  p.host = lower(end == std::string::npos ? rest : rest.substr(0, end));
  p.ok = !p.scheme.empty() && !p.host.empty();
  return p;
}

bool host_in(const std::string& host, std::initializer_list<const char*> allow) {
  return std::any_of(allow.begin(), allow.end(),
                     [&](const char* a) { return host == a; });
}

std::string default_base_url(Env env) {
  switch (env) {
    case Env::LocalMock:
      return "http://127.0.0.1:" + getenv_or("KALSHI_MOCK_PORT", "18099");
    case Env::Demo:
      return "https://external-api.demo.kalshi.co";
    case Env::Prod:
      // Canonical prod host per Kalshi api_environments (PLAN_TOKEN_RULES F1).
      // api.elections.kalshi.com is a compatibility host, kept in the allowlist
      // (same signature scheme) but no longer the default.
      return "https://external-api.kalshi.com";
  }
  return "http://127.0.0.1:18099";
}

// WS endpoints (docs/kalshi_ws_protocol.md). The WS host is a SEPARATE host from
// REST (external-api-ws.* vs api.elections/external-api.*), so it needs its own
// allowlist — a prod collector must never open the demo WS and vice-versa.
std::string default_ws_url(Env env) {
  switch (env) {
    case Env::LocalMock:
      return "ws://127.0.0.1:" + getenv_or("KALSHI_MOCK_WS_PORT", "18200") +
             "/trade-api/ws/v2";
    case Env::Demo:
      return "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2";
    case Env::Prod:
      return "wss://external-api-ws.kalshi.com/trade-api/ws/v2";
  }
  return "ws://127.0.0.1:18200/trade-api/ws/v2";
}

// Same fail-closed shape as validate_base_url, for the WS URL. TLS rule (plain
// ws:// only for localmock localhost) is never bypassable.
void validate_ws_url(Env env, Mode mode, const std::string& url) {
  const UrlParts u = parse_url(url);
  if (!u.ok)
    throw SafetyViolation("KALSHI_WS_URL is not a valid scheme://host URL: " + url);

  const bool is_localhost = host_in(u.host, {"localhost", "127.0.0.1"});
  if (u.scheme != "wss") {
    if (!(env == Env::LocalMock && is_localhost))
      throw SafetyViolation("non-TLS WS URL '" + url + "' allowed only for local_mock localhost");
  }

  if (truthy("KALSHI_HOST_UNSAFE_OVERRIDE")) {
    if (mode == Mode::Live)
      throw SafetyViolation("KALSHI_HOST_UNSAFE_OVERRIDE is refused in live mode");
    return;  // allowlist skipped, TLS already checked
  }

  bool ok = false;
  switch (env) {
    case Env::LocalMock: ok = is_localhost; break;
    case Env::Demo:      ok = host_in(u.host, {"external-api-ws.demo.kalshi.co"}); break;
    case Env::Prod:      ok = host_in(u.host, {"external-api-ws.kalshi.com"}); break;
  }
  if (!ok)
    throw SafetyViolation("WS host '" + u.host + "' is not permitted for env=" +
                          to_string(env) +
                          " (set KALSHI_HOST_UNSAFE_OVERRIDE=1 to bypass — refused in live)");
}

// Validate base_url against env. Throws SafetyViolation on mismatch. The TLS
// rule (non-https only for localmock localhost) is enforced even under the
// unsafe host override.
void validate_base_url(Env env, Mode mode, const std::string& url) {
  const UrlParts u = parse_url(url);
  if (!u.ok)
    throw SafetyViolation("KALSHI_BASE_URL is not a valid scheme://host URL: " + url);

  const bool is_localhost = host_in(u.host, {"localhost", "127.0.0.1"});

  // TLS rule (never bypassable): plain http only for localmock localhost.
  if (u.scheme != "https") {
    if (!(env == Env::LocalMock && is_localhost))
      throw SafetyViolation("non-TLS URL '" + url + "' allowed only for local_mock localhost");
  }

  const bool override_allowed = truthy("KALSHI_HOST_UNSAFE_OVERRIDE");
  if (override_allowed) {
    if (mode == Mode::Live)
      throw SafetyViolation("KALSHI_HOST_UNSAFE_OVERRIDE is refused in live mode");
    std::fprintf(stderr,
                 "[env] WARNING: KALSHI_HOST_UNSAFE_OVERRIDE set — host allowlist "
                 "skipped for env=%s host=%s (TLS rule still enforced)\n",
                 to_string(env), u.host.c_str());
    return;  // allowlist skipped, TLS already checked
  }

  bool ok = false;
  switch (env) {
    case Env::LocalMock:
      ok = is_localhost;  // only localhost
      break;
    case Env::Demo:
      ok = host_in(u.host, {"external-api.demo.kalshi.co"});
      break;
    case Env::Prod:
      // Canonical first; api.elections.kalshi.com kept as a compatibility host.
      ok = host_in(u.host, {"external-api.kalshi.com", "api.elections.kalshi.com"});
      break;
  }
  if (!ok)
    throw SafetyViolation("host '" + u.host + "' is not permitted for env=" +
                          to_string(env) +
                          " (set KALSHI_HOST_UNSAFE_OVERRIDE=1 to bypass — refused in live)");
}

}  // namespace

Runtime resolve_runtime() {
  Runtime rt;

  // --- env ---
  const std::string env_s = lower(getenv_or("KALSHI_ENV", "local_mock"));
  if (env_s == "local_mock" || env_s == "localmock" || env_s == "local") {
    rt.env = Env::LocalMock;
  } else if (env_s == "demo") {
    rt.env = Env::Demo;
  } else if (env_s == "prod" || env_s == "production") {
    if (!truthy("KALSHI_ALLOW_PROD"))
      throw SafetyViolation("KALSHI_ENV=prod requires KALSHI_ALLOW_PROD=1 (fail closed)");
    rt.env = Env::Prod;
  } else {
    throw SafetyViolation("unknown KALSHI_ENV '" + env_s +
                          "' (expected local_mock|demo|prod) — fail closed");
  }

  // --- mode ---
  const std::string mode_s = lower(getenv_or("KALSHI_MODE", "data_collect"));
  if (mode_s == "data_collect" || mode_s == "datacollect" || mode_s == "collect") {
    rt.mode = Mode::DataCollect;
  } else if (mode_s == "shadow") {
    rt.mode = Mode::Shadow;
  } else if (mode_s == "live") {
    if (!truthy("KALSHI_ALLOW_LIVE"))
      throw SafetyViolation("KALSHI_MODE=live requires KALSHI_ALLOW_LIVE=1 (fail closed)");
    if (rt.env == Env::LocalMock)
      throw SafetyViolation("live mode is not permitted with env=local_mock");
    rt.mode = Mode::Live;
  } else {
    throw SafetyViolation("unknown KALSHI_MODE '" + mode_s +
                          "' (expected data_collect|shadow|live) — fail closed");
  }

  // --- flags ---
  rt.read_only = rt.mode != Mode::Live;
  rt.shadow = rt.mode == Mode::Shadow;
  rt.orders_enabled = rt.mode == Mode::Live && truthy("KALSHI_ALLOW_LIVE") &&
                      (rt.env == Env::Demo || rt.env == Env::Prod);

  // --- base url + cross-validation ---
  const char* override_url = std::getenv("KALSHI_BASE_URL");
  rt.rest_base_url = override_url ? std::string(override_url) : default_base_url(rt.env);
  validate_base_url(rt.env, rt.mode, rt.rest_base_url);

  // --- ws (used by the Phase 8 shadow-smoke harness) ---
  rt.ws_sign_path = getenv_or("KALSHI_WS_SIGN_PATH", "/trade-api/ws/v2");
  const char* ws_override = std::getenv("KALSHI_WS_URL");
  rt.ws_url = ws_override ? std::string(ws_override) : default_ws_url(rt.env);
  validate_ws_url(rt.env, rt.mode, rt.ws_url);

  return rt;
}

std::string describe(const Runtime& rt) {
  std::string s = "env=";
  s += to_string(rt.env);
  s += " mode=";
  s += to_string(rt.mode);
  s += " base=";
  s += rt.rest_base_url;  // host only, never a secret
  s += rt.read_only ? " read_only" : " WRITABLE";
  s += rt.orders_enabled ? " orders_enabled" : " orders_blocked";
  return s;
}

}  // namespace kalshi
