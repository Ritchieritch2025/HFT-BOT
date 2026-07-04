#pragma once
//
// Streaming (operator<<) for the source-agnostic bus types. Kept out of the
// hot headers so <ostream> weight is opt-in — include this only where you print.

#include "trading/bus.hpp"
#include "trading/fixedpoint.hpp"

#include <ostream>

namespace trading {

inline std::ostream& operator<<(std::ostream& os, SourceId s) { return os << to_string(s); }
inline std::ostream& operator<<(std::ostream& os, Side s) { return os << to_string(s); }
inline std::ostream& operator<<(std::ostream& os, Kind k) { return os << to_string(k); }
inline std::ostream& operator<<(std::ostream& os, ExecDecision d) { return os << to_string(d); }

inline std::ostream& operator<<(std::ostream& os, EntityId e) {
  return os << "E#" << e.v;
}
inline std::ostream& operator<<(std::ostream& os, TraceId t) {
  return os << "T#" << t.v;
}

inline std::ostream& operator<<(std::ostream& os, const NormalizedEvent& e) {
  os << "NormalizedEvent{src=" << e.source << " entity=" << e.entity_id
     << " kind=" << e.kind() << " trace=" << e.trace_id;
  if (e.source_sequence) os << " seq=" << *e.source_sequence;
  return os << "}";
}

inline std::ostream& operator<<(std::ostream& os, const FeatureVector& f) {
  return os << "FeatureVector{entity=" << f.entity_id << " trace=" << f.trace_id
            << " n=" << f.values.size() << "}";
}

inline std::ostream& operator<<(std::ostream& os, const Signal& s) {
  return os << "Signal{entity=" << s.entity_id << " trace=" << s.trace_id
            << " side=" << s.side << " strength=" << s.strength << "}";
}

inline std::ostream& operator<<(std::ostream& os, const OrderIntent& o) {
  os << "OrderIntent{entity=" << o.entity_id << " trace=" << o.trace_id
     << " side=" << o.side << " price=" << format_price_e4(o.limit_price)
     << " size=" << format_count_fp(o.size)
     << " tif=" << (o.tif == OrderIntent::TimeInForce::IOC ? "ioc" : "gtc");
  if (o.post_only) os << " post_only";
  return os << "}";
}

}  // namespace trading
