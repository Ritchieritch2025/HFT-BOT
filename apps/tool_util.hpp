#pragma once
// Shared preamble for the one-shot REST inspection tools (account_info,
// account_upgrade, rate_probe): resolve the runtime fail-closed, require the
// credential env vars, and fill the authenticated client Config.

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/limits.hpp"

#include <cstdio>
#include <ctime>
#include <string>

namespace kalshi::tool {

// Fills rt + cfg for an authenticated client. Returns 0 on success, else the
// process exit code (2) after printing the refusal / missing-env error.
inline int preamble(Runtime& rt, Config& cfg) {
  try {
    rt = resolve_runtime();
  } catch (const SafetyViolation& e) {
    std::fprintf(stderr, "refused: %s\n", e.what());
    return 2;
  }
  const std::string key_id = daemon::env_or("KALSHI_API_KEY_ID", "");
  const std::string key_path = daemon::env_or("KALSHI_PRIVATE_KEY_PATH", "");
  if (key_id.empty() || key_path.empty()) {
    std::fprintf(stderr, "set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH\n");
    return 2;
  }
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = rt.rest_base_url;
  return 0;
}

// "permanent" or "expires YYYY-MM-DD" for a tier grant.
inline std::string grant_expiry(const ApiUsageLevelGrant& g) {
  if (!g.expires_ts) return "permanent";
  const std::time_t t = static_cast<std::time_t>(*g.expires_ts);
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%d", std::gmtime(&t));
  return std::string("expires ") + buf;
}

}  // namespace kalshi::tool
