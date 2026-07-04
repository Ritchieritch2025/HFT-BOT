#pragma once
//
// Deterministic orderbook reconstruction for a single binary market. Pure
// logic — depends only on the source-agnostic trading types, so it is fully
// unit-testable without any network.
//
// Invariants (fail-safe, per audit directive 7):
//   - a delta that would drive a level negative means the book is ALREADY
//     wrong: mark invalid, do NOT clamp-and-continue;
//   - once invalid, every apply_delta returns NeedResync without mutating, and
//     best/implied/depth accessors refuse to serve (nullopt / empty) — a
//     manager that ignores return codes cannot keep reading a corrupt book;
//   - only load_snapshot clears the invalid state.
//
// Sequencing: Kalshi WS seq is scoped to the subscription (sid), so cross-book
// gap detection belongs in OrderBookManager, keyed by (sid, EntityId). The
// per-book seq!=last+1 check here is the local consistency guard under that
// keying. Recovery is resubscribe-first (a fresh snapshot with a coherent seq);
// a REST snapshot is only a bootstrap/validation source.

#include "trading/bus.hpp"
#include "trading/fixedpoint.hpp"
#include "trading/ids.hpp"

#include <cstdint>
#include <map>
#include <optional>
#include <vector>

namespace kalshi {

using trading::CountFp;
using trading::EntityId;
using trading::Level;
using trading::PriceE4;
using trading::Side;

// Raw snapshot for load/resync (source-agnostic; avoids depending on rest_api
// types so this header stays pure).
struct SnapshotView {
  std::vector<Level> yes;
  std::vector<Level> no;
  std::uint64_t seq = 0;
};

enum class ApplyResult : std::uint8_t { Ok, NeedResync, Invalid };
inline const char* to_string(ApplyResult r) {
  switch (r) {
    case ApplyResult::Ok: return "Ok";
    case ApplyResult::NeedResync: return "NeedResync";
    case ApplyResult::Invalid: return "Invalid";
  }
  return "?";
}

class OrderBook {
 public:
  OrderBook() = default;

  // Establishes state from a coherent snapshot; clears any invalid flag and
  // sets the sequence baseline. Non-positive sizes are dropped.
  void load_snapshot(const SnapshotView& snap) {
    yes_.clear();
    no_.clear();
    for (const Level& l : snap.yes) if (l.size > 0) yes_[l.price] = l.size;
    for (const Level& l : snap.no) if (l.size > 0) no_[l.price] = l.size;
    last_seq_ = snap.seq;
    valid_ = true;
  }

  // Applies a single delta. See invariants above.
  ApplyResult apply_delta(Side side, PriceE4 price, CountFp delta, std::uint64_t seq) {
    if (!valid_) return ApplyResult::NeedResync;                 // refuse until resync
    if (seq != last_seq_ + 1) return ApplyResult::NeedResync;    // gap, no mutation

    auto& book = side == Side::Yes ? yes_ : no_;
    auto it = book.find(price);
    const CountFp cur = it == book.end() ? 0 : it->second;
    const CountFp next = cur + delta;
    if (next < 0) {                                             // book is wrong
      valid_ = false;
      return ApplyResult::Invalid;
    }
    if (next == 0) {
      if (it != book.end()) book.erase(it);
    } else if (it == book.end()) {
      book.emplace(price, next);
    } else {
      it->second = next;
    }
    last_seq_ = seq;
    return ApplyResult::Ok;
  }

  bool valid() const { return valid_; }
  std::uint64_t last_seq() const { return last_seq_; }

  // Best resting YES bid (highest yes price). nullopt if invalid or empty.
  std::optional<PriceE4> best_yes_bid() const {
    if (!valid_ || yes_.empty()) return std::nullopt;
    return yes_.rbegin()->first;
  }
  // Best resting NO bid (highest no price).
  std::optional<PriceE4> best_no_bid() const {
    if (!valid_ || no_.empty()) return std::nullopt;
    return no_.rbegin()->first;
  }
  // A NO bid at q is a YES ask at (1 - q).
  std::optional<PriceE4> implied_yes_ask() const {
    auto nb = best_no_bid();
    if (!nb) return std::nullopt;
    return static_cast<PriceE4>(trading::kPriceMax - *nb);
  }
  std::optional<PriceE4> implied_yes_bid() const { return best_yes_bid(); }

  // Crossed = yes bid >= implied yes ask. False if either side absent/invalid.
  bool crossed() const {
    auto b = best_yes_bid();
    auto a = implied_yes_ask();
    return b && a && *b >= *a;
  }

  // Depth views. Empty while invalid. Ascending price.
  std::vector<Level> yes_depth(std::size_t max = 0) const { return depth(yes_, max); }
  std::vector<Level> no_depth(std::size_t max = 0) const { return depth(no_, max); }

  std::optional<CountFp> yes_size_at(PriceE4 p) const { return size_at(yes_, p); }
  std::optional<CountFp> no_size_at(PriceE4 p) const { return size_at(no_, p); }

  std::size_t yes_levels() const { return valid_ ? yes_.size() : 0; }
  std::size_t no_levels() const { return valid_ ? no_.size() : 0; }

 private:
  std::vector<Level> depth(const std::map<PriceE4, CountFp>& book, std::size_t max) const {
    std::vector<Level> out;
    if (!valid_) return out;
    for (const auto& [p, s] : book) {
      out.push_back({p, s});
      if (max && out.size() >= max) break;
    }
    return out;
  }
  std::optional<CountFp> size_at(const std::map<PriceE4, CountFp>& book, PriceE4 p) const {
    if (!valid_) return std::nullopt;
    auto it = book.find(p);
    return it == book.end() ? std::optional<CountFp>(0) : std::optional<CountFp>(it->second);
  }

  std::map<PriceE4, CountFp> yes_;
  std::map<PriceE4, CountFp> no_;
  std::uint64_t last_seq_ = 0;
  bool valid_ = false;  // no data until first snapshot
};

// Resync contract. Resubscribe-first: on gap/invalid the manager asks the
// transport to re-subscribe (delivering a fresh snapshot with a coherent seq).
// fetch_validation is a bootstrap/cross-check source only.
struct ResyncHandler {
  virtual ~ResyncHandler() = default;
  virtual void request_resubscribe(EntityId) = 0;
  virtual std::optional<SnapshotView> fetch_validation(EntityId) = 0;
};

// Owns one book per entity, applies snapshots/deltas, and drives resync on
// gap/invalid. Single-threaded (bus threading contract).
class OrderBookManager {
 public:
  explicit OrderBookManager(ResyncHandler* handler = nullptr) : handler_(handler) {}

  void on_snapshot(EntityId e, const SnapshotView& snap) {
    books_[e].load_snapshot(snap);
  }

  ApplyResult on_delta(EntityId e, Side side, PriceE4 price, CountFp delta,
                       std::uint64_t seq) {
    OrderBook& book = books_[e];
    const ApplyResult r = book.apply_delta(side, price, delta, seq);
    if (r != ApplyResult::Ok) {
      ++resyncs_;
      if (handler_) {
        handler_->request_resubscribe(e);
        if (auto snap = handler_->fetch_validation(e)) book.load_snapshot(*snap);
      }
    }
    return r;
  }

  const OrderBook* book(EntityId e) const {
    auto it = books_.find(e);
    return it == books_.end() ? nullptr : &it->second;
  }
  OrderBook& book_ref(EntityId e) { return books_[e]; }
  std::uint64_t resync_count() const { return resyncs_; }

 private:
  std::map<EntityId, OrderBook> books_;
  ResyncHandler* handler_;
  std::uint64_t resyncs_ = 0;
};

}  // namespace kalshi
