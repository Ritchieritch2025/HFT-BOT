// Recovery-ladder tests (I4): request_resync enqueues + fires get_snapshot;
// tick() escalates one rung per timeout (get_snapshot -> unsub/resub ->
// reconnect); note_recovered clears. Pure C++, no deps.

#include "kalshi/recovery.hpp"

#include <iostream>
#include <string>
#include <vector>

using namespace kalshi;
using trading::EntityId;

namespace {
int g_failures = 0;
void check(bool ok, const std::string& what) {
  std::cout << (ok ? "PASS: " : "FAIL: ") << what << "\n";
  if (!ok) ++g_failures;
}
}  // namespace

int main() {
  std::vector<std::string> snap_tickers, resub_tickers;
  int snaps = 0, resubs = 0, reconnects = 0;
  RecoveryLadder::Actions acts{
      [&](std::uint64_t, const std::vector<std::string>& t) { ++snaps; snap_tickers = t; },
      [&](std::uint64_t, const std::vector<std::string>& t) { ++resubs; resub_tickers = t; },
      [&]() { ++reconnects; }};
  auto resolver = [](EntityId e) { return "MKT-" + std::to_string(e.v % 100); };

  RecoveryLadder ladder(acts, resolver, /*rung_timeout_ms*/ 1000);
  const EntityId A = trading::make_entity_id(trading::SourceId::Kalshi, "MKT-A");

  ladder.tick(0);  // establish now=0
  ladder.request_resync(7, {A});
  check(snaps == 1 && !snap_tickers.empty(), "request_resync fires get_snapshot (rung 0)");
  check(ladder.rung(7) == RecoveryLadder::Rung::GetSnapshot, "rung == GetSnapshot");
  check(ladder.requests() == 1, "one resync request counted");

  // request again while active -> idempotent, no extra action
  ladder.request_resync(7, {A});
  check(snaps == 1 && ladder.requests() == 1, "duplicate request while active is a no-op");

  // tick before timeout -> no escalation
  ladder.tick(500);
  check(ladder.rung(7) == RecoveryLadder::Rung::GetSnapshot && resubs == 0,
        "no escalation before timeout");

  // tick after timeout -> escalate to UnsubResub
  ladder.tick(1600);
  check(resubs == 1 && ladder.rung(7) == RecoveryLadder::Rung::UnsubResub,
        "timeout escalates to unsub/resub");

  // next timeout -> Reconnect
  ladder.tick(2800);
  check(reconnects == 1 && ladder.rung(7) == RecoveryLadder::Rung::Reconnect,
        "next timeout escalates to reconnect");

  // recovery clears the ladder
  ladder.request_resync(8, {A});
  check(ladder.rung(8) == RecoveryLadder::Rung::GetSnapshot, "second sid resync starts");
  ladder.note_recovered(8);
  check(ladder.rung(8) == RecoveryLadder::Rung::Done && ladder.recoveries() == 1,
        "note_recovered clears the ladder");

  std::cout << (g_failures == 0 ? "ALL PASS\n" : "FAILURES\n");
  return g_failures == 0 ? 0 : 1;
}
