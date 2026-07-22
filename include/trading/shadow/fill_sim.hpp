#pragma once
//
// Stateful fill simulator — the pessimistic heart of the shadow MM engine
// (ACCEPTANCE_影子做市引擎 hard fill model, items 1-4 and 9).
//
// Three tracks (W-FS1):
//   Strict     — fill ONLY on a strict trade-through: a real taker print at a
//                price strictly worse than our level (proof the whole level
//                was consumed). No queue state needed; pessimistic on queue.
//   Queue      — queue position modeled: queue_ahead is snapshotted from the
//                displayed depth at our level at activation; at-or-through
//                prints drain the queue first, then fill us. Orders whose
//                displayed depth is UNKNOWN are INELIGIBLE in this track
//                (fail closed, counted) — never assumed queue-front.
//   Optimistic — at-or-through fills instantly, no queue. DIAGNOSTIC ONLY:
//                exists so the known-answer test can show how inflated it is
//                versus Strict/Queue. Never used for a go/no-go number.
//
// Timing (item 4 + 9): an order becomes active at place_ts + place_latency;
// a cancel becomes effective at cancel_ts + cancel_latency. Prints before
// activation never fill; fills can still land inside the cancel window —
// that window IS the adverse-selection exposure being measured.
//
// Only real tape taker flow fills anything (item 3): no synthetic favorable
// fills exist anywhere in this file.

#include "trading/shadow/order_registry.hpp"

#include <cstdint>
#include <vector>

namespace trading::shadow {

enum class Track : std::uint8_t { Strict, Queue, Optimistic };

inline const char* to_string(Track t) {
  switch (t) {
    case Track::Strict: return "strict";
    case Track::Queue: return "queue";
    case Track::Optimistic: return "optimistic";
  }
  return "?";
}

// Speed axis (item 9): PnL is reported as a function of these two numbers.
struct SpeedParams {
  std::uint64_t place_latency_ns = 7'000'000;    // decision -> live at venue
  std::uint64_t cancel_latency_ns = 7'000'000;   // cancel decision -> dead
};

struct Fill {
  std::uint32_t order_idx = 0;
  std::uint32_t market = 0;
  std::int32_t price_cents = 0;  // our resting price (maker: we fill at OUR level)
  std::int32_t qty = 0;
  bool buy_yes = false;
  std::uint64_t ts_ns = 0;
};

// One market's active orders for one track. The simulator owns per-market
// order lists (small vectors, preallocated growth) and produces Fills; the
// caller (accounting thread) owns registry + ledger.
class FillSim {
 public:
  FillSim(OrderRegistry& reg, Track track) : reg_(reg), track_(track) {}

  // Activation bookkeeping: caller invokes when tape time reaches
  // active_ts_ns. queue_ahead: displayed contracts at our level (nullopt-style
  // -1 = unknown -> ineligible in Queue track).
  // Returns false when the order cannot participate in this track.
  bool activate(std::uint32_t idx, std::int64_t displayed_at_level) {
    OrderRec& r = reg_.at(idx);
    if (track_ == Track::Queue && displayed_at_level < 0) {
      ++ineligible_unknown_depth_;
      return false;
    }
    r.queue_ahead = (track_ == Track::Queue) ? displayed_at_level : 0;
    open_.push_back(idx);
    return true;
  }

  void remove(std::uint32_t idx) {
    for (std::size_t i = 0; i < open_.size(); ++i)
      if (open_[i] == idx) {
        open_[i] = open_.back();
        open_.pop_back();
        return;
      }
  }

  // A real taker print from the tape: yes-price q, size, taker bought YES
  // (taker_yes=true, hits resting sell-YES) or bought NO (hits resting
  // buy-YES). ts is tape time. Emits fills through `out`.
  void on_trade(std::uint32_t market, std::int32_t q, std::int32_t size,
                bool taker_yes, std::uint64_t ts,
                std::vector<Fill>& out) {
    for (std::size_t i = 0; i < open_.size();) {
      const std::uint32_t idx = open_[i];
      OrderRec& r = reg_.at(idx);
      bool advance = true;
      if (r.market == market && ts >= r.active_ts_ns && crosses(r, q, taker_yes)) {
        // Cancel already effective? The order is dead even if the registry
        // hasn't been swept yet.
        if (r.cancel_eff_ns != 0 && ts >= r.cancel_eff_ns) {
          ++i;
          continue;
        }
        std::int32_t available = size;
        if (track_ == Track::Queue && at_level_only(r, q)) {
          // Drain the queue ahead of us first (item 1).
          const std::int64_t drain =
              available < r.queue_ahead ? available : r.queue_ahead;
          r.queue_ahead -= drain;
          available -= static_cast<std::int32_t>(drain);
        } else if (track_ == Track::Strict && at_level_only(r, q)) {
          // Strict: at-level prints never fill us — only strictly-through.
          available = 0;
        }
        if (available > 0 && (track_ != Track::Queue || r.queue_ahead == 0)) {
          const std::int32_t qty =
              available < r.remaining() ? available : r.remaining();
          if (qty > 0) {
            out.push_back(Fill{idx, market, r.price_cents, qty, r.buy_yes, ts});
            reg_.apply_fill(idx, qty);
            if (r.remaining() == 0) {
              open_[i] = open_.back();
              open_.pop_back();
              advance = false;
            }
          }
        }
      }
      if (advance) ++i;
    }
  }

  std::uint64_t ineligible_unknown_depth() const {
    return ineligible_unknown_depth_;
  }
  std::size_t open_count() const { return open_.size(); }
  Track track() const { return track_; }

 private:
  // Does this print cross our resting order at all (at-or-through)?
  static bool crosses(const OrderRec& r, std::int32_t q, bool taker_yes) {
    if (r.buy_yes)  // resting buy YES filled by aggressive YES sellers
      return !taker_yes && q <= r.price_cents;
    // resting sell YES filled by aggressive YES buyers
    return taker_yes && q >= r.price_cents;
  }
  static bool at_level_only(const OrderRec& r, std::int32_t q) {
    return q == r.price_cents;
  }

  OrderRegistry& reg_;
  Track track_;
  std::vector<std::uint32_t> open_;
  std::uint64_t ineligible_unknown_depth_ = 0;
};

}  // namespace trading::shadow
