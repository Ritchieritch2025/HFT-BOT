#include "trading/storage.hpp"

#include "simdjson.h"

#include <cstring>
#include <ctime>
#include <filesystem>

namespace trading {

namespace {

// --- base64 (for non-UTF-8 raw payloads) ---
constexpr char kB64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

std::string base64_encode(std::string_view in) {
  std::string out;
  out.reserve((in.size() + 2) / 3 * 4);
  std::size_t i = 0;
  for (; i + 3 <= in.size(); i += 3) {
    const unsigned v = (static_cast<unsigned char>(in[i]) << 16) |
                       (static_cast<unsigned char>(in[i + 1]) << 8) |
                       static_cast<unsigned char>(in[i + 2]);
    out += kB64[(v >> 18) & 63];
    out += kB64[(v >> 12) & 63];
    out += kB64[(v >> 6) & 63];
    out += kB64[v & 63];
  }
  if (i + 1 == in.size()) {
    const unsigned v = static_cast<unsigned char>(in[i]) << 16;
    out += kB64[(v >> 18) & 63];
    out += kB64[(v >> 12) & 63];
    out += "==";
  } else if (i + 2 == in.size()) {
    const unsigned v = (static_cast<unsigned char>(in[i]) << 16) |
                       (static_cast<unsigned char>(in[i + 1]) << 8);
    out += kB64[(v >> 18) & 63];
    out += kB64[(v >> 12) & 63];
    out += kB64[(v >> 6) & 63];
    out += '=';
  }
  return out;
}

int b64val(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}

std::string base64_decode(std::string_view in) {
  std::string out;
  int buf = 0, bits = 0;
  for (char c : in) {
    if (c == '=') break;
    const int v = b64val(c);
    if (v < 0) continue;
    buf = (buf << 6) | v;
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out += static_cast<char>((buf >> bits) & 0xFF);
    }
  }
  return out;
}

// Is the payload valid UTF-8 (so it can be stored as an escaped JSON string)?
bool is_valid_utf8(std::string_view s) {
  std::size_t i = 0;
  while (i < s.size()) {
    const unsigned char c = s[i];
    std::size_t n;
    if (c < 0x80) n = 0;
    else if ((c >> 5) == 0x6) n = 1;
    else if ((c >> 4) == 0xE) n = 2;
    else if ((c >> 3) == 0x1E) n = 3;
    else return false;
    if (i + n >= s.size() && n > 0) return false;
    for (std::size_t k = 1; k <= n; ++k)
      if ((static_cast<unsigned char>(s[i + k]) >> 6) != 0x2) return false;
    i += n + 1;
  }
  return true;
}

void json_escape(std::string_view s, std::string& out) {
  out += '"';
  for (char ch : s) {
    const unsigned char c = ch;
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      case '\b': out += "\\b"; break;
      case '\f': out += "\\f"; break;
      default:
        if (c < 0x20) {
          char u[8];
          std::snprintf(u, sizeof(u), "\\u%04x", c);
          out += u;
        } else {
          out += ch;
        }
    }
  }
  out += '"';
}

}  // namespace

SourceId source_from_wire(std::string_view s) {
  if (s == "Kalshi") return SourceId::Kalshi;
  if (s == "Replay") return SourceId::Replay;
  if (s == "SportsOdds") return SourceId::SportsOdds;
  if (s == "SportsStats") return SourceId::SportsStats;
  if (s == "News") return SourceId::News;
  if (s == "Weather") return SourceId::Weather;
  if (s == "Custom") return SourceId::Custom;
  return SourceId::Unknown;
}

// --- RawLogWriter ---

RawLogWriter::RawLogWriter(std::string path, std::size_t max_bytes, bool fsync_each)
    : base_path_(std::move(path)), max_bytes_(max_bytes), fsync_each_(fsync_each) {
  time_partitioned_ = base_path_.find("{UTC_DATE}") != std::string::npos ||
                      base_path_.find("{UTC_HOUR}") != std::string::npos;
  if (!time_partitioned_) {
    active_base_path_ = base_path_;
    select_latest_shard();
    open_current();
  }
}

RawLogWriter::~RawLogWriter() {
  if (f_) std::fclose(f_);
}

bool RawLogWriter::close_current() {
  if (!f_) return true;
  std::FILE* closing = f_;
  f_ = nullptr;
  return std::fclose(closing) == 0;
}

bool RawLogWriter::open_current() {
  current_path_ = index_ == 0 ? active_base_path_
                              : active_base_path_ + "." + std::to_string(index_);
  std::error_code ec;
  const auto parent = std::filesystem::path(current_path_).parent_path();
  if (!parent.empty()) std::filesystem::create_directories(parent, ec);
  if (ec) {
    f_ = nullptr;
    return false;
  }
  f_ = std::fopen(current_path_.c_str(), "ab");
  bytes_ = 0;
  if (f_) {
    if (std::fseek(f_, 0, SEEK_END) != 0) {
      std::fclose(f_);
      f_ = nullptr;
      return false;
    }
    const long pos = std::ftell(f_);
    if (pos < 0) {
      std::fclose(f_);
      f_ = nullptr;
      return false;
    }
    bytes_ = static_cast<std::size_t>(pos);
  }
  return f_ != nullptr;
}

void RawLogWriter::select_latest_shard() {
  index_ = 0;
  // Rotation creates a contiguous .1, .2, ... sequence. Resuming the highest
  // existing shard preserves append chronology across process restarts.
  for (std::size_t candidate = 1;; ++candidate) {
    std::error_code ec;
    if (!std::filesystem::exists(
            active_base_path_ + "." + std::to_string(candidate), ec) || ec) {
      break;
    }
    index_ = candidate;
  }
}

std::string RawLogWriter::resolve_time_path(std::int64_t recv_wall_ns) const {
  if (!time_partitioned_) return base_path_;
  const std::time_t sec = static_cast<std::time_t>(recv_wall_ns / 1'000'000'000LL);
  std::tm utc{};
#if defined(_WIN32)
  gmtime_s(&utc, &sec);
#else
  gmtime_r(&sec, &utc);
#endif
  char date[16], hour[4];
  std::strftime(date, sizeof(date), "%Y-%m-%d", &utc);
  std::strftime(hour, sizeof(hour), "%H", &utc);
  std::string out = base_path_;
  auto replace_all = [&out](const std::string& token, const std::string& value) {
    std::size_t pos = 0;
    while ((pos = out.find(token, pos)) != std::string::npos) {
      out.replace(pos, token.size(), value);
      pos += value.size();
    }
  };
  replace_all("{UTC_DATE}", date);
  replace_all("{UTC_HOUR}", hour);
  return out;
}

bool RawLogWriter::ensure_time_partition(std::int64_t recv_wall_ns) {
  if (!time_partitioned_) return f_ != nullptr;
  const std::string desired = resolve_time_path(recv_wall_ns);
  if (desired == active_base_path_ && f_) return true;
  const bool first_open = active_base_path_.empty();
  if (!close_current()) return false;
  active_base_path_ = desired;
  select_latest_shard();
  if (!first_open) ++rotations_;
  return open_current();
}

bool RawLogWriter::rotate() {
  if (!close_current()) return false;
  ++index_;
  ++rotations_;
  return open_current();
}

bool RawLogWriter::write(const RawRecord& rec) {
  if (!ensure_time_partition(rec.recv_wall_ns) || !f_) return false;
  std::string line;
  line.reserve(rec.raw.size() + 256);
  line += "{\"recv_mono_ns\":";
  line += std::to_string(rec.recv_mono_ns);
  line += ",\"recv_wall_ns\":";
  line += std::to_string(rec.recv_wall_ns);
  if (rec.source_event_time_ms) {
    line += ",\"source_event_time_ms\":";
    line += std::to_string(*rec.source_event_time_ms);
  }
  line += ",\"source\":\"";
  line += to_string(rec.source);
  line += "\",\"channel\":";
  json_escape(rec.channel, line);
  line += ",\"source_ticker\":";
  json_escape(rec.source_ticker, line);
  if (rec.source_sequence) {
    line += ",\"source_sequence\":";
    line += std::to_string(*rec.source_sequence);
  }
  if (rec.source_stream_id) {
    line += ",\"sid\":";
    line += std::to_string(*rec.source_stream_id);
  }
  if (rec.stream_epoch != 0) {
    line += ",\"stream_epoch\":";
    line += std::to_string(rec.stream_epoch);
  }
  if (rec.marker) {
    line += ",\"marker\":";
    json_escape(*rec.marker, line);
  }
  if (is_valid_utf8(rec.raw)) {
    line += ",\"raw\":";
    json_escape(rec.raw, line);
  } else {
    line += ",\"raw_b64\":\"";
    line += base64_encode(rec.raw);
    line += '"';
  }
  line += "}\n";

  if (bytes_ > 0 && bytes_ + line.size() > max_bytes_ && !rotate()) return false;
  const std::size_t wrote = std::fwrite(line.data(), 1, line.size(), f_);
  if (wrote != line.size()) return false;
  bytes_ += line.size();
  if (fsync_each_ && std::fflush(f_) != 0) return false;
  return true;
}

bool RawLogWriter::flush() {
  return f_ != nullptr && std::fflush(f_) == 0;
}

// --- RawLogReader ---

RawLogReader::RawLogReader(const std::string& path) {
  f_ = std::fopen(path.c_str(), "rb");
}
RawLogReader::~RawLogReader() {
  if (f_) std::fclose(f_);
}

std::optional<RawRecord> RawLogReader::next() {
  if (!f_) return std::nullopt;
  // Loop (not recursion) so a run of corrupt lines can't overflow the stack.
  for (;;) {
    buf_.clear();
    int c;
    while ((c = std::fgetc(f_)) != EOF) {
      if (c == '\n') break;
      buf_ += static_cast<char>(c);
    }
    if (buf_.empty()) {
      if (c == EOF) return std::nullopt;  // clean end of file
      continue;                            // blank line — skip
    }
    if (c == EOF) {
      ++skipped_;                          // truncated final line — skip, not fatal
      return std::nullopt;
    }

    try {
      simdjson::padded_string json(buf_);
      simdjson::ondemand::parser parser;
      simdjson::ondemand::document doc;
      if (parser.iterate(json).get(doc) != simdjson::SUCCESS) { ++skipped_; continue; }
      // MUST confirm the top-level value is an object before field access —
      // ondemand field lookup on a non-object (array/scalar/null) is UB.
      simdjson::ondemand::object o;
      if (doc.get_object().get(o) != simdjson::SUCCESS) { ++skipped_; continue; }

      RawRecord r;
      std::int64_t i64;
      std::uint64_t u64;
      std::string_view sv;
      if (o["recv_mono_ns"].get(i64) == simdjson::SUCCESS) r.recv_mono_ns = i64;
      if (o["recv_wall_ns"].get(i64) == simdjson::SUCCESS) r.recv_wall_ns = i64;
      if (o["source_event_time_ms"].get(i64) == simdjson::SUCCESS) r.source_event_time_ms = i64;
      if (o["source"].get(sv) == simdjson::SUCCESS) r.source = source_from_wire(sv);
      if (o["channel"].get(sv) == simdjson::SUCCESS) r.channel = std::string(sv);
      if (o["source_ticker"].get(sv) == simdjson::SUCCESS) r.source_ticker = std::string(sv);
      if (o["source_sequence"].get(u64) == simdjson::SUCCESS) r.source_sequence = u64;
      if (o["sid"].get(u64) == simdjson::SUCCESS) r.source_stream_id = u64;  // absent = legacy
      if (o["stream_epoch"].get(u64) == simdjson::SUCCESS)
        r.stream_epoch = static_cast<std::uint32_t>(u64);
      if (o["marker"].get(sv) == simdjson::SUCCESS) r.marker = std::string(sv);
      if (o["raw"].get(sv) == simdjson::SUCCESS) {
        r.raw = std::string(sv);  // simdjson returns the unescaped bytes
      } else if (o["raw_b64"].get(sv) == simdjson::SUCCESS) {
        r.raw = base64_decode(sv);
      }
      return r;
    } catch (const simdjson::simdjson_error&) {
      ++skipped_;  // corrupt line — keep going
    }
  }
}

// --- ReplaySource ---

void ReplaySource::start() {
  RawLogReader reader(path_);
  std::optional<RawRecord> rec;
  while (!stopped_ && (rec = reader.next()).has_value()) {
    ++records_;
    if (rec->marker) {  // annotation, not decodable data
      ++markers_;
      if (marker_fn_) marker_fn_(*rec);
      continue;
    }
    if (auto ev = decoder_.decode(*rec)) {
      if (sink_) sink_->on_event(*ev);
      ++emitted_;
    }
  }
}

// --- NormalizedWriter ---

NormalizedWriter::NormalizedWriter(const std::string& path) {
  f_ = std::fopen(path.c_str(), "ab");
}
NormalizedWriter::~NormalizedWriter() {
  if (f_) std::fclose(f_);
}

void NormalizedWriter::write(const NormalizedEvent& e) {
  if (!f_) return;
  std::string line = "{\"trace_id\":";
  line += std::to_string(e.trace_id.v);
  line += ",\"source\":\"";
  line += to_string(e.source);
  line += "\",\"entity_id\":";
  line += std::to_string(e.entity_id.v);
  line += ",\"kind\":\"";
  line += to_string(e.kind());
  line += "\",\"recv_mono_ns\":";
  line += std::to_string(e.local_receive_mono_ns);
  if (e.source_sequence) {
    line += ",\"seq\":";
    line += std::to_string(*e.source_sequence);
  }
  if (e.source_stream_id) {
    line += ",\"sid\":";
    line += std::to_string(*e.source_stream_id);
  }
  if (e.stream_epoch != 0) {
    line += ",\"stream_epoch\":";
    line += std::to_string(e.stream_epoch);
  }
  line += "}\n";
  std::fwrite(line.data(), 1, line.size(), f_);
}

}  // namespace trading
