// panic — the standalone kill switch (W-K2, PLAN_RISK_KILLSWITCH §3; S3).
//
// Sequence (GUARDRAILS S3, verbatim contract):
//   1. CANCEL-ALL resting orders
//   2. VERIFY ZERO resting (re-enumerate until empty; incomplete enumeration
//      is NEVER "clear" — same fail-closed rule as account_view's primitive)
//   3. REPRICE-CROSS LIQUIDATION ROUNDS for residual positions
//   4. REPORT (machine-parseable verdict line + exit code)
//
// STANDALONE BY CONSTRUCTION: no strategy headers, no ring, no Redis, no
// DuckDB — only the proven client/env/wire layers. It must work when
// everything else is on fire.
//
// DRY-RUN IS THE DEFAULT: enumerate + print the full action plan (every
// cancel, every liquidation order with price, client_order_id, IOC) and
// transmit NOTHING mutating. --execute performs it, gated:
//   env=local_mock + localhost  -> mock drill (how the tests exercise the
//                                  real execute code path; same class as
//                                  test_integration's localhost POSTs)
//   env=prod                    -> require_orders_allowed(): live mode +
//                                  KALSHI_ALLOW_LIVE, i.e. the operator has
//                                  explicitly armed it this session (S1).
//                                  Registered live_order = console-forbidden.
//
// OPERATOR RULING (2026-07-10, E2, recorded in PLAN_RISK_KILLSWITCH W-K2):
// panic liquidation orders CROSS (taker) — certainty over price, taker fees
// are an accepted panic cost; every panic order carries dead-man expiry.
// Implementation of the dead-man clause: liquidation orders are
// time_in_force=immediate_or_cancel — expiry is IMMEDIATE, the strongest
// dead-man (an IOC can never become an orphaned resting order, audit N4).
// They also set reduce_only=true (position-capped by the exchange — panic
// can shrink risk, never create it, Q8). If a future panic version ever
// uses a resting order type, expiration_time becomes MANDATORY here.
// All other (strategic) exits stay post-only passive per MM_ROADMAP 1.5B —
// that red line is untouched by this tool.
//
// client_order_id idempotency (design contract #9): every order intent gets
// a DETERMINISTIC id from (action, side, count, price, ticker, seq) with the
// run-stable timestamp captured ONCE at startup — a retry after ack loss
// reuses the SAME id, so a duplicate can fill at most once. Cancel retries
// reuse the same DELETE (idempotent by order_id); "already canceled" /
// "not found" on retry counts as success (exchange is truth, S2).
//
// Field names verified against docs/vendor/kalshi/latest/openapi.yaml
// (CreateOrderV2Request, Order, Market) — E4 discipline, never memory.
//
//   panic [--execute] [--rounds N] [--wait-ms MS] [--json work/panic_report.json]
//
// Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH (+ the KALSHI_ENV family
// resolved by the safety layer). Exit 0 = flat + zero resting, verified.
// Any other outcome exits nonzero with a loud partial-failure report (D2).

#include "daemon_util.hpp"
#include "kalshi/client.hpp"
#include "kalshi/env.hpp"
#include "kalshi/wire.hpp"
#include "simdjson.h"

#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

using namespace kalshi;

namespace {

constexpr int kMaxPages = 50;          // enumeration cap; hitting it = FAIL
constexpr int kVerifyAttempts = 10;    // zero-resting re-poll budget
constexpr std::uint8_t kPanicStrategyId = 29;  // client_order_id namespace
                                               // (top of the 0..29 range,
                                               // reserved for panic)

struct RestingOrder {
  std::string order_id;
  std::string ticker;
  std::string side;        // BookSide: bid | ask
  std::string price;       // fixed-point string, kept verbatim
  std::string remaining;   // fixed-point count, kept verbatim
};

struct Position {
  std::string ticker;
  std::string position_fp;  // verbatim fixed-point count; sign = direction
  bool is_long = false;     // position_fp > 0  (long YES)
};

struct Report {
  int orders_seen = 0, cancels_ok = 0, cancels_failed = 0, cancel_retries = 0;
  int positions_seen = 0, liq_orders_sent = 0, liq_retries = 0;
  int rounds_used = 0;
  bool zero_resting_verified = false;
  bool flat_verified = false;
  std::vector<std::string> failures;
};

// ── tiny helpers ─────────────────────────────────────────────────────────

bool fp_is_zero(std::string_view s) {
  for (char c : s)
    if (c >= '1' && c <= '9') return false;
  return !s.empty();
}

bool fp_is_negative(std::string_view s) { return !s.empty() && s[0] == '-'; }

// fixed-point dollar string -> integer centicents (E4), digits only; -1 on
// any malformed input (fail-closed: caller skips + records).
long fp_to_e4(std::string_view s) {
  bool neg = false; size_t i = 0;
  if (i < s.size() && (s[i] == '-')) { neg = true; ++i; }
  long ip = 0; bool any = false;
  for (; i < s.size() && s[i] != '.'; ++i) {
    if (s[i] < '0' || s[i] > '9') return -1;
    ip = ip * 10 + (s[i] - '0'); any = true;
  }
  long fp = 0; int fd = 0;
  if (i < s.size() && s[i] == '.') {
    for (++i; i < s.size(); ++i) {
      if (s[i] < '0' || s[i] > '9') return -1;
      if (fd < 4) { fp = fp * 10 + (s[i] - '0'); ++fd; }
      else if (s[i] != '0') return -1;   // sub-E4 precision would be lost
    }
  }
  if (!any) return -1;
  while (fd < 4) { fp *= 10; ++fd; }
  long v = ip * 10000 + fp;
  return neg ? -v : v;
}

std::string e4_to_fp(long e4) {        // E4 -> "D.DDDD" (prices are >= 0)
  char buf[32];
  std::snprintf(buf, sizeof(buf), "%ld.%04ld", e4 / 10000, e4 % 10000);
  return buf;
}

}  // namespace

// ── enumeration (same fail-closed rules as tools/account_view.py) ────────

static bool list_resting(KalshiClient& client, simdjson::ondemand::parser& parser,
                         std::vector<RestingOrder>& out, Report& rep) {
  out.clear();
  std::string cursor;
  for (int page = 0; page < kMaxPages; ++page) {
    std::string path = "/portfolio/orders?status=resting&limit=200";
    if (!cursor.empty()) path += "&cursor=" + cursor;
    auto r = client.request(Method::Get, path);
    if (!r || !r->ok()) {
      rep.failures.push_back("enumerate resting: HTTP/transport failure");
      return false;
    }
    try {
      simdjson::padded_string j(r->body);
      auto doc = parser.iterate(j);
      for (auto o : doc["orders"].get_array()) {
        RestingOrder ro;
        std::string_view v;
        if (o["status"].get(v) != simdjson::SUCCESS || v != "resting") continue;
        if (o["order_id"].get(v) != simdjson::SUCCESS) continue;
        ro.order_id = std::string(v);
        if (o["ticker"].get(v) == simdjson::SUCCESS) ro.ticker = std::string(v);
        if (o["side"].get(v) == simdjson::SUCCESS) ro.side = std::string(v);
        if (o["yes_price_dollars"].get(v) == simdjson::SUCCESS) ro.price = std::string(v);
        if (o["remaining_count_fp"].get(v) == simdjson::SUCCESS) ro.remaining = std::string(v);
        out.push_back(std::move(ro));
      }
      std::string_view cv;
      auto doc2 = parser.iterate(j);   // re-iterate for cursor (ondemand is single-pass)
      cursor = (doc2["cursor"].get(cv) == simdjson::SUCCESS) ? std::string(cv) : "";
    } catch (...) {
      rep.failures.push_back("enumerate resting: malformed body");
      return false;
    }
    if (cursor.empty()) return true;
  }
  rep.failures.push_back("enumerate resting: pagination exceeded cap — INCOMPLETE");
  return false;  // truncated enumeration is never trusted (account_view B1 rule)
}

static bool list_positions(KalshiClient& client, simdjson::ondemand::parser& parser,
                           std::vector<Position>& out, Report& rep) {
  out.clear();
  std::string cursor;
  for (int page = 0; page < kMaxPages; ++page) {
    std::string path = "/portfolio/positions?limit=200";
    if (!cursor.empty()) path += "&cursor=" + cursor;
    auto r = client.request(Method::Get, path);
    if (!r || !r->ok()) {
      rep.failures.push_back("enumerate positions: HTTP/transport failure");
      return false;
    }
    try {
      simdjson::padded_string j(r->body);
      auto doc = parser.iterate(j);
      for (auto m : doc["market_positions"].get_array()) {
        Position p;
        std::string_view v;
        if (m["ticker"].get(v) != simdjson::SUCCESS) continue;
        p.ticker = std::string(v);
        if (m["position_fp"].get(v) != simdjson::SUCCESS) continue;
        p.position_fp = std::string(v);
        if (fp_is_zero(p.position_fp)) continue;
        p.is_long = !fp_is_negative(p.position_fp);
        out.push_back(std::move(p));
      }
      std::string_view cv;
      auto doc2 = parser.iterate(j);
      cursor = (doc2["cursor"].get(cv) == simdjson::SUCCESS) ? std::string(cv) : "";
    } catch (...) {
      rep.failures.push_back("enumerate positions: malformed body");
      return false;
    }
    if (cursor.empty()) return true;
  }
  rep.failures.push_back("enumerate positions: pagination exceeded cap — INCOMPLETE");
  return false;
}

// best bid/ask (E4) for crossing prices; false = market unreadable
static bool market_touch(KalshiClient& client, simdjson::ondemand::parser& parser,
                         const std::string& ticker, long& bid_e4, long& ask_e4) {
  auto r = client.request(Method::Get, "/markets/" + ticker);
  if (!r || !r->ok()) return false;
  try {
    simdjson::padded_string j(r->body);
    auto doc = parser.iterate(j);
    std::string_view b, a;
    if (doc["market"]["yes_bid_dollars"].get(b) != simdjson::SUCCESS) return false;
    if (doc["market"]["yes_ask_dollars"].get(a) != simdjson::SUCCESS) return false;
    bid_e4 = fp_to_e4(b);
    ask_e4 = fp_to_e4(a);
    return bid_e4 >= 0 && ask_e4 >= 0;
  } catch (...) { return false; }
}

// deterministic, retry-stable client_order_id (contract #9)
static std::string liq_client_order_id(const Position& pos, long price_e4,
                                       std::uint64_t run_ns, std::uint64_t seq) {
  wire::ExecPayload p;
  p.action = pos.is_long ? wire::kActionSell : wire::kActionBuy;
  p.side = wire::kSideYes;
  p.order_type = wire::kTypeLimit;
  p.count = 1;                                   // identity only; count rides the body
  p.price_cents = static_cast<std::int32_t>(price_e4 / 100);
  p.strategy_id = kPanicStrategyId;
  p.seq = seq;
  p.ts_ns = run_ns;                              // run-stable: retries reuse the id
  p.set_ticker(pos.ticker);
  return wire::client_order_id(p);
}

// one liquidation order body (CreateOrderV2Request, openapi-verified fields)
static std::string liq_body(const Position& pos, long price_e4,
                            const std::string& coid) {
  std::string count = pos.position_fp;
  if (fp_is_negative(count)) count = count.substr(1);   // magnitude
  std::string b;
  b.reserve(320);
  b += R"({"ticker":")";  b += pos.ticker;
  // exit long YES = sell YES = book "ask"; exit short YES = buy YES = "bid"
  b += R"(","side":")";   b += pos.is_long ? "ask" : "bid";
  b += R"(","count":")";  b += count;
  b += R"(","price":")";  b += e4_to_fp(price_e4);
  b += R"(","time_in_force":"immediate_or_cancel")";   // dead-man: expiry NOW
  b += R"(,"reduce_only":true)";                       // never creates risk (Q8)
  b += R"(,"self_trade_prevention_type":"taker_at_cross")";
  b += R"(,"client_order_id":")"; b += coid; b += R"("})";
  return b;
}

int main(int argc, char** argv) {
  bool execute = false;
  int rounds = 3, wait_ms = 1500;
  const char* json_path = nullptr;
  for (int i = 1; i < argc; ++i) {
    if (!std::strcmp(argv[i], "--execute")) execute = true;
    else if (!std::strcmp(argv[i], "--rounds") && i + 1 < argc) rounds = std::atoi(argv[++i]);
    else if (!std::strcmp(argv[i], "--wait-ms") && i + 1 < argc) wait_ms = std::atoi(argv[++i]);
    else if (!std::strcmp(argv[i], "--json") && i + 1 < argc) json_path = argv[++i];
    else { std::fprintf(stderr, "usage: panic [--execute] [--rounds N] [--wait-ms MS] [--json PATH]\n"); return 2; }
  }
  if (rounds < 1 || rounds > 10) { std::fprintf(stderr, "rounds must be 1..10\n"); return 2; }

  const char* key_id = std::getenv("KALSHI_API_KEY_ID");
  const char* key_path = std::getenv("KALSHI_PRIVATE_KEY_PATH");
  if (!key_id || !key_path) {
    std::fprintf(stderr, "PANIC ABORT: KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set\n");
    return 2;
  }

  Runtime rt;
  try {
    rt = resolve_runtime();
  } catch (const SafetyViolation& e) {
    std::fprintf(stderr, "PANIC ABORT (env): %s\n", e.what());
    return 2;
  }
  if (execute) {
    // Mock drill on localhost is the rehearsal path (tests). Anything else
    // must pass the single live choke point (S1: operator-armed session).
    if (rt.env != Env::LocalMock) {
      try {
        require_orders_allowed(rt);
      } catch (const SafetyViolation& e) {
        std::fprintf(stderr, "PANIC ABORT (not armed): %s\n", e.what());
        return 2;
      }
    }
  }
  std::printf("panic: %s | env=%s | rounds=%d\n",
              execute ? "EXECUTE" : "DRY-RUN (plan only, nothing transmitted)",
              to_string(rt.env), rounds);

  Config cfg;
  cfg.api_key_id = key_id;
  cfg.private_key_pem = read_file(key_path);
  cfg.base_url = rt.rest_base_url;
  cfg.pool_size = 1;
  KalshiClient client(std::move(cfg));
  simdjson::ondemand::parser parser;
  Report rep;
  const std::uint64_t run_ns = daemon::now_ns();   // run-stable id timestamp

  // ── 1. cancel-all ──────────────────────────────────────────────────────
  std::vector<RestingOrder> resting;
  if (!list_resting(client, parser, resting, rep)) {
    std::fprintf(stderr, "PANIC FAIL: cannot enumerate resting orders (%s)\n",
                 rep.failures.back().c_str());
    return 1;
  }
  rep.orders_seen = static_cast<int>(resting.size());
  for (const auto& o : resting) {
    std::printf("  cancel %-14s %-40s %s yes@%s rem=%s\n",
                o.order_id.substr(0, 13).c_str(), o.ticker.c_str(),
                o.side.c_str(), o.price.c_str(), o.remaining.c_str());
    if (!execute) continue;
    const std::string path = "/portfolio/events/orders/" + o.order_id;
    bool ok = false;
    for (int attempt = 0; attempt < 3 && !ok; ++attempt) {
      if (attempt) ++rep.cancel_retries;
      auto r = client.request(Method::Delete, path);   // idempotent by order_id
      if (r && r->ok()) { ok = true; break; }
      if (r && (r->status == 404 || r->status == 400)) {
        // already canceled / already executed — the exchange is truth (S2);
        // verification below is what certifies "zero resting", not this code.
        ok = true; break;
      }
      // transport error or 5xx: retry the SAME request
    }
    if (ok) ++rep.cancels_ok;
    else {
      ++rep.cancels_failed;
      rep.failures.push_back("cancel refused/unreachable: " + o.order_id +
                             " (" + o.ticker + ")");
    }
  }

  // ── 2. verify zero resting ─────────────────────────────────────────────
  if (execute) {
    for (int i = 0; i < kVerifyAttempts; ++i) {
      if (!list_resting(client, parser, resting, rep)) break;  // fail-closed below
      if (resting.empty()) { rep.zero_resting_verified = true; break; }
      std::this_thread::sleep_for(std::chrono::milliseconds(wait_ms));
    }
    if (!rep.zero_resting_verified)
      rep.failures.push_back("zero-resting NOT verified after cancel-all");
  } else {
    std::printf("  [dry-run] would verify zero resting (<=%d polls)\n", kVerifyAttempts);
  }

  // ── 3. reprice-cross liquidation rounds ────────────────────────────────
  std::vector<Position> positions;
  if (!list_positions(client, parser, positions, rep)) {
    std::fprintf(stderr, "PANIC FAIL: cannot enumerate positions (%s)\n",
                 rep.failures.back().c_str());
    return 1;
  }
  rep.positions_seen = static_cast<int>(positions.size());

  std::uint64_t seq = 0;
  for (int round = 0; round < rounds && !positions.empty(); ++round) {
    rep.rounds_used = round + 1;
    for (const auto& pos : positions) {
      long bid_e4 = 0, ask_e4 = 0;
      if (!market_touch(client, parser, pos.ticker, bid_e4, ask_e4)) {
        rep.failures.push_back("market unreadable: " + pos.ticker);
        continue;
      }
      // Crossing price, one cent deeper each round (reprice-cross):
      //   exit long  -> sell into the bid: bid - round cents (floor 1c)
      //   exit short -> buy from the ask:  ask + round cents (cap 99c)
      long px = pos.is_long ? bid_e4 - 100L * round : ask_e4 + 100L * round;
      if (px < 100) px = 100;
      if (px > 9900) px = 9900;
      const std::string coid = liq_client_order_id(pos, px, run_ns, ++seq);
      std::printf("  round %d: %s %s %s yes @ %s IOC reduce_only coid=%s\n",
                  round + 1, pos.is_long ? "SELL" : "BUY",
                  pos.position_fp.c_str(), pos.ticker.c_str(),
                  e4_to_fp(px).c_str(), coid.substr(0, 13).c_str());
      if (!execute) continue;
      bool sent = false;
      for (int attempt = 0; attempt < 3 && !sent; ++attempt) {
        if (attempt) ++rep.liq_retries;
        auto r = client.request(Method::Post, wire::kCreateOrderPath,
                                liq_body(pos, px, coid));    // SAME coid on retry (#9)
        if (r && (r->ok() || r->status == 201)) { sent = true; break; }
        if (r && r->status == 409) { sent = true; break; }   // duplicate coid =
                                                             // earlier attempt landed
      }
      if (sent) ++rep.liq_orders_sent;
      else rep.failures.push_back("liquidation order unreachable: " + pos.ticker);
    }
    if (!execute) break;                          // dry-run prints round 1 only
    std::this_thread::sleep_for(std::chrono::milliseconds(wait_ms));
    if (!list_positions(client, parser, positions, rep)) break;
  }
  if (execute) {
    if (list_positions(client, parser, positions, rep) && positions.empty())
      rep.flat_verified = true;
    else if (!positions.empty()) {
      std::string left;
      for (const auto& p : positions) left += p.ticker + "=" + p.position_fp + " ";
      rep.failures.push_back("NOT FLAT after " + std::to_string(rounds) +
                             " rounds: " + left);
    }
  }

  // ── 4. report ──────────────────────────────────────────────────────────
  const bool clean = execute
      ? (rep.cancels_failed == 0 && rep.zero_resting_verified &&
         rep.flat_verified && rep.failures.empty())
      : rep.failures.empty();
  std::printf("panic report: orders=%d cancels_ok=%d cancels_failed=%d "
              "retries=%d | positions=%d liq_sent=%d rounds=%d | "
              "zero_resting=%s flat=%s\n",
              rep.orders_seen, rep.cancels_ok, rep.cancels_failed,
              rep.cancel_retries + rep.liq_retries, rep.positions_seen,
              rep.liq_orders_sent, rep.rounds_used,
              execute ? (rep.zero_resting_verified ? "VERIFIED" : "NO") : "n/a",
              execute ? (rep.flat_verified ? "VERIFIED" : "NO") : "n/a");
  for (const auto& f : rep.failures)
    std::fprintf(stderr, "  FAILURE: %s\n", f.c_str());
  if (json_path) {
    FILE* f = std::fopen(json_path, "w");
    if (f) {
      std::fprintf(f,
        "{\"mode\":\"%s\",\"orders_seen\":%d,\"cancels_ok\":%d,"
        "\"cancels_failed\":%d,\"retries\":%d,\"positions_seen\":%d,"
        "\"liq_orders_sent\":%d,\"rounds_used\":%d,"
        "\"zero_resting_verified\":%s,\"flat_verified\":%s,\"failures\":%d,"
        "\"verdict\":\"%s\"}\n",
        execute ? "execute" : "dry_run", rep.orders_seen, rep.cancels_ok,
        rep.cancels_failed, rep.cancel_retries + rep.liq_retries,
        rep.positions_seen, rep.liq_orders_sent, rep.rounds_used,
        rep.zero_resting_verified ? "true" : "false",
        rep.flat_verified ? "true" : "false",
        static_cast<int>(rep.failures.size()),
        clean ? "CLEAN" : "PARTIAL-FAILURE");
      std::fclose(f);
    }
  }
  std::printf("PANIC %s\n", clean ? (execute ? "CLEAN" : "DRY-RUN OK")
                                  : "PARTIAL-FAILURE");
  return clean ? 0 : 1;
}
