#include "trading/storage.hpp"

#include "simdjson.h"

#include <cstring>

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
  open_current();
}

RawLogWriter::~RawLogWriter() {
  if (f_) std::fclose(f_);
}

void RawLogWriter::open_current() {
  current_path_ = index_ == 0 ? base_path_ : base_path_ + "." + std::to_string(index_);
  f_ = std::fopen(current_path_.c_str(), "ab");
  bytes_ = 0;
}

void RawLogWriter::rotate() {
  if (f_) std::fclose(f_);
  ++index_;
  ++rotations_;
  open_current();
}

void RawLogWriter::write(const RawRecord& rec) {
  if (!f_) return;
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
  if (is_valid_utf8(rec.raw)) {
    line += ",\"raw\":";
    json_escape(rec.raw, line);
  } else {
    line += ",\"raw_b64\":\"";
    line += base64_encode(rec.raw);
    line += '"';
  }
  line += "}\n";

  if (bytes_ > 0 && bytes_ + line.size() > max_bytes_) rotate();
  std::fwrite(line.data(), 1, line.size(), f_);
  bytes_ += line.size();
  if (fsync_each_) std::fflush(f_);
}

void RawLogWriter::flush() {
  if (f_) std::fflush(f_);
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
  buf_.clear();
  int c;
  while ((c = std::fgetc(f_)) != EOF) {
    if (c == '\n') break;
    buf_ += static_cast<char>(c);
  }
  if (buf_.empty()) return std::nullopt;
  if (c == EOF) {
    // No terminating newline: truncated final line — skip, don't fail.
    ++skipped_;
    return std::nullopt;
  }

  try {
    simdjson::padded_string json(buf_);
    simdjson::ondemand::parser parser;
    auto doc = parser.iterate(json);
    RawRecord r;
    std::int64_t i64;
    std::uint64_t u64;
    std::string_view sv;
    if (doc["recv_mono_ns"].get(i64) == simdjson::SUCCESS) r.recv_mono_ns = i64;
    if (doc["recv_wall_ns"].get(i64) == simdjson::SUCCESS) r.recv_wall_ns = i64;
    if (doc["source_event_time_ms"].get(i64) == simdjson::SUCCESS) r.source_event_time_ms = i64;
    if (doc["source"].get(sv) == simdjson::SUCCESS) r.source = source_from_wire(sv);
    if (doc["channel"].get(sv) == simdjson::SUCCESS) r.channel = std::string(sv);
    if (doc["source_ticker"].get(sv) == simdjson::SUCCESS) r.source_ticker = std::string(sv);
    if (doc["source_sequence"].get(u64) == simdjson::SUCCESS) r.source_sequence = u64;
    if (doc["raw"].get(sv) == simdjson::SUCCESS) {
      r.raw = std::string(sv);  // simdjson returns the unescaped bytes
    } else if (doc["raw_b64"].get(sv) == simdjson::SUCCESS) {
      r.raw = base64_decode(sv);
    }
    return r;
  } catch (const simdjson::simdjson_error&) {
    ++skipped_;
    return next();  // skip a corrupt line, keep going
  }
}

// --- ReplaySource ---

void ReplaySource::start() {
  RawLogReader reader(path_);
  std::optional<RawRecord> rec;
  while (!stopped_ && (rec = reader.next()).has_value()) {
    ++records_;
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
  line += "}\n";
  std::fwrite(line.data(), 1, line.size(), f_);
}

}  // namespace trading
