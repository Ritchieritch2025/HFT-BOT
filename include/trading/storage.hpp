#pragma once
//
// Durable, source-agnostic market-data storage + replay.
//
// RawLogWriter appends one NDJSON line per received message, preserving the
// original payload BYTE-EXACT: valid UTF-8 JSON goes in a "raw" escaped string,
// anything else (binary frames, permessage-deflate) goes in "raw_b64". The
// reader recovers the exact bytes and tolerates a truncated final line (a crash
// mid-write with fsync off).
//
// ReplaySource is a trading::DataSource backed by a raw log + a per-source
// decoder; it preserves each record's original SourceId, so downstream behaves
// identically live vs replay.

#include "trading/bus.hpp"
#include "trading/ids.hpp"

#include <cstdint>
#include <cstdio>
#include <functional>
#include <optional>
#include <string>

namespace trading {

// One stored message, source-agnostic. `raw` holds the exact original bytes.
struct RawRecord {
  std::int64_t recv_mono_ns = 0;
  std::int64_t recv_wall_ns = 0;
  std::optional<std::int64_t> source_event_time_ms;
  SourceId source = SourceId::Unknown;
  std::string channel;
  std::string source_ticker;  // raw records do NOT depend on EntityId/registry
  std::optional<std::uint64_t> source_sequence;    // per-sid seq (orderbook only)
  std::optional<std::uint64_t> source_stream_id;   // sid
  std::uint32_t stream_epoch = 0;                   // (re)connection epoch
  // Marker records carry NO payload — they annotate the stream: "gap",
  // "resync_begin"/"resync_end", "loss", "epoch_change". Reader/replay surface
  // them so replayed state honestly mirrors live blind spots.
  std::optional<std::string> marker;
  std::string raw;            // byte-exact original payload (empty for markers)
};

SourceId source_from_wire(std::string_view);  // inverse of trading::to_string

class RawLogWriter {
 public:
  explicit RawLogWriter(std::string path, std::size_t max_bytes = 256ull * 1024 * 1024,
                        bool fsync_each = false);
  ~RawLogWriter();
  RawLogWriter(const RawLogWriter&) = delete;
  RawLogWriter& operator=(const RawLogWriter&) = delete;

  void write(const RawRecord& rec);  // appends one NDJSON line; rotates by size
  void flush();

  std::size_t rotations() const { return rotations_; }
  const std::string& current_path() const { return current_path_; }

 private:
  void open_current();
  void rotate();

  std::string base_path_;
  std::string current_path_;
  std::FILE* f_ = nullptr;
  std::size_t max_bytes_;
  std::size_t bytes_ = 0;
  std::size_t index_ = 0;
  std::size_t rotations_ = 0;
  bool fsync_each_;
};

class RawLogReader {
 public:
  explicit RawLogReader(const std::string& path);
  ~RawLogReader();
  RawLogReader(const RawLogReader&) = delete;
  RawLogReader& operator=(const RawLogReader&) = delete;

  // Next record, or nullopt at EOF. A truncated final line is skipped (counted
  // in skipped_truncated()), not treated as fatal.
  std::optional<RawRecord> next();
  std::size_t skipped_truncated() const { return skipped_; }
  bool ok() const { return f_ != nullptr; }

 private:
  std::FILE* f_ = nullptr;
  std::string buf_;
  std::size_t skipped_ = 0;
};

// Turns one stored raw record into a NormalizedEvent (source-specific). Returns
// nullopt for record types it does not model.
struct RawDecoder {
  virtual ~RawDecoder() = default;
  virtual std::optional<NormalizedEvent> decode(const RawRecord&) = 0;
};

// A DataSource that replays a raw log through a decoder into a sink, preserving
// each record's original SourceId.
class ReplaySource : public DataSource {
 public:
  ReplaySource(std::string path, RawDecoder& decoder, SourceId id)
      : path_(std::move(path)), decoder_(decoder), id_(id) {}

  SourceId id() const override { return id_; }
  void set_sink(MarketDataSink* sink) override { sink_ = sink; }
  // Marker records ("gap"/"loss"/"epoch_change"/checksum) are surfaced here so
  // the replay can mirror live blind spots (e.g. invalidate on a gap) — a
  // marker is not decodable into a NormalizedEvent.
  void on_marker(std::function<void(const RawRecord&)> fn) { marker_fn_ = std::move(fn); }
  void start() override;  // reads all records, decodes, pushes to the sink
  void stop() override { stopped_ = true; }

  std::uint64_t emitted() const { return emitted_; }
  std::uint64_t records() const { return records_; }
  std::uint64_t markers() const { return markers_; }

 private:
  std::string path_;
  RawDecoder& decoder_;
  SourceId id_;
  MarketDataSink* sink_ = nullptr;
  std::function<void(const RawRecord&)> marker_fn_;
  std::uint64_t emitted_ = 0;
  std::uint64_t records_ = 0;
  std::uint64_t markers_ = 0;
  bool stopped_ = false;
};

// Normalized-event JSONL writer for replay/analysis (deterministic EntityId
// makes these stable across restarts).
class NormalizedWriter {
 public:
  explicit NormalizedWriter(const std::string& path);
  ~NormalizedWriter();
  void write(const NormalizedEvent&);

 private:
  std::FILE* f_ = nullptr;
};

}  // namespace trading
