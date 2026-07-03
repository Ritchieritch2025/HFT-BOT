#pragma once
//
// Bounded lock-free MPMC ring (Dmitry Vyukov's algorithm), header-only, no
// dependencies. This is the in-process handoff between the hot decision
// thread and the submit/telemetry threads: try_push/try_pop never block and
// never allocate — a full ring is an explicit drop decision at the caller,
// not a stall.
//
// Memory-ordering contract (verbatim Vyukov — do not "simplify"):
// each cell carries a sequence number; publishers/consumers synchronize
// exclusively through acquire loads and release stores on that sequence.
// The two position counters use relaxed ordering plus CAS for slot claiming.

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <utility>

namespace kalshi {

template <typename T>
class Ring {
 public:
  explicit Ring(std::size_t capacity)
      : cells_(std::make_unique<Cell[]>(capacity)), mask_(capacity - 1) {
    if (capacity < 2 || (capacity & (capacity - 1)) != 0)
      throw std::invalid_argument("Ring capacity must be a power of two >= 2");
    for (std::size_t i = 0; i != capacity; ++i)
      cells_[i].seq.store(i, std::memory_order_relaxed);
    enqueue_pos_.store(0, std::memory_order_relaxed);
    dequeue_pos_.store(0, std::memory_order_relaxed);
  }

  Ring(const Ring&) = delete;
  Ring& operator=(const Ring&) = delete;

  // False when full. Never blocks.
  bool try_push(T&& v) {
    Cell* cell;
    std::size_t pos = enqueue_pos_.load(std::memory_order_relaxed);
    for (;;) {
      cell = &cells_[pos & mask_];
      const std::size_t seq = cell->seq.load(std::memory_order_acquire);
      const auto dif = static_cast<std::intptr_t>(seq) - static_cast<std::intptr_t>(pos);
      if (dif == 0) {
        if (enqueue_pos_.compare_exchange_weak(pos, pos + 1,
                                               std::memory_order_relaxed))
          break;
      } else if (dif < 0) {
        return false;  // full
      } else {
        pos = enqueue_pos_.load(std::memory_order_relaxed);
      }
    }
    cell->val = std::move(v);
    cell->seq.store(pos + 1, std::memory_order_release);
    return true;
  }

  // False when empty. Never blocks.
  bool try_pop(T& out) {
    Cell* cell;
    std::size_t pos = dequeue_pos_.load(std::memory_order_relaxed);
    for (;;) {
      cell = &cells_[pos & mask_];
      const std::size_t seq = cell->seq.load(std::memory_order_acquire);
      const auto dif =
          static_cast<std::intptr_t>(seq) - static_cast<std::intptr_t>(pos + 1);
      if (dif == 0) {
        if (dequeue_pos_.compare_exchange_weak(pos, pos + 1,
                                               std::memory_order_relaxed))
          break;
      } else if (dif < 0) {
        return false;  // empty
      } else {
        pos = dequeue_pos_.load(std::memory_order_relaxed);
      }
    }
    out = std::move(cell->val);
    cell->seq.store(pos + mask_ + 1, std::memory_order_release);
    return true;
  }

  std::size_t capacity() const noexcept { return mask_ + 1; }

  // Racy snapshot; for stats/logging only.
  std::size_t size_approx() const noexcept {
    return enqueue_pos_.load(std::memory_order_relaxed) -
           dequeue_pos_.load(std::memory_order_relaxed);
  }

 private:
  struct Cell {
    std::atomic<std::size_t> seq;
    T val;
  };

  std::unique_ptr<Cell[]> cells_;
  std::size_t mask_;
  alignas(64) std::atomic<std::size_t> enqueue_pos_;
  alignas(64) std::atomic<std::size_t> dequeue_pos_;
};

}  // namespace kalshi
