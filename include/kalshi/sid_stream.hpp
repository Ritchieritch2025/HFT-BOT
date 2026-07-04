#pragma once
//
// Per-subscription (sid) sequence stream.
//
// Kalshi WS `seq` is monotonic PER SID across ALL markets on that subscription,
// and is ALSO advanced by sequenced control responses (ok-with-seq,
// unsubscribed) — see docs/kalshi_ws_protocol.md I1. Gap detection therefore
// lives HERE, at the sid level, NOT in the per-market OrderBook: a single gap
// means data was lost on the whole stream, so EVERY market on the sid is
// invalidated and must be re-validated by an in-stream orderbook_snapshot
// (docs/kalshi_ws_protocol.md I3, I4). REST snapshots must never reseed a
// WS-live book.
//
// sid values die with the connection; each SidStream carries a stream_epoch so
// consumers can drop messages from a superseded connection (I8).

#include "trading/ids.hpp"

#include <cstdint>
#include <set>

namespace kalshi {

class SidStream {
 public:
  enum class SeqResult : std::uint8_t {
    First,  // first message seen on this sid (baseline established)
    Ok,     // contiguous with the expected next seq
    Gap,    // non-contiguous jump forward => data lost (invalidate members)
    Stale,  // seq older than expected (duplicate/reordered) => ignore
  };

  SidStream(std::uint64_t sid, std::uint32_t epoch) : sid_(sid), epoch_(epoch) {}

  std::uint64_t sid() const { return sid_; }
  std::uint32_t epoch() const { return epoch_; }
  std::uint64_t expected_next() const { return expected_; }

  void add_market(trading::EntityId e) { members_.insert(e); }
  void remove_market(trading::EntityId e) { members_.erase(e); }
  bool has_market(trading::EntityId e) const { return members_.count(e) != 0; }
  const std::set<trading::EntityId>& members() const { return members_; }

  // Observe a sequenced message's seq in arrival order. EVERY sequenced message
  // on the sid (snapshot, delta, ok-with-seq, unsubscribed) must call this so
  // control responses do not read as gaps (I1). Advances the expected counter
  // on First/Ok/Gap (re-baselines to the observed point after a gap so the
  // stream continues; per-market validity is gated separately).
  SeqResult observe(std::uint64_t seq) {
    if (!seen_) {
      seen_ = true;
      expected_ = seq + 1;
      return SeqResult::First;
    }
    if (seq == expected_) {
      ++expected_;
      return SeqResult::Ok;
    }
    if (seq < expected_) return SeqResult::Stale;
    expected_ = seq + 1;  // gap: resync the counter to the observed point
    return SeqResult::Gap;
  }

 private:
  std::uint64_t sid_;
  std::uint32_t epoch_;
  bool seen_ = false;
  std::uint64_t expected_ = 0;
  std::set<trading::EntityId> members_;
};

inline const char* to_string(SidStream::SeqResult r) {
  switch (r) {
    case SidStream::SeqResult::First: return "First";
    case SidStream::SeqResult::Ok: return "Ok";
    case SidStream::SeqResult::Gap: return "Gap";
    case SidStream::SeqResult::Stale: return "Stale";
  }
  return "?";
}

}  // namespace kalshi
