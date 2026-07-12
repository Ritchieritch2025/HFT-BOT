#pragma once
//
// Durable WS recorder. The read/transport thread stamps receive times ONCE and
// enqueues an owned RawRecord onto an SPSC ring; a dedicated writer thread
// drains it into a RawLogWriter. The read loop NEVER blocks: on ring overflow
// it drops + counts, and the writer emits a "loss" marker record recording how
// many frames were lost as soon as the queue drains.
//
// Marker records ("gap", "resync_begin/end", "loss", "epoch_change") annotate
// the stream so replay honestly mirrors live blind spots.
//
// NOTE: RawRecord owns its bytes (std::string). A slab/freelist of frame
// buffers is a Phase-7 perf refinement gated on measured allocation cost; the
// interface here does not change when it lands.

#include "kalshi/ring.hpp"
#include "trading/storage.hpp"
#include "trading/timestamp.hpp"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <thread>

namespace kalshi {

class WsRecorder {
 public:
  WsRecorder(std::string path, std::size_t ring_capacity = 8192,
             std::size_t max_bytes = 256ull * 1024 * 1024)
      : ring_(ring_capacity), writer_(std::move(path), max_bytes) {}

  ~WsRecorder() { stop(); }

  void start() {
    if (running_.exchange(true)) return;
    thread_ = std::thread([this] { drain_loop(); });
  }

  void stop() {
    if (!running_.exchange(false)) return;
    if (thread_.joinable()) thread_.join();
    drain_once();  // flush anything left after the writer thread exits
    if (!writer_.flush()) write_failures_.fetch_add(1, std::memory_order_relaxed);
  }

  // Hot path (read/transport thread). Non-blocking: enqueue or drop+count.
  void record(trading::RawRecord&& rec) {
    const std::int64_t dropped_wall_ns = rec.recv_wall_ns;
    if (ring_.try_push(std::move(rec))) {
      recorded_.fetch_add(1, std::memory_order_relaxed);
    } else {
      dropped_.fetch_add(1, std::memory_order_relaxed);
      pending_loss_.fetch_add(1, std::memory_order_relaxed);
      std::int64_t empty = 0;
      pending_loss_wall_ns_.compare_exchange_strong(
          empty, dropped_wall_ns, std::memory_order_relaxed);
    }
  }

  // Enqueue a marker (best-effort; markers are small and rarely dropped).
  void mark(const std::string& kind, std::uint32_t epoch = 0,
            std::optional<std::uint64_t> sid = std::nullopt) {
    trading::RawRecord m;
    m.source = trading::SourceId::Kalshi;
    m.marker = kind;
    m.stream_epoch = epoch;
    m.source_stream_id = sid;
    m.recv_mono_ns = trading::mono_ns();
    m.recv_wall_ns = trading::wall_ns();
    if (!ring_.try_push(std::move(m))) dropped_.fetch_add(1, std::memory_order_relaxed);
  }

  std::uint64_t recorded() const { return recorded_.load(); }
  std::uint64_t dropped() const { return dropped_.load(); }
  std::uint64_t write_failures() const { return write_failures_.load(); }

 private:
  void drain_loop() {
    trading::RawRecord rec;
    while (running_.load(std::memory_order_relaxed)) {
      bool did = false;
      while (ring_.try_pop(rec)) {
        if (!writer_.write(rec)) write_failures_.fetch_add(1, std::memory_order_relaxed);
        did = true;
      }
      emit_pending_loss();
      // Emit only after the currently queued receive records have drained.
      // The wrapper additionally requires old-file size stability before it
      // hashes a closed hour; together this avoids treating the boundary
      // marker as a close barrier while pre-boundary rows remain queued.
      emit_hour_open_if_needed();
      if (!did) std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
  }
  void drain_once() {
    trading::RawRecord rec;
    while (ring_.try_pop(rec)) {
      if (!writer_.write(rec)) write_failures_.fetch_add(1, std::memory_order_relaxed);
    }
    emit_pending_loss();
    emit_hour_open_if_needed();
  }
  // When the ring has drained, record how many frames were lost during the
  // backlog as a single "loss" marker, then reset.
  void emit_pending_loss() {
    const std::uint64_t lost = pending_loss_.exchange(0, std::memory_order_relaxed);
    if (lost == 0) return;
    trading::RawRecord m;
    m.source = trading::SourceId::Kalshi;
    m.marker = "loss";
    m.recv_mono_ns = trading::mono_ns();
    m.recv_wall_ns = pending_loss_wall_ns_.exchange(0, std::memory_order_relaxed);
    if (m.recv_wall_ns <= 0) m.recv_wall_ns = trading::wall_ns();
    m.source_sequence = lost;  // reuse the seq field to carry the lost count
    if (!writer_.write(m)) write_failures_.fetch_add(1, std::memory_order_relaxed);
  }
  void emit_hour_open_if_needed() {
    if (!writer_.time_partitioned()) return;
    const std::int64_t wall = trading::wall_ns();
    const std::int64_t hour = wall / 3'600'000'000'000LL;
    if (hour == last_hour_) return;
    trading::RawRecord m;
    m.source = trading::SourceId::Kalshi;
    m.marker = "hour_open";
    m.recv_mono_ns = trading::mono_ns();
    m.recv_wall_ns = wall;
    bool durable = false;
    if (!writer_.write(m)) {
      write_failures_.fetch_add(1, std::memory_order_relaxed);
    } else if (!writer_.flush()) {
      write_failures_.fetch_add(1, std::memory_order_relaxed);
    } else {
      durable = true;
    }
    if (durable) last_hour_ = hour;
  }

  Ring<trading::RawRecord> ring_;
  trading::RawLogWriter writer_;  // touched only on the writer thread
  std::thread thread_;
  std::atomic<bool> running_{false};
  std::atomic<std::uint64_t> recorded_{0}, dropped_{0}, pending_loss_{0};
  std::atomic<std::int64_t> pending_loss_wall_ns_{0};
  std::atomic<std::uint64_t> write_failures_{0};
  std::int64_t last_hour_ = -1;  // writer thread only
};

}  // namespace kalshi
