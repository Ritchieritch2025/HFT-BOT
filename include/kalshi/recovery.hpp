#pragma once
//
// Recovery ladder (docs/kalshi_ws_protocol.md I4). On a sid gap/overflow the
// OrderBookManager (hot path) calls request_resync, which only ENQUEUES intent;
// a control loop drives tick(now_ms), escalating one rung per timeout:
//
//   GetSnapshot -> (timeout) UnsubResub -> (timeout) Reconnect -> Done
//
// In-stream recovery only: a REST snapshot never re-seeds a WS-live book. A
// market re-validated by an in-stream orderbook_snapshot calls note_recovered.

#include "kalshi/orderbook.hpp"

#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <vector>

namespace kalshi {

class RecoveryLadder : public ResyncHandler {
 public:
  enum class Rung : std::uint8_t { GetSnapshot, UnsubResub, Reconnect, Done };

  struct Actions {
    std::function<void(std::uint64_t sid, const std::vector<std::string>&)> get_snapshot;
    std::function<void(std::uint64_t sid, const std::vector<std::string>&)> unsub_resub;
    std::function<void()> reconnect;
  };

  RecoveryLadder(Actions actions,
                 std::function<std::string(trading::EntityId)> resolver,
                 std::int64_t rung_timeout_ms = 2000)
      : act_(std::move(actions)), resolve_(std::move(resolver)),
        timeout_(rung_timeout_ms) {}

  // Hot-path safe: enqueue only. Idempotent per sid while a resync is active.
  void request_resync(std::uint64_t sid,
                      const std::vector<trading::EntityId>& markets) override {
    St& st = state_[sid];
    if (st.active) return;
    st.active = true;
    st.rung = Rung::GetSnapshot;
    st.since_ms = now_;
    st.tickers.clear();
    for (trading::EntityId e : markets)
      if (resolve_) st.tickers.push_back(resolve_(e));
    ++requests_;
    if (act_.get_snapshot) act_.get_snapshot(sid, st.tickers);  // rung 0 action
  }

  // A market's in-stream snapshot re-validated the sid: clear the resync.
  void note_recovered(std::uint64_t sid) {
    auto it = state_.find(sid);
    if (it != state_.end() && it->second.active) {
      it->second.active = false;
      it->second.rung = Rung::Done;
      ++recoveries_;
    }
  }

  // Control-loop driver. Escalates any active sid whose rung has timed out.
  void tick(std::int64_t now_ms) {
    now_ = now_ms;
    for (auto& [sid, st] : state_) {
      if (!st.active || now_ms - st.since_ms < timeout_) continue;
      st.since_ms = now_ms;
      switch (st.rung) {
        case Rung::GetSnapshot:
          st.rung = Rung::UnsubResub;
          ++escalations_;
          if (act_.unsub_resub) act_.unsub_resub(sid, st.tickers);
          break;
        case Rung::UnsubResub:
          st.rung = Rung::Reconnect;
          ++escalations_;
          if (act_.reconnect) act_.reconnect();
          break;
        case Rung::Reconnect:
          st.active = false;  // reconnect issued; a fresh connection restarts flow
          st.rung = Rung::Done;
          break;
        case Rung::Done:
          break;
      }
    }
  }

  Rung rung(std::uint64_t sid) const {
    auto it = state_.find(sid);
    return it == state_.end() ? Rung::Done : it->second.rung;
  }
  std::uint64_t requests() const { return requests_; }
  std::uint64_t escalations() const { return escalations_; }
  std::uint64_t recoveries() const { return recoveries_; }

 private:
  struct St {
    bool active = false;
    Rung rung = Rung::Done;
    std::int64_t since_ms = 0;
    std::vector<std::string> tickers;
  };
  Actions act_;
  std::function<std::string(trading::EntityId)> resolve_;
  std::int64_t timeout_;
  std::int64_t now_ = 0;
  std::map<std::uint64_t, St> state_;
  std::uint64_t requests_ = 0, escalations_ = 0, recoveries_ = 0;
};

}  // namespace kalshi
