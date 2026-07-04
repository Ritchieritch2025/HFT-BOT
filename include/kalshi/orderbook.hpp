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

#include "kalshi/sid_stream.hpp"
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

  // Applies a single delta. Sequence-gap detection lives in SidStream (seq is
  // per-sid, not per-market — docs/kalshi_ws_protocol.md I1/I3), so this only
  // guards the local invariants: refuse while invalid, and go invalid (never
  // clamp) on a negative level. `seq` is stored as informational only.
  ApplyResult apply_delta(Side side, PriceE4 price, CountFp delta, std::uint64_t seq) {
    if (!valid_) return ApplyResult::NeedResync;  // refuse until resync

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

  // Deterministic content checksum (sorted levels + seq + validity). Written as
  // marker records during live capture and recomputed on replay to prove the
  // replayed book matches the live book bit-for-bit.
  std::uint64_t checksum() const {
    std::uint64_t h = 1469598103934665603ULL;
    const auto mix = [&](std::uint64_t x) { h ^= x; h *= 1099511628211ULL; };
    mix(valid_ ? 1 : 0);
    mix(last_seq_);
    for (const auto& [p, s] : yes_) { mix(1); mix(static_cast<std::uint64_t>(p)); mix(static_cast<std::uint64_t>(s)); }
    for (const auto& [p, s] : no_) { mix(2); mix(static_cast<std::uint64_t>(p)); mix(static_cast<std::uint64_t>(s)); }
    return h;
  }

  // Force the book invalid (e.g. a sid-level gap invalidates every member
  // market, I3). Only load_snapshot recovers it.
  void invalidate() { valid_ = false; }

  // Human-readable one-line state. Invalid books never print stale numbers.
  std::string dump_state() const {
    if (!valid_) return "OrderBook{INVALID (needs resync)}";
    std::string s = "OrderBook{seq=" + std::to_string(last_seq_) +
                    " yes_levels=" + std::to_string(yes_.size()) +
                    " no_levels=" + std::to_string(no_.size());
    if (auto b = best_yes_bid()) s += " yes_bid=" + trading::format_price_e4(*b);
    if (auto a = implied_yes_ask()) s += " impl_yes_ask=" + trading::format_price_e4(*a);
    if (crossed()) s += " CROSSED";
    s += "}";
    return s;
  }

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

// Resync contract. NON-BLOCKING and IN-STREAM only: on a sid gap or a book
// corruption the manager asks the transport to request a fresh in-stream
// orderbook_snapshot (update_subscription get_snapshot). It MUST NOT block or
// perform REST/IO synchronously on the hot path, and a REST snapshot must never
// reseed a WS-live book (docs/kalshi_ws_protocol.md I4). Implementations
// typically enqueue onto a control ring drained by a control thread.
struct ResyncHandler {
  virtual ~ResyncHandler() = default;
  virtual void request_resync(std::uint64_t sid,
                              const std::vector<EntityId>& markets) = 0;
};

// Owns one book per entity and one SidStream per subscription. Gap detection is
// per-sid (SidStream); a gap invalidates EVERY market on the sid and requests
// an in-stream resync. Single-threaded (bus threading contract).
class OrderBookManager {
 public:
  explicit OrderBookManager(ResyncHandler* handler = nullptr) : handler_(handler) {}

  // Bind a market to a sid on (re)subscribe (before any messages).
  void bind(std::uint64_t sid, std::uint32_t epoch, EntityId e) {
    stream(sid, epoch).add_market(e);
    sid_of_[e] = sid;
  }

  // In-stream snapshot: (re)establishes the market's book, advances the sid seq,
  // and marks the market valid. A snapshot arriving after a gap is the recovery.
  void on_snapshot(std::uint64_t sid, std::uint32_t epoch, EntityId e,
                   const SnapshotView& snap, std::uint64_t seq) {
    SidStream& s = stream(sid, epoch);
    s.add_market(e);
    sid_of_[e] = sid;
    (void)s.observe(seq);  // snapshot advances the sid counter (I1)
    SnapshotView sv = snap;
    sv.seq = seq;
    books_[e].load_snapshot(sv);
  }

  // Delta for a market on a sid. Routes through SidStream for gap detection.
  ApplyResult on_delta(std::uint64_t sid, std::uint32_t epoch, EntityId e,
                       Side side, PriceE4 price, CountFp delta, std::uint64_t seq) {
    SidStream& s = stream(sid, epoch);
    switch (s.observe(seq)) {
      case SidStream::SeqResult::Gap:
        invalidate_all(s);
        request_all(s);
        return ApplyResult::NeedResync;
      case SidStream::SeqResult::Stale:
        ++stale_;
        return ApplyResult::NeedResync;
      case SidStream::SeqResult::First:
      case SidStream::SeqResult::Ok:
        break;
    }
    OrderBook& book = books_[e];
    if (!book.valid()) {
      ++dropped_invalid_;  // waiting on a snapshot for this market
      return ApplyResult::NeedResync;
    }
    const ApplyResult r = book.apply_delta(side, price, delta, seq);
    if (r == ApplyResult::Invalid) {
      ++corrupt_;
      if (handler_) handler_->request_resync(s.sid(), {e});
    }
    return r;
  }

  // Sequenced control response (ok-with-seq / unsubscribed): no book effect but
  // MUST advance the sid counter so it does not read as a gap (I1).
  ApplyResult on_control_seq(std::uint64_t sid, std::uint32_t epoch, std::uint64_t seq) {
    SidStream& s = stream(sid, epoch);
    if (s.observe(seq) == SidStream::SeqResult::Gap) {
      invalidate_all(s);
      request_all(s);
      return ApplyResult::NeedResync;
    }
    return ApplyResult::Ok;
  }

  const OrderBook* book(EntityId e) const {
    auto it = books_.find(e);
    return it == books_.end() ? nullptr : &it->second;
  }
  OrderBook& book_ref(EntityId e) { return books_[e]; }

  // Free a market's book + sid membership (lifecycle determined/settled).
  void forget(EntityId e) {
    books_.erase(e);
    auto it = sid_of_.find(e);
    if (it != sid_of_.end()) {
      auto s = streams_.find(it->second);
      if (s != streams_.end()) s->second.remove_market(e);
      sid_of_.erase(it);
    }
  }
  // A book is tradeable only if present and valid (belt-and-braces for the
  // execution gate).
  bool tradeable(EntityId e) const {
    const OrderBook* b = book(e);
    return b && b->valid();
  }

  std::uint64_t resync_count() const { return resyncs_; }
  std::uint64_t stale_count() const { return stale_; }
  std::uint64_t dropped_invalid_count() const { return dropped_invalid_; }

 private:
  SidStream& stream(std::uint64_t sid, std::uint32_t epoch) {
    auto it = streams_.find(sid);
    if (it == streams_.end()) it = streams_.emplace(sid, SidStream(sid, epoch)).first;
    return it->second;
  }
  void invalidate_all(SidStream& s) {
    for (EntityId e : s.members()) books_[e].invalidate();  // I3
    ++resyncs_;
  }
  void request_all(SidStream& s) {
    if (!handler_) return;
    std::vector<EntityId> mk(s.members().begin(), s.members().end());
    handler_->request_resync(s.sid(), mk);  // non-blocking (I4)
  }

  std::map<std::uint64_t, SidStream> streams_;
  std::map<EntityId, OrderBook> books_;
  std::map<EntityId, std::uint64_t> sid_of_;
  ResyncHandler* handler_;
  std::uint64_t resyncs_ = 0, stale_ = 0, dropped_invalid_ = 0, corrupt_ = 0;
};

}  // namespace kalshi
